"""Frame-bound live panorama sessions with a single replaceable physical intention.

No capture sweep, mapping activation, preset creation or automatic return. The
existing camera lease, bounded motion and visual observation barriers remain in
charge of the device. Browser media identities are explicitly local observations.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
import io
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from PIL import Image

from .panorama import _digest, _error
from .panorama_capture import PanoramaCaptureError
from .panorama_navigation import (
    MAXIMUM_LIVE_CENTER_ERROR_PIXELS, VisualNavigator,
    select_direct_native_reference, select_native_reference,
)
from .panorama_scan import ATTEMPT_SECONDS, _Scan, _Stopped
from .processing.panorama_mapping import _rotation_basis, panorama_pixel_to_ray
from .settings import iter_camera_devices, iter_camera_sources

MAXIMUM_LIVE_NAVIGATION_COMMANDS = 16


@dataclass
class _ControlChain:
    stack: AsyncExitStack
    reference_held: bool = False
    camera: Any = None
    stopped_scanner: Any = None
    prepared_at: float = 0

    async def close_camera(self) -> None:
        camera, self.camera = self.camera, None
        self.stopped_scanner = None
        if camera is not None:
            await camera.close()


async def _stopped_control_is_current(scanner: Any, camera: Any) -> bool:
    frame = scanner.last_frame or {}
    evidence = frame.get("capture_evidence") or {}
    received = frame.get("received_monotonic")
    accepted = getattr(scanner, "last_stop_accepted_monotonic", None)
    if (scanner.physical_state != "stopped" or not evidence.get("capture_instance")
            or type(evidence.get("generation")) is not int
            or type(evidence.get("sequence")) is not int
            or type(received) not in (int, float) or type(accepted) not in (int, float)
            or not accepted <= received <= time.monotonic()
            or not 0 <= time.monotonic() - received <= 1):
        return False
    try:
        current = await camera.stop_receipt_is_current(scanner.last_stop_receipt)
    except Exception:
        return False
    return current is True and 0 <= time.monotonic() - received <= 1


async def _arrival_stop_is_current(scanner: Any, camera: Any, result: Any) -> bool:
    """Reuse fresh visual arrival only while its accepted Stop still owns control."""
    frame = scanner.last_frame or {}
    evidence = frame.get("capture_evidence") or {}
    receipt = getattr(scanner, "last_stop_receipt", None)
    accepted = getattr(scanner, "last_stop_accepted_monotonic", None)
    received = frame.get("received_monotonic")
    if (not isinstance(result, dict) or result.get("verified") is not True
            or result.get("kind") != "visual_aim_verified"
            or scanner.physical_state != "stopped"
            or not evidence.get("capture_instance")
            or type(evidence.get("generation")) is not int
            or type(evidence.get("sequence")) is not int
            or result.get("capture_evidence") != evidence
            or type(accepted) not in (float, int) or type(received) not in (float, int)
            or not accepted <= received <= time.monotonic()
            or not 0 <= time.monotonic() - received <= 1.0):
        return False
    error = (result.get("measurement") or {}).get("center_error_pixels")
    if type(error) not in (float, int) or not 0 <= error <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS:
        return False
    try:
        current = await camera.stop_receipt_is_current(receipt)
    except Exception:
        return False
    # The control lookup can wait; never renew the image's freshness with it.
    return current is True and 0 <= time.monotonic() - received <= 1.0


class OpenSession(BaseModel):
    camera_id: str
    source_id: str
    artifact_id: str
    revision: int = Field(ge=1)


class Intention(BaseModel):
    sequence: int = Field(strict=True, ge=1)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    # Callers with a smaller physical budget can reduce the existing bound;
    # an intention can never enlarge the product's command limit.
    maximum_commands: int = Field(default=MAXIMUM_LIVE_NAVIGATION_COMMANDS,
                                  strict=True, ge=1, le=MAXIMUM_LIVE_NAVIGATION_COMMANDS)


class Stop(BaseModel):
    sequence: int = Field(strict=True, ge=1)


class Observation(BaseModel):
    sequence: int = Field(strict=True, ge=1)
    epoch: str = Field(min_length=1, max_length=96)
    media_time: float = Field(ge=0, allow_inf_nan=False)
    width: int = Field(ge=2, le=8192)
    height: int = Field(ge=2, le=8192)
    image: str = Field(max_length=2_000_000)


@dataclass
class Session:
    id: str
    camera_id: str
    source_id: str
    binding: dict
    localizer: Any
    request: Request
    can_control: bool = False
    sequence: int = 0
    pending: tuple[Intention, float] | None = None
    task: asyncio.Task | None = None
    phase: str = "localizing"
    error: str | None = None
    blocked: bool = False
    closed: bool = False
    seen: float = field(default_factory=time.monotonic)
    observed_sequence: int = 0
    observation_epoch: str | None = None
    last_registration: float = 0
    departure_hint: dict | None = None
    commands: int = 0
    response: dict = field(default_factory=dict)
    response_rotations: dict = field(default_factory=dict)
    result: dict | None = None
    observation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prepared_camera: Any = None
    preparation: asyncio.Future | None = None
    prepared_at: float = 0
    control_chain: _ControlChain | None = None
    recognition_task: asyncio.Task | None = None

    def public(self) -> dict:
        return dict(
            session_id=self.id,
            can_control=self.can_control,
            sequence=self.sequence,
            phase=self.phase,
            error=self.error,
            blocked=self.blocked,
            closed=self.closed,
            pending_sequence=self.pending[0].sequence if self.pending else None,
            moving=bool(self.task and not self.task.done()),
            commands=self.commands,
            result=self.result,
        )


class LivePanoramaService:
    def __init__(self, panorama: Any, sources: Any):
        self.panorama, self.sources = panorama, sources
        self.sessions: dict[str, Session] = {}
        self.camera_owners: dict[str, str] = {}
        self.recognition_tasks: dict[str, asyncio.Task] = {}
        self.watchdog: asyncio.Task | None = None

    async def catalog(self, request: Request) -> dict:
        settings = await self.sources.read_settings(request)
        choices = []
        for camera in iter_camera_devices(settings):
            try:
                self.sources._authorize(request, camera["id"])
            except HTTPException:
                continue
            for source in iter_camera_sources(camera, enabled_only=True):
                data = await self.sources.get_source(request, camera["id"], source["id"])
                for kind in ("active", "candidate"):
                    artifact = data.get(kind)
                    selected_reference = self.sources._pointer(source).get(kind)
                    if (
                        not artifact
                        or not isinstance(selected_reference, dict)
                        or selected_reference.get("artifact_id") != artifact["id"]
                    ):
                        continue
                    reason = None
                    try:
                        self.panorama._source_artifact(
                            camera,
                            source,
                            artifact["id"],
                            artifact["revision"],
                            require_active=False,
                        )
                    except HTTPException as error:
                        reason = error.detail.get("code", "panorama_unavailable")
                    choices.append(
                        dict(
                            camera_id=camera["id"],
                            camera_name=camera.get("name", camera["id"]),
                            source_id=source["id"],
                            source_name=source.get("name", source["id"]),
                            kind=kind,
                            artifact=artifact,
                            reason=reason,
                            secondary_sources=[
                                dict(id=item["id"], name=item.get("name", item["id"]))
                                for item in iter_camera_sources(camera, enabled_only=True)
                                if item.get("role") == "zoom" and item["id"] != source["id"]
                            ],
                        )
                    )
        return {"choices": choices}

    async def open(self, request: Request, body: OpenSession) -> dict:
        self.sources._authorize(request, body.camera_id)
        _, camera, source = await self.sources._context(body.camera_id, body.source_id, request)
        pointer = self.sources._pointer(source)
        if not any(
            isinstance(pointer.get(kind), dict)
            and pointer[kind].get("artifact_id") == body.artifact_id
            for kind in ("active", "candidate")
        ):
            raise _error(
                "source_panorama_changed", "A seleção mudou; atualize a lista de panoramas."
            )
        binding, _ = self.panorama._source_artifact(
            camera, source, body.artifact_id, body.revision, require_active=False
        )
        binding["selection_digest"] = _digest(self.sources._pointer(source))
        localizer = await self.panorama.reference_localizer(body.camera_id, body.source_id, binding)
        # Bounded sessions and no device action merely to open a view.
        if len(self.sessions) >= 16:
            raise _error("live_session_limit", "Feche outra sessão de panorâmica ao vivo.")
        session = Session(
            uuid.uuid4().hex, body.camera_id, body.source_id, binding, localizer, request
        )
        try:
            self.sources._authorize(request, body.camera_id, control=True)
            session.can_control = True
        except HTTPException:
            session.error = "camera_control_permission_required"
        self.sessions[session.id] = session
        if self.watchdog is None or self.watchdog.done():
            self.watchdog = asyncio.create_task(self._watch(), name="live-panorama-expiration")
        return {**session.public(), "panorama": binding, "lens": localizer.lens}

    def session(self, request: Request, identifier: str, *, control: bool = False) -> Session:
        session = self.sessions.get(identifier)
        if not session or session.closed:
            raise _error("live_session_expired", "Reabra a panorâmica ao vivo.", 410)
        try:
            self.sources._authorize(request, session.camera_id, control=control)
        except HTTPException:
            session.closed, session.pending = True, None
            raise
        try:
            self.sources._authorize(request, session.camera_id, control=True)
        except HTTPException:
            if session.can_control:
                session.sequence += 1
            session.can_control = False
            session.pending = None
            session.error = "camera_control_permission_required"
        session.seen = time.monotonic()
        return session

    async def _start_preparation(self, session: Session) -> None:
        if (session.closed or session.blocked or not session.can_control
                or (session.task is not None and not session.task.done())):
            return
        if session.preparation is not None:
            if not session.preparation.done() or time.monotonic() - session.prepared_at <= 30:
                return
            await self._discard_preparation(session)
            # Closing yields: a concurrent observation or click may now own
            # the session. Only the active, aligned observer prepares metadata.
            if (session.closed or session.blocked or not session.can_control or session.preparation is not None
                    or (session.task is not None and not session.task.done())):
                return
        camera = self.panorama.camera_factory(
            services=self.panorama.services, camera_id=session.camera_id, source_id=session.source_id,
            settings={}, job_id=f"live-{session.id}",
            output_dir=self.panorama.root.parent / "live-panorama" / session.id / "preparation",
        )
        session.prepared_camera = camera
        task = session.preparation = asyncio.create_task(camera.discover())

        def completed(result: asyncio.Task) -> None:
            if not result.cancelled():
                # Retrieve failures even if the viewer never clicks. Awaiting
                # this task in an intention still propagates the same failure.
                result.exception()
            if session.preparation is result:
                session.prepared_at = time.monotonic()

        task.add_done_callback(completed)

    def _prime_recognition(self, session: Session, sequence: int, frame: dict, diagnostics: dict) -> None:
        """Recognize one immutable observation; never publish its pose or use hardware."""
        previous = self.recognition_tasks.get(session.camera_id)
        locate = getattr(session.localizer, "locate", None)
        received = frame.get("received_monotonic")
        if (session.closed or session.blocked or not session.can_control or session.sequence != sequence
                or (previous is not None and not previous.done()) or not callable(locate)
                or type(received) not in (int, float) or not 0 <= time.monotonic() - received <= 1
                or not isinstance(frame.get("image"), np.ndarray)):
            return
        image = frame["image"].copy()
        evidence = dict(frame.get("capture_evidence") or {})
        started = time.monotonic()
        diagnostics["early_recognition"] = {"state": "running"}

        async def recognize() -> None:
            worker = asyncio.create_task(asyncio.to_thread(locate, image, evidence))
            try:
                result = await asyncio.shield(worker)
                diagnostics["early_recognition"].update(state="finished", status=result.get("status"))
            except asyncio.CancelledError:
                diagnostics["early_recognition"]["state"] = "cancelled"
                raise
            except Exception:
                # Preparation cannot replace the final frame's independent decision.
                diagnostics["early_recognition"]["state"] = "failed"
            finally:
                # Cancelling an asyncio wrapper cannot stop a running model thread.
                # Drain it on session cleanup, never through the physical-operation queue.
                await asyncio.shield(asyncio.gather(worker, return_exceptions=True))
                diagnostics["early_recognition"]["seconds"] = time.monotonic() - started

        task = session.recognition_task = asyncio.create_task(recognize())
        self.recognition_tasks[session.camera_id] = task

        def completed(result: asyncio.Task) -> None:
            if not result.cancelled():
                result.exception()
            if self.recognition_tasks.get(session.camera_id) is result:
                self.recognition_tasks.pop(session.camera_id, None)

        task.add_done_callback(completed)

    async def _drain_recognition(self, session: Session) -> None:
        task = session.recognition_task
        if task is not None:
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            if session.recognition_task is task:
                session.recognition_task = None

    async def _discard_preparation(self, session: Session) -> None:
        task, camera = session.preparation, session.prepared_camera
        session.preparation, session.prepared_camera = None, None
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if camera is not None:
            await camera.close()

    def _retain_retarget_metadata(self, session: Session, camera: Any, capabilities: dict,
                                  prepared_at: float, *, stopped: bool, failed: bool) -> bool:
        """Carry protocol discovery across a serial retarget, never pose or leases.

        The caller has already closed/released this operation's resources. The
        next operation still checks binding/authority and refreshes observations
        before acquiring a new lease. Do not extend discovery's original age.
        """
        if (failed or not stopped or session.closed or session.blocked or not session.can_control
                or session.pending is None or session.preparation is not None
                or not 0 <= time.monotonic() - prepared_at <= 30
                or not callable(getattr(camera, "refresh_discovery_observations", None))):
            return False
        prepared = asyncio.get_running_loop().create_future()
        prepared.set_result(capabilities)
        session.prepared_camera = camera
        session.preparation = prepared
        session.prepared_at = prepared_at
        return True

    async def current(self, session: Session) -> dict:
        _, camera, source = await self.sources._context(
            session.camera_id, session.source_id, session.request
        )
        binding, _ = self.panorama._source_artifact(
            camera, source, session.binding["id"], session.binding["revision"], require_active=False
        )
        if (
            _digest(self.sources._pointer(source)) != session.binding["selection_digest"]
            or binding["source_identity"] != session.binding["source_identity"]
            or binding["geometry"] != session.binding["geometry"]
        ):
            raise _error("source_panorama_changed", "A referência mudou; reabra esta visualização.")
        return camera

    async def observe(self, session: Session, body: Observation) -> dict:
        async with session.observation_lock:
            await self.current(session)
            if session.observation_epoch != body.epoch:
                session.observation_epoch = body.epoch
                session.observed_sequence = 0
                session.last_registration = 0
            if body.sequence <= session.observed_sequence:
                raise _error("stale_frame", "Observação antiga descartada.")
            session.observed_sequence = body.sequence
            intention_sequence = session.sequence
            if session.task and not session.task.done():
                return {**session.public(), "status": "unlocalized", "reason": "moving"}
            lens = session.localizer.lens
            # Only the original optical raster is admitted. Equal aspect ratio
            # does not establish crop/flip/source provenance for another stream.
            if (body.width, body.height) != (lens["width"], lens["height"]):
                return {
                    **session.public(),
                    "status": "unlocalized",
                    "reason": "panorama_source_geometry_changed",
                }
            try:
                encoded = base64.b64decode(body.image, validate=True)
                with Image.open(io.BytesIO(encoded)) as header:
                    if header.width > 960 or header.height > 960 or min(header.size) < 32:
                        raise ValueError()
                image = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
                if (
                    image is None
                    or image.shape[1] > 960
                    or image.shape[0] > 960
                    or min(image.shape[:2]) < 32
                ):
                    raise ValueError()
                scale_x, scale_y = body.width / image.shape[1], body.height / image.shape[0]
                if abs(scale_x / scale_y - 1) > 0.005:
                    raise ValueError()
            except (ValueError, OSError, cv2.error):
                raise _error("invalid_frame", "Frame de análise inválido.", 422) from None
            evidence = dict(
                capture_instance=f"browser:{session.id}:{body.epoch}"[:128],
                generation=0,
                sequence=body.sequence,
            )
            geometry = dict(
                source_size=[body.width, body.height],
                image_size=[image.shape[1], image.shape[0]],
                to_source=[
                    [scale_x, 0, (scale_x - 1) / 2],
                    [0, scale_y, (scale_y - 1) / 2],
                    [0, 0, 1],
                ],
                capture_evidence=evidence,
            )
            started = time.monotonic()
            located = await asyncio.to_thread(session.localizer.locate, image, evidence, geometry,
                                             reference_candidates=True)
            elapsed = time.monotonic() - started
            if (
                session.closed
                or intention_sequence != session.sequence
                or session.task
                and not session.task.done()
            ):
                return {"status": "unlocalized", "reason": "superseded_observation"}
            if elapsed >= 1.5:
                # Slow recognition can seed image correspondences, but its old
                # frame must not grant pointing authority or a visible pose.
                located = {"status": "unlocalized", "reason": "panorama_frame_not_recent"}
            output = {k: v for k, v in located.items() if k != "capture_evidence"}
            output.update(
                observation_sequence=body.sequence,
                epoch=body.epoch,
                media_time=body.media_time,
                timing_basis="browser_presented_frame",
                estimator_ms=elapsed * 1000,
            )
            if located.get("status") == "localized":
                session.last_registration = time.monotonic()
                # Only selects a prepared destination; never certifies motor
                # state, departure stability, or arrival of an operation.
                session.departure_hint = {"rotation_matrix": located["rotation_matrix"],
                                          "observed_at": session.last_registration}
                await self._start_preparation(session)
                output["geometry"] = dict(
                    lens=lens,
                    panorama_to_camera=_rotation_basis(located["rotation_matrix"]).T.tolist(),
                )
                if not session.blocked:
                    session.phase = "aligned"
            else:
                session.last_registration = 0
                session.departure_hint = None
                if not session.blocked:
                    session.phase = "unlocalized"
            return {**session.public(), **output}

    def intend(self, session: Session, body: Intention) -> dict:
        if body.sequence <= session.sequence:
            return session.public()
        if session.blocked:
            raise _error(
                "live_control_uncertain", "Controle suspenso: requalifique o estado físico."
            )
        mask = session.localizer.coverage_mask
        x, y = (
            min(mask.shape[1] - 1, int(body.x * mask.shape[1])),
            min(mask.shape[0] - 1, int(body.y * mask.shape[0])),
        )
        ray = panorama_pixel_to_ray(body.x, body.y)
        if mask[y, x] == 0 or session.localizer.target_reference(ray) is None:
            raise _error("visual_target_unreachable", "Ponto fora da cobertura óptica qualificada.")
        if not session.task or session.task.done():
            if time.monotonic() - session.last_registration > 3:
                raise _error("live_registration_required", "Aguarde a localização do vídeo atual.")
            if self.camera_owners.get(session.camera_id) not in (None, session.id):
                raise _error("camera_busy", "Outra sessão controla esta câmera.")
            self.camera_owners[session.camera_id] = session.id
        session.sequence = body.sequence
        session.pending = (body, time.monotonic() + 10)
        session.result = None
        session.error = None
        session.phase = "moving"
        session.last_registration = 0
        if not session.task or session.task.done():
            session.task = asyncio.create_task(
                self._run(session), name=f"live-panorama:{session.id}"
            )
        return session.public()

    def stop(self, session: Session, body: Stop) -> dict:
        if body.sequence > session.sequence:
            session.sequence = body.sequence
            session.pending = None
            session.last_registration = 0
            session.result = None
            session.phase = "stopping" if session.task and not session.task.done() else "localizing"
        return session.public()

    async def _run(self, session: Session) -> None:
        try:
            async with AsyncExitStack() as stack:
                session.control_chain = _ControlChain(stack)
                while session.pending and not session.closed and not session.blocked:
                    intention, expires = session.pending
                    session.pending = None
                    if time.monotonic() > expires:
                        session.error, session.phase = "intention_expired", "localizing"
                        break
                    await self._operate(session, intention, expires)
        except asyncio.CancelledError:
            session.pending = None
            session.result = None
            raise
        except Exception as error:
            session.pending = None
            session.phase = "error"
            session.error = getattr(error, "code", None) or (
                error.detail.get("code")
                if isinstance(error, HTTPException) and isinstance(error.detail, dict)
                else "live_control_failed"
            )
        finally:
            session.control_chain = None
            if session.closed or session.blocked:
                session.pending = None
            if self.camera_owners.get(session.camera_id) == session.id:
                self.camera_owners.pop(session.camera_id, None)
            session.last_registration = 0
            if session.phase in ("moving", "stopping", "stabilizing"):
                session.phase = "localizing"

    @asynccontextmanager
    async def _hold_reference(self, session: Session):
        chain = session.control_chain
        if chain is None:
            async with self.panorama.reference_coordinator.hold(session.camera_id, session.source_id):
                yield
            return
        if not chain.reference_held:
            await chain.stack.enter_async_context(
                self.panorama.reference_coordinator.hold(session.camera_id, session.source_id)
            )
            chain.reference_held = True
            # Close control/capture before releasing the reference lock.
            chain.stack.push_async_callback(chain.close_camera)
        yield

    async def _operate(self, session: Session, intention: Intention, expires: float) -> None:
        started = time.monotonic()
        hint = session.departure_hint
        if hint is not None and not 0 <= started - hint["observed_at"] <= 1.5:
            hint = None
        session.departure_hint = None  # Never carry it into a retarget after physical motion.
        timings = {}
        async with self._hold_reference(session):
            timings["coordinator_acquired"] = time.monotonic() - started
            camera_settings = await self.current(session)
            self.sources._authorize(session.request, session.camera_id, control=True)

            def cancelled():
                return (
                    session.closed
                    or session.sequence != intention.sequence
                    or time.monotonic() - session.seen > 15
                )

            if cancelled():
                return
            if time.monotonic() > expires:
                raise _error("intention_expired", "O destino expirou antes de obter o controle.")
            directory = (
                self.panorama.root.parent / "live-panorama" / session.id / str(intention.sequence)
            )
            preparation = None
            metadata_prepared_at = 0.0
            chain = session.control_chain
            previous_scanner = chain.stopped_scanner if chain is not None else None
            if chain is not None and chain.camera is not None and not 0 <= time.monotonic() - chain.prepared_at <= 30:
                await chain.close_camera()
                previous_scanner = None
            if (session.preparation is not None and session.preparation.done()
                    and time.monotonic() - session.prepared_at > 30):
                await self._discard_preparation(session)
            if chain is not None and chain.camera is not None:
                camera = chain.camera
                metadata_prepared_at = chain.prepared_at
                preparation = asyncio.get_running_loop().create_future()
                preparation.set_result(previous_scanner.capabilities)
                chain.stopped_scanner = None
            elif session.preparation is not None:
                preparation, camera = session.preparation, session.prepared_camera
                timings["discovery_waited_for_preparation"] = not preparation.done()
                # A click can take ownership before discovery finishes. Its
                # completion callback then no longer owns session.preparation,
                # so it cannot stamp this operation's metadata. Start the age
                # conservatively before waiting, never renew completed metadata.
                metadata_prepared_at = (
                    session.prepared_at if preparation.done() else time.monotonic()
                )
                session.preparation, session.prepared_camera = None, None
            else:
                camera = self.panorama.camera_factory(
                    services=self.panorama.services,
                    camera_id=session.camera_id,
                    source_id=session.source_id,
                    settings={},
                    job_id=f"live-{session.id}",
                    output_dir=directory,
                )
            if chain is not None:
                chain.camera = camera

            async def progress(event: dict) -> None:
                if session.sequence == intention.sequence:
                    session.phase = (
                        "moving" if event.get("physical_state") != "stopped" else "stabilizing"
                    )

            scanner = _Scan(camera, directory, progress, cancelled, {})
            # A fresh session must first observe both actuator axes before it
            # can apply measured corrections. Three commands only paid for the
            # two probes and one correction, which made valid distant clicks
            # fail predictably. Keep the operation bounded while leaving room
            # for convergence and fine visual confirmation.
            navigator = VisualNavigator(
                scanner,
                session.localizer,
                maximum_commands=intention.maximum_commands,
                stable_frame_observer=lambda frame: self._prime_recognition(
                    session, intention.sequence, frame, timings),
            )
            navigator.response = session.response
            navigator.response_rotations = session.response_rotations
            failure = None
            endpoint_refresh = None
            continuous_preparation = None
            try:
                if previous_scanner is not None:
                    # The retained lease already owns this stopped capture. Its
                    # next stream barrier can overlap fresh, read-only discovery;
                    # neither result alone permits the next physical command.
                    await camera.acquire()
                    scanner.acquired = True
                    if await _stopped_control_is_current(previous_scanner, camera):
                        scanner.last_frame = previous_scanner.last_frame
                        scanner.last_stop_receipt = previous_scanner.last_stop_receipt
                        scanner.last_stop_accepted_monotonic = previous_scanner.last_stop_accepted_monotonic
                        scanner.physical_state = "stopped"
                        endpoint_refresh = asyncio.create_task(scanner.refresh_stopped_frame())
                if preparation is None:
                    scanner.capabilities = await camera.discover()
                    metadata_prepared_at = time.monotonic()
                else:
                    await preparation
                    scanner.capabilities = await camera.refresh_discovery_observations()
                timings["discovery_prepared"] = preparation is not None
                timings["discovery_metadata_age_seconds"] = max(0.0, time.monotonic() - metadata_prepared_at)
                timings["discovery_complete"] = time.monotonic() - started
                automation = scanner.capabilities.get("motion_automation", {})
                explicitly_disabled = all(
                    automation.get(key) is False for key in ("auto_tracking", "automatic_return")
                )
                operator_confirmed = (camera_settings or {}).get("control", {}).get(
                    "automation_exclusive_control_confirmed"
                ) is True
                if any(value is True for value in automation.values()):
                    raise PanoramaCaptureError("external_automation_active")
                if not explicitly_disabled and not operator_confirmed:
                    raise PanoramaCaptureError("motion_automation_unqualified")
                if cancelled():
                    return
                if time.monotonic() > expires:
                    raise _error(
                        "intention_expired", "O destino expirou antes de obter o controle."
                    )
                await camera.acquire()
                timings["control_acquired"] = time.monotonic() - started
                scanner.acquired = True
                scanner._check()
                continued = False
                if endpoint_refresh is not None:
                    scanner.last_frame = await asyncio.shield(endpoint_refresh)
                    continued = await _stopped_control_is_current(scanner, camera)
                if not continued:
                    # Slow discovery may age the concurrent observation. Use
                    # the full existing Stop/window path instead of renewing it.
                    if not await scanner._stop():
                        raise PanoramaCaptureError("stop_unconfirmed")
                    timings["initial_stop_accepted"] = time.monotonic() - started
                    if scanner.capabilities.get("velocity_supported"):
                        # Read-only, lease-bound preparation overlaps observation;
                        # never postpone Stop or native travel to await it.
                        continuous_preparation = asyncio.create_task(camera.prepare_continuous_move())
                    scanner.last_frame = await scanner._reference_window()
                    scanner.physical_state = "stopped"
                timings["retarget_stop_continued"] = continued
                timings["initial_reference_observed"] = time.monotonic() - started
                scanner._check()
                references = self.panorama.native_references(session.camera_id, session.source_id, session.binding)
                ray = panorama_pixel_to_ray(intention.x, intention.y)
                selected = select_native_reference(session.localizer, hint, ray, references) if hint and references else None
                timings["native_departure_hint_used"] = selected is not None
                if selected is None and references:
                    selected = select_direct_native_reference(session.localizer, ray, references, hint)
                    timings["native_direct_target_used"] = selected is not None
                if selected is not None:
                    navigator.selected_native_reference = selected
                    await camera.bind_reference_destination(selected["destination"])
                    result = await navigator.aim(ray, native_destination=selected["destination"], native_ray=selected["ray"])
                else:
                    await navigator.prepare_stopped_view()
                    timings["initial_view_qualified"] = time.monotonic() - started
                    result = await navigator.aim(ray, **({"native_references": references} if references else {}))
                timings["aim_complete"] = time.monotonic() - started
                if not cancelled():
                    session.result = {"sequence": intention.sequence, **result}
            except _Stopped:
                pass
            except Exception as error:
                failure = error
                if (getattr(navigator, "selected_native_reference", None) is not None
                        and getattr(error, "code", None) in {
                            "visual_native_reference_unconfirmed", "return_binding_changed", "return_preset_changed",
                            "return_preset_ambiguous", "return_optical_state_mismatch", "return_owner_mismatch",
                        }):
                    self.panorama.invalidate_native_reference(navigator.selected_native_reference, error.code)
            finally:
                if continuous_preparation is not None and not continuous_preparation.done() and (cancelled() or failure is not None):
                    continuous_preparation.cancel()
                if endpoint_refresh is not None:
                    # A rejected/obsolete intent must not release capture while
                    # its read-only observer is still using it.
                    await asyncio.shield(asyncio.gather(endpoint_refresh, return_exceptions=True))
                session.commands += navigator.commands
                metadata_renewal = None
                def metadata_needs_renewal() -> bool:
                    return (chain is not None and failure is None and scanner.acquired
                        and not session.closed and not session.blocked and session.can_control
                        and session.pending is not None and time.monotonic() <= session.pending[1]
                        and not 0 <= time.monotonic() - metadata_prepared_at <= 30)

                if metadata_needs_renewal():
                    # Read-only discovery can overlap the mandatory Stop observation.
                    # Neither its old metadata nor its result authorizes movement.
                    metadata_renewal = asyncio.create_task(camera.refresh_discovery_metadata())
                try:
                    if scanner.acquired:
                        try:
                            reused = (failure is None and not cancelled()
                                      and await _arrival_stop_is_current(scanner, camera, session.result))
                            # Exposure can still settle after a cancelled pulse.
                            # Observe under the scanner's existing total budget;
                            # retain all visual gates and pending-intent expiry.
                            if not reused or cancelled():
                                await scanner._confirm_stop(observation_timeout=ATTEMPT_SECONDS, reuse_accepted=cancelled())
                            else:
                                scanner.checkpoint["final_stop_reused"] = True
                        except Exception:
                            scanner.physical_state = "stop_unconfirmed"
                        timings["final_stop_qualified"] = time.monotonic() - started
                        if scanner.physical_state != "stopped":
                            session.blocked = True
                            session.result = None
                            session.pending = None
                            session.error = "stop_unconfirmed"
                            session.phase = "error"
                        elif session.result is not None and not cancelled():
                            try:
                                epoch = await camera.motion_epoch()
                            except Exception:
                                epoch = None
                            if type(epoch) is int:
                                session.result["motion_epoch"] = epoch
                finally:
                    if continuous_preparation is not None:
                        prepared = await asyncio.shield(asyncio.gather(
                            continuous_preparation, return_exceptions=True))
                        timings["continuous_metadata_prepared"] = prepared[0] is True
                    if (metadata_renewal is None and scanner.physical_state == "stopped"
                            and metadata_needs_renewal()):
                        metadata_renewal = asyncio.create_task(camera.refresh_discovery_metadata())
                    if metadata_renewal is not None:
                        try:
                            scanner.capabilities = await metadata_renewal
                            metadata_prepared_at = time.monotonic()
                            timings["retarget_metadata_renewed"] = True
                        except Exception as error:
                            failure = error
                        finally:
                            if not metadata_renewal.done():
                                metadata_renewal.cancel()
                            await asyncio.gather(metadata_renewal, return_exceptions=True)
                    keep_control = (
                        chain is not None and failure is None and scanner.physical_state == "stopped"
                        and not session.closed and not session.blocked and session.can_control
                        and session.pending is not None and time.monotonic() <= session.pending[1]
                        and session.preparation is None
                        and 0 <= time.monotonic() - metadata_prepared_at <= 30
                    )
                    if keep_control:
                        chain.stopped_scanner = scanner
                        chain.prepared_at = metadata_prepared_at
                    else:
                        if chain is not None:
                            await chain.close_camera()
                        else:
                            await camera.close()
                        timings["camera_released"] = time.monotonic() - started
                    timings["retarget_control_retained"] = keep_control
                timings["retarget_metadata_retained"] = not keep_control and self._retain_retarget_metadata(
                    session, camera, scanner.capabilities, metadata_prepared_at,
                    stopped=scanner.physical_state == "stopped", failed=failure is not None,
                )
                try:
                    await asyncio.to_thread(navigator.localization_replay.preserve,
                        self.panorama.root.parent / "live-panorama-localization",
                        {"session_id": session.id, "sequence": intention.sequence,
                         "physical_state": scanner.physical_state})
                except (OSError, ValueError):
                    scanner.issues.append({"code": "localization_replay_write_failed"})
                timings["replay_preserved"] = time.monotonic() - started
                self.panorama._atomic(
                    directory / "live-result.json",
                    dict(
                        sequence=intention.sequence,
                        commands=navigator.commands,
                        total_commands=session.commands,
                        superseded=cancelled(),
                        physical_state=scanner.physical_state,
                        final_stop_reused=bool(scanner.checkpoint.get("final_stop_reused")),
                        result=session.result,
                        error=getattr(failure, "code", None),
                        trace=navigator.trace,
                        observation_timings=scanner.checkpoint.get("navigation_observation_timings", []),
                        elapsed_seconds=timings,
                    ),
                )
            if failure is not None:
                # Any failed physical operation discards pending motion, even
                # when its old UI error has been superseded by another click.
                session.pending = None
                raise failure

    async def _watch(self) -> None:
        while self.sessions:
            await asyncio.sleep(1)
            for identifier, session in list(self.sessions.items()):
                if time.monotonic() - session.seen > 15:
                    session.closed = True
                    session.pending = None
                if session.closed and (not session.task or session.task.done()):
                    await self._discard_preparation(session)
                    await self._drain_recognition(session)
                    self.sessions.pop(identifier, None)

    async def shutdown(self) -> None:
        for session in self.sessions.values():
            session.closed = True
            session.pending = None
        tasks = [session.task for session in self.sessions.values() if session.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in list(self.sessions.values()):
            await self._discard_preparation(session)
            await self._drain_recognition(session)
        if self.watchdog:
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)


def register_live_panorama_routes(app: Any, panorama: Any, sources: Any) -> LivePanoramaService:
    service = LivePanoramaService(panorama, sources)
    prefix = "/api/cameras/live-panorama"

    @app.get(prefix)
    async def catalog(request: Request):
        return await service.catalog(request)

    @app.post(prefix + "/sessions")
    async def open_session(request: Request, body: OpenSession):
        return await service.open(request, body)

    @app.get(prefix + "/sessions/{identifier}")
    async def status(request: Request, identifier: str):
        session = service.session(request, identifier)
        if session.task and not session.task.done():
            try:
                await service.current(session)
            except HTTPException:
                session.sequence += 1
                session.pending, session.result = None, None
                session.blocked = True
                session.error = "source_panorama_changed"
                session.phase = "stopping"
        return session.public()

    @app.post(prefix + "/sessions/{identifier}/observe")
    async def observe(request: Request, identifier: str, body: Observation):
        return await service.observe(service.session(request, identifier), body)

    @app.post(prefix + "/sessions/{identifier}/intent")
    async def intent(request: Request, identifier: str, body: Intention):
        return service.intend(service.session(request, identifier, control=True), body)

    @app.post(prefix + "/sessions/{identifier}/stop")
    async def stop(request: Request, identifier: str, body: Stop):
        return service.stop(service.session(request, identifier, control=True), body)

    @app.delete(prefix + "/sessions/{identifier}")
    async def close(request: Request, identifier: str):
        session = service.session(request, identifier)
        session.closed, session.pending = True, None
        return session.public()

    return service
