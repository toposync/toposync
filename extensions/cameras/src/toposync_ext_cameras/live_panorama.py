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
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from PIL import Image

from .panorama import _digest, _error
from .panorama_capture import PanoramaCaptureError
from .panorama_navigation import VisualNavigator
from .panorama_scan import _Scan, _Stopped
from .processing.panorama_mapping import _rotation_basis, panorama_pixel_to_ray
from .settings import iter_camera_devices, iter_camera_sources

MAXIMUM_LIVE_NAVIGATION_COMMANDS = 12


class OpenSession(BaseModel):
    camera_id: str
    source_id: str
    artifact_id: str
    revision: int = Field(ge=1)


class Intention(BaseModel):
    sequence: int = Field(strict=True, ge=1)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


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
    commands: int = 0
    response: dict = field(default_factory=dict)
    response_rotations: dict = field(default_factory=dict)
    result: dict | None = None
    observation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
            located = await asyncio.to_thread(session.localizer.locate, image, evidence, geometry)
            if (
                session.closed
                or intention_sequence != session.sequence
                or session.task
                and not session.task.done()
            ):
                return {"status": "unlocalized", "reason": "superseded_observation"}
            output = {k: v for k, v in located.items() if k != "capture_evidence"}
            output.update(
                observation_sequence=body.sequence,
                epoch=body.epoch,
                media_time=body.media_time,
                timing_basis="browser_presented_frame",
                estimator_ms=(time.monotonic() - started) * 1000,
            )
            if located.get("status") == "localized":
                session.last_registration = time.monotonic()
                output["geometry"] = dict(
                    lens=lens,
                    panorama_to_camera=_rotation_basis(located["rotation_matrix"]).T.tolist(),
                )
                if not session.blocked:
                    session.phase = "aligned"
            else:
                session.last_registration = 0
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
            while session.pending and not session.closed and not session.blocked:
                intention, expires = session.pending
                session.pending = None
                if time.monotonic() > expires:
                    session.error, session.phase = "intention_expired", "localizing"
                    break
                await self._operate(session, intention, expires)
        except Exception as error:
            session.pending = None
            session.phase = "error"
            session.error = getattr(error, "code", None) or (
                error.detail.get("code")
                if isinstance(error, HTTPException) and isinstance(error.detail, dict)
                else "live_control_failed"
            )
        finally:
            if self.camera_owners.get(session.camera_id) == session.id:
                self.camera_owners.pop(session.camera_id, None)
            session.last_registration = 0
            if session.phase in ("moving", "stopping", "stabilizing"):
                session.phase = "localizing"

    async def _operate(self, session: Session, intention: Intention, expires: float) -> None:
        async with self.panorama.reference_coordinator.hold(session.camera_id, session.source_id):
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
            camera = self.panorama.camera_factory(
                services=self.panorama.services,
                camera_id=session.camera_id,
                source_id=session.source_id,
                settings={},
                job_id=f"live-{session.id}",
                output_dir=directory,
            )

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
                maximum_commands=MAXIMUM_LIVE_NAVIGATION_COMMANDS,
            )
            navigator.response = session.response
            navigator.response_rotations = session.response_rotations
            failure = None
            try:
                scanner.capabilities = await camera.discover()
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
                scanner.acquired = True
                scanner._check()
                if not await scanner._stop():
                    raise PanoramaCaptureError("stop_unconfirmed")
                scanner.last_frame = await scanner._reference_window()
                scanner.last_pose = await camera.position()
                scanner.physical_state = "stopped"
                scanner._check()
                result = await navigator.aim(panorama_pixel_to_ray(intention.x, intention.y))
                if not cancelled():
                    session.result = {"sequence": intention.sequence, **result}
            except _Stopped:
                pass
            except Exception as error:
                failure = error
            finally:
                session.commands += navigator.commands
                try:
                    if scanner.acquired:
                        try:
                            await scanner._confirm_stop()
                        except Exception:
                            scanner.physical_state = "stop_unconfirmed"
                        if scanner.physical_state != "stopped":
                            session.blocked = True
                            session.result = None
                            session.pending = None
                            session.error = "stop_unconfirmed"
                            session.phase = "error"
                finally:
                    await camera.close()
                self.panorama._atomic(
                    directory / "live-result.json",
                    dict(
                        sequence=intention.sequence,
                        commands=navigator.commands,
                        total_commands=session.commands,
                        superseded=cancelled(),
                        physical_state=scanner.physical_state,
                        result=session.result,
                        error=getattr(failure, "code", None),
                        trace=navigator.trace,
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
                    self.sessions.pop(identifier, None)

    async def shutdown(self) -> None:
        for session in self.sessions.values():
            session.closed = True
            session.pending = None
        tasks = [session.task for session in self.sessions.values() if session.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
