"""Camera I/O for autonomous panoramas, without changing camera configuration.

Only the shared fenced controller moves the head. Decoder time is never
presented as media/exposure time, and visual evidence does not change the
controller's geometry_safe contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from typing import Any, Awaitable, Callable

import numpy as np

from .capture_service import CameraCaptureRequest
from .onvif.reolink_cgi import ReolinkCgiClient
from .ptz_controller import PtzControlError, PtzFailureCode, PtzFailureStage
from .settings import (
    camera_source_has_ptz,
    get_camera_device,
    get_camera_source,
    normalize_cameras_settings,
)


class PanoramaCaptureError(RuntimeError):
    """A public, credential-free failure code for the acquisition job."""

    def __init__(
        self,
        code: str,
        *,
        ptz_failure_code: PtzFailureCode | str | None = None,
        ptz_failure_stage: PtzFailureStage | str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.ptz_failure_code: str | None = None
        self.ptz_failure_stage: str | None = None
        if ptz_failure_code is not None:
            try:
                self.ptz_failure_code = PtzFailureCode(ptz_failure_code).value
            except (TypeError, ValueError):
                self.ptz_failure_code = PtzFailureCode.UNKNOWN.value
        if ptz_failure_stage is not None:
            try:
                self.ptz_failure_stage = PtzFailureStage(ptz_failure_stage).value
            except (TypeError, ValueError):
                self.ptz_failure_stage = PtzFailureStage.SERVICE_BOUNDARY.value


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


_MANAGED_RETURN_PRESET_NAME = re.compile(
    r"^(?:Pano [OW] [a-f0-9]{16}|Panorama [a-f0-9]{20})$"
)


def _configuration_signature(camera: dict[str, Any], source: dict[str, Any]) -> str:
    # Asset/crop metadata and labels deliberately do not affect transport identity.
    value = {
        "camera_enabled": camera.get("enabled"),
        "control": camera.get("control"),
        "onvif": camera.get("onvif"),
        "source_id": source.get("id"),
        "enabled": source.get("enabled"),
        "origin": source.get("origin"),
        "video": source.get("video"),
        "mount_revision": camera.get("metadata", {}).get("panorama_mount_revision"),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class PanoramaCamera:
    """One job's camera, capture lease and fenced control lease.

    ``resolve_onvif`` is the plugin's existing operation-context resolver. It
    accepts camera_id, camera_source_id, transport_context=None and returns
    (client, ptz_xaddr, profile_token, source_id, bound_context).
    ``capture_service`` is the existing CameraCaptureService instance, and
    ``dependencies`` supplies its normal ConfigStore and service registry.
    No request credential or authenticated URL is returned to the job manifest.
    """

    def __init__(
        self,
        *,
        camera_id: str,
        source_id: str,
        owner_id: str,
        services: Any,
        capture_service: Any,
        dependencies: Any,
        resolve_onvif: Callable[..., Awaitable[Any]],
    ):
        self.camera_id, self.source_id, self.owner_id = camera_id, source_id, owner_id
        self.services, self.capture_service = services, capture_service
        self.dependencies, self.resolve_onvif = dependencies, resolve_onvif
        self.lease: dict[str, Any] | None = None
        self._capture_lease: Any = None
        self._client: Any = None
        self._ptz_xaddr = ""
        self._media_xaddr = ""
        self._profile_token = ""
        self._signature = ""
        self._camera: dict[str, Any] = {}
        self._source: dict[str, Any] = {}
        self._capabilities: dict[str, Any] | None = None
        self._device_information: dict[str, Any] = {}
        self._reolink: ReolinkCgiClient | None = None
        self._renewal: asyncio.Task[Any] | None = None
        self._control_error: str | None = None
        self._last_frame_key: tuple[str, int, int] | None = None
        self._direct_attempted = False
        self._transport_fallback_reason: str | None = None
        self._dimensions: tuple[int, int] | None = None
        self._temporary_presets: set[str] = set()
        self._return_destinations: dict[str, dict[str, Any]] = {}
        self._return_creations: dict[str, dict[str, Any]] = {}
        self._return_may_change_zoom = False
        self._automation: dict[str, bool | None] = {"auto_tracking": None, "automatic_return": None}

    async def _configuration(self) -> None:
        settings = await self.dependencies.config_store.get_settings()
        configuration = normalize_cameras_settings(
            settings.extensions.get("com.toposync.cameras", {})
        )
        camera = get_camera_device(configuration, camera_id=self.camera_id)
        if camera is None or not camera.get("enabled", True):
            raise PanoramaCaptureError("camera_unavailable")
        source = get_camera_source(
            camera, source_id=self.source_id, kind="video", enabled_only=True
        )
        if source is None or not camera_source_has_ptz(source):
            raise PanoramaCaptureError("source_binding_unverified")
        signature = _configuration_signature(camera, source)
        if self._signature and self._signature != signature:
            raise PanoramaCaptureError("camera_configuration_changed")
        self._signature, self._camera, self._source = signature, camera, source

    async def discover(self) -> dict[str, Any]:
        """Read capabilities and the exact optical profile; never acquire control."""
        await self._configuration()
        if self._capabilities is not None:
            return self._capabilities
        try:
            client, endpoint, token, source_id, _ = await self.resolve_onvif(
                camera_id=self.camera_id,
                camera_source_id=self.source_id,
                transport_context=None,
            )
            if source_id != self.source_id:
                raise PanoramaCaptureError("source_binding_unverified")
            self._client, self._ptz_xaddr, self._profile_token = client, endpoint, token
            media, _ = await client.get_capabilities()
            self._media_xaddr = str(media or "")
            profiles = await client.get_profiles(self._media_xaddr)
            matches = [profile for profile in profiles if profile.token == token and profile.has_ptz]
            if len(matches) != 1:
                raise PanoramaCaptureError("source_binding_unverified")
            configured_token = self._source.get("origin", {}).get("profile_token")
            if configured_token and configured_token != token:
                raise PanoramaCaptureError("source_binding_unverified")
            profile = matches[0]
            try:
                information = await client.get_device_information()
                self._device_information = {
                    key: str(information.get(key) or "")
                    for key in ("manufacturer", "model", "firmware_version")
                }
            except Exception:
                self._device_information = {}
            manufacturer = self._device_information.get("manufacturer", "").lower()
            if "reolink" in manufacturer:
                self._reolink = ReolinkCgiClient(
                    device_xaddr=client.xaddr,
                    username=client.username,
                    password=client.password,
                    timeout_s=client.timeout_s,
                )
                try:
                    self._automation = await self._reolink.get_motion_automation()
                except Exception:
                    pass
            capabilities = await client.get_ptz_capabilities(
                endpoint,
                profile_token=token,
                configuration_token=getattr(profile, "ptz_configuration_token", ""),
            )
            capabilities = dict(capabilities)
            position = await self.position()
            capabilities.update(
                camera_id=self.camera_id,
                source_id=self.source_id,
                source_identity={
                    "camera_id": self.camera_id,
                    "source_id": self.source_id,
                    "profile_token": token,
                    "configuration_token": capabilities.get("configuration_token"),
                    "node_token": capabilities.get("node_token"),
                    "width": profile.width,
                    "height": profile.height,
                    "device": self._device_information,
                    "zoom": position["zoom"],
                    "zoom_state": "reported" if position["zoom"] is not None else "unreported",
                    "mount_revision": self._camera.get("metadata", {}).get("panorama_mount_revision"),
                },
                position=position,
                motion_automation=self._automation,
            )
            # The shared transport currently accepts normalized absolute axes.
            # Do not silently send coordinates in another advertised space.
            for mode in ("absolute", "continuous", "relative", "absolute_zoom"):
                spaces = capabilities.get("spaces", {}).get(mode, [])
                default = capabilities.get("defaults", {}).get(mode)
                capabilities[f"{mode}_supported"] = bool(
                    capabilities.get(mode) is True
                    and sum(bool(space.get("normalized") and space.get("uri") == default) for space in spaces) == 1
                )
            capabilities["absolute_zoom_supported"] = bool(
                capabilities["absolute_zoom_supported"] and capabilities.get("limits", {}).get("zoom")
            )
            timeout_bounds = capabilities.get("continuous_timeout_s")
            minimum_timeout = (
                _finite(timeout_bounds.get("min")) if isinstance(timeout_bounds, dict) else None
            )
            maximum_timeout = (
                _finite(timeout_bounds.get("max")) if isinstance(timeout_bounds, dict) else None
            )
            capabilities["velocity_supported"] = bool(
                capabilities["continuous_supported"]
                and minimum_timeout is not None
                and maximum_timeout is not None
                and maximum_timeout > 0
                and 0 <= minimum_timeout <= min(maximum_timeout, 2.0)
            )
            mode = next(
                (
                    mode
                    for mode, supported in (
                        ("continuous", capabilities["velocity_supported"]),
                        ("relative", capabilities["relative_supported"]),
                        ("absolute", capabilities["absolute_supported"]),
                    )
                    if supported
                ),
                None,
            )
            selected = next((space for space in capabilities.get("spaces", {}).get(mode, [])
                             if space.get("uri") == capabilities.get("defaults", {}).get(mode)), {})
            capabilities["axes"] = {
                axis: bounds.get("min") != bounds.get("max") if bounds else None
                for axis, bounds in (("pan", selected.get("x")), ("tilt", selected.get("y")))
            }
            self._capabilities = capabilities
            return capabilities
        except PanoramaCaptureError:
            raise
        except Exception:
            raise PanoramaCaptureError("camera_discovery_failed") from None

    async def acquire(self) -> dict[str, Any]:
        await self.discover()
        if any(value is True for value in self._automation.values()):
            raise PanoramaCaptureError("external_automation_active")
        if self.lease is not None:
            await self._guard()
            return self.lease
        try:
            lease = await self.services.call(
                "cameras.control.acquire",
                camera_id=self.camera_id,
                camera_source_id=self.source_id,
                owner_kind="manual",
                owner_id=f"panorama:{self.owner_id}",
                ttl_s=15.0,
            )
            if not isinstance(lease, dict) or not lease.get("lease_id") or not lease.get("fence"):
                raise PanoramaCaptureError("control_unavailable")
            self.lease = lease
            self._control_error = None
            self._renewal = asyncio.create_task(self._keep_control(), name=f"panorama-lease:{self.owner_id}")
            return lease
        except PanoramaCaptureError:
            raise
        except Exception:
            raise PanoramaCaptureError("control_unavailable") from None

    async def _keep_control(self) -> None:
        try:
            while self.lease is not None:
                await asyncio.sleep(4.0)
                await self.renew()
        except asyncio.CancelledError:
            return
        except Exception:
            self._control_error = "control_lost"

    async def renew(self) -> None:
        if not self.lease:
            raise PanoramaCaptureError("control_unavailable")
        for attempt in range(3):
            try:
                await self.services.call(
                    "cameras.control.renew",
                    lease_id=self.lease["lease_id"], fence=self.lease["fence"], ttl_s=15.0,
                )
                return
            except Exception:
                # A finite pulse's automatic Stop temporarily blocks renewal.
                # Retry only after independently checking the same lease/fence;
                # an expired or replacement owner is never reacquired here.
                try:
                    snapshot = await self._guard(stopping=True)
                except Exception:
                    break
                if (
                    attempt == 2 or snapshot.get("state") == "fault"
                    or snapshot.get("transport_binding_current") is False
                ):
                    break
                await asyncio.sleep(0.1)
        self._control_error = "control_lost"
        raise PanoramaCaptureError("control_lost") from None

    async def _guard(self, *, stopping: bool = False) -> dict[str, Any]:
        if not self.lease or (self._control_error and not stopping):
            raise PanoramaCaptureError(self._control_error or "control_unavailable")
        if not stopping:
            await self._configuration()
        try:
            snapshot = await self.services.call(
                "cameras.control.snapshot", camera_id=self.camera_id,
                source_id=self.source_id, refresh_physical=False, include_readiness=False,
            )
        except Exception:
            raise PanoramaCaptureError("control_unavailable") from None
        active = snapshot.get("active_lease") or {}
        if any(active.get(key) != self.lease[key] for key in ("lease_id", "fence")):
            self._control_error = "control_lost"
            raise PanoramaCaptureError("control_lost")
        if not stopping and snapshot.get("transport_binding_current") is False:
            raise PanoramaCaptureError("camera_configuration_changed")
        return snapshot

    async def position(self) -> dict[str, Any]:
        if self._client is None:
            await self.discover()
        if self.lease is not None:
            await self._guard()
        try:
            status = await self._client.get_ptz_status(
                self._ptz_xaddr, profile_token=self._profile_token
            )
            error = str(status.error or "").strip()
            maker = self._device_information.get("manufacturer", "").lower()
            model = self._device_information.get("model", "").upper()
            # Verified Tapo C530WS responses use the literal zero as no error.
            if (
                error == "0" and model in {"C530WS", "TAPO C530WS"}
                and ("tp-link" in maker or "tapo" in maker)
            ):
                error = ""
            result = {
                "pan": _finite(status.pan), "tilt": _finite(status.tilt),
                "zoom": _finite(status.zoom), "native_pan": None, "native_tilt": None,
                "pan_tilt_space": str(getattr(status, "pan_tilt_space", "") or ""),
                "zoom_space": str(getattr(status, "zoom_space", "") or ""),
                "move_status": str(status.move_status or "UNKNOWN").upper(),
                "error": error, "observed_monotonic": time.monotonic(),
            }
            result["position_provenance"] = {
                "pan_tilt": {"source": "ONVIF.GetStatus", "space": result["pan_tilt_space"] or None},
                "zoom": {"source": "ONVIF.GetStatus", "space": result["zoom_space"] or None},
                "native": None,
            }
            if self._reolink is not None:
                try:
                    native = await self._reolink.get_current_position()
                    result.update(native_pan=native.pan, native_tilt=native.tilt)
                    result["position_provenance"]["native"] = {
                        "source": "Reolink.GetPtzCurPos", "pan_field": "Ppos", "tilt_field": "Tpos",
                        "units": "device_native", "degrees_conversion_verified": False,
                        "observed_monotonic": time.monotonic(),
                    }
                except Exception:
                    result["native_position_unavailable"] = True
            return result
        except Exception:
            raise PanoramaCaptureError("position_unavailable") from None

    async def _submit(self, command: dict[str, Any]) -> dict[str, Any]:
        stopping = command.get("kind") == "stop"
        await self._guard(stopping=stopping)
        if not stopping:
            await self.renew()
        lease = dict(self.lease or {})
        command_id = f"panorama_{uuid.uuid4().hex}"
        try:
            receipt = await self.services.call(
                "cameras.control.submit", lease_id=lease["lease_id"], fence=lease["fence"],
                command_id=command_id, command=command,
            )
        except PtzControlError as error:
            raise PanoramaCaptureError(
                "movement_unconfirmed",
                ptz_failure_code=error.failure_code,
                ptz_failure_stage=error.failure_stage,
            ) from None
        except Exception:
            raise PanoramaCaptureError(
                "movement_unconfirmed",
                ptz_failure_code=PtzFailureCode.UNKNOWN,
                ptz_failure_stage=PtzFailureStage.SERVICE_BOUNDARY,
            ) from None
        if not isinstance(receipt, dict) or not (
            receipt.get("accepted") is True and receipt.get("stale_after_execution") is False
            and receipt.get("lease_id") == lease["lease_id"]
            and receipt.get("fence") == lease["fence"] and receipt.get("command_id") == command_id
        ):
            raise PanoramaCaptureError(
                "movement_unconfirmed",
                ptz_failure_code=PtzFailureCode.UNKNOWN,
                ptz_failure_stage=PtzFailureStage.RECEIPT_VALIDATION,
            )
        return receipt

    async def move_absolute(
        self, *, pan: float, tilt: float, zoom: float | None = None
    ) -> dict[str, Any]:
        if not self._capabilities or not self._capabilities.get("absolute_supported"):
            raise PanoramaCaptureError("absolute_position_unavailable")
        for value, name in ((pan, "pan"), (tilt, "tilt")):
            limit = self._capabilities.get("limits", {}).get(name)
            if _finite(value) is None or not -1 <= value <= 1:
                raise PanoramaCaptureError("outside_camera_limits")
            if limit and not limit["min"] <= value <= limit["max"]:
                raise PanoramaCaptureError("outside_camera_limits")
        command = {"kind": "absolute_move", "pan": pan, "tilt": tilt}
        if zoom is not None:
            if not self._capabilities.get("absolute_zoom_supported"):
                raise PanoramaCaptureError("return_optical_state_mismatch")
            limit = self._capabilities.get("limits", {}).get("zoom")
            if _finite(zoom) is None or not 0 <= zoom <= 1:
                raise PanoramaCaptureError("outside_camera_limits")
            if not limit or not limit["min"] <= zoom <= limit["max"]:
                raise PanoramaCaptureError("outside_camera_limits")
            command["zoom"] = zoom
            self._return_may_change_zoom = True
        return await self._submit(command)

    async def move_velocity(
        self, *, pan: float = 0.0, tilt: float = 0.0, timeout_s: float = 0.5
    ) -> dict[str, Any]:
        if not self._capabilities or not self._capabilities.get("velocity_supported"):
            raise PanoramaCaptureError("normalized_motion_unavailable")
        if any(_finite(value) is None or abs(value) > 1 for value in (pan, tilt)):
            raise PanoramaCaptureError("invalid_velocity")
        if _finite(timeout_s) is None or not 0.05 <= timeout_s <= 2.0:
            raise PanoramaCaptureError("invalid_movement_duration")
        pan, tilt = self._motion_coordinates("continuous", pan, tilt)
        return await self._submit(
            {"kind": "continuous_move", "pan": pan, "tilt": tilt, "zoom": 0.0,
             "timeout_s": timeout_s, "allow_relative_fallback": False}
        )

    async def move_relative(self, *, pan: float = 0.0, tilt: float = 0.0) -> dict:
        if not self._capabilities or not self._capabilities.get("relative_supported"):
            raise PanoramaCaptureError("normalized_motion_unavailable")
        if any(_finite(value) is None or abs(value) > 1 for value in (pan, tilt)):
            raise PanoramaCaptureError("invalid_relative_movement")
        pan, tilt = self._motion_coordinates("relative", pan, tilt)
        return await self._submit({"kind": "relative_move", "pan": pan, "tilt": tilt, "zoom": 0.0})

    def _motion_coordinates(self, mode: str, pan: float, tilt: float) -> tuple[float, float]:
        capabilities = self._capabilities or {}
        selected = next((space for space in capabilities.get("spaces", {}).get(mode, [])
                         if space.get("uri") == capabilities.get("defaults", {}).get(mode)), {})
        values = []
        for axis, requested in (("x", pan), ("y", tilt)):
            bounds = selected.get(axis)
            value = max(bounds["min"], min(bounds["max"], requested)) if bounds else requested
            if (requested == 0 and value != 0) or (requested != 0 and value * requested <= 0):
                raise PanoramaCaptureError("axis_movement_unavailable")
            values.append(value)
        return values[0], values[1]

    async def stop(self) -> dict[str, Any]:
        # The controller's finite-pulse watchdog may already be stopping the
        # same head. Its transient state must not turn a redundant Stop into
        # a permanent failure. Every retry still verifies lease and fence.
        include_zoom = self._return_may_change_zoom
        for attempt in range(3):
            try:
                result = await self._submit(
                    {"kind": "stop", "pan_tilt": True, "zoom": include_zoom}
                )
                self._return_may_change_zoom = False
                return result
            except PanoramaCaptureError as error:
                if error.code != "movement_unconfirmed" or attempt == 2:
                    raise
                # A retry may be recovering a transient controller fault. Only
                # a Stop covering every PTZ axis may clear that state.
                include_zoom = True
                await asyncio.sleep(0.1)
        raise PanoramaCaptureError("movement_unconfirmed")

    async def _open_frames(self, *, direct: bool = False) -> None:
        await self.discover()
        if self._capture_lease is not None:
            await self.capture_service.release(self._capture_lease.lease_id)
            self._capture_lease = None
        parameters: dict[str, Any] = {}
        if direct:
            self._direct_attempted = True
            # A configured RTSP relay can retain an explicit ONVIF profile
            # binding. Require that exact binding, independently of transport;
            # discovery already verified its unique profile and source identity.
            configured_token = self._source.get("origin", {}).get("profile_token")
            if not configured_token or configured_token != self._profile_token:
                raise PanoramaCaptureError("direct_source_binding_unverified")
            try:
                uri = await self._client.get_stream_uri(
                    self._media_xaddr, profile_token=self._profile_token
                )
            except Exception:
                raise PanoramaCaptureError("direct_source_unavailable") from None
            # A configured relay may have different credentials. This URI came
            # from the camera's authenticated ONVIF session, not that relay.
            parameters.update(
                rtsp_url=uri, username=self._client.username, password=self._client.password,
            )
        request = CameraCaptureRequest(
            owner_id=f"panorama:{self.owner_id}", camera_id=self.camera_id,
            source_id=self.source_id, fps=12.0, **parameters,
        )
        try:
            self._capture_lease = await self.capture_service.open(request, self.dependencies)
        except Exception:
            raise PanoramaCaptureError("video_unavailable") from None
        self._last_frame_key = None

    async def frame(self, *, timeout_s: float = 3.0) -> dict[str, Any]:
        """Return a distinct atomic decoded sample, without fabricating PTS."""
        if _finite(timeout_s) is None or not 0 < timeout_s <= 30:
            raise PanoramaCaptureError("invalid_frame_timeout")
        await self._configuration()
        if self._control_error:
            raise PanoramaCaptureError(self._control_error)
        if self._capture_lease is None:
            try:
                await self._open_frames()
            except PanoramaCaptureError as error:
                if self._direct_attempted:
                    raise
                self._transport_fallback_reason = error.code
                await self._open_frames(direct=True)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._control_error:
                raise PanoramaCaptureError(self._control_error)
            sample = self._capture_lease.grabber.get_latest_sample()
            key = (sample.capture_instance, sample.generation, sample.sequence)
            if sample.frame is not None and sample.sequence > 0 and key != self._last_frame_key:
                image = np.asarray(sample.frame)
                if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
                    raise PanoramaCaptureError("invalid_frame")
                height, width = image.shape[:2]
                identity = (self._capabilities or {}).get("source_identity", {})
                expected = (identity.get("width"), identity.get("height"))
                if all(expected) and expected != (width, height):
                    raise PanoramaCaptureError("source_geometry_changed")
                if self._dimensions and self._dimensions != (width, height):
                    raise PanoramaCaptureError("source_geometry_changed")
                self._dimensions, self._last_frame_key = (width, height), key
                return {
                    "image": image.copy(),
                    "media_time": _finite(getattr(sample, "media_time", None)),
                    "received_monotonic": sample.source_received_monotonic,
                    "published_at": sample.published_at,
                    "capture_instance": sample.capture_instance,
                    "sequence": sample.sequence, "generation": sample.generation,
                    "physical_timestamp_verified": bool(sample.physical_capture_verified),
                    "captured_monotonic": sample.captured_monotonic or None,
                    "width": width, "height": height,
                    "transport": "direct" if self._direct_attempted else "configured",
                    **(
                        {"transport_fallback_reason": self._transport_fallback_reason}
                        if self._direct_attempted and self._transport_fallback_reason
                        else {}
                    ),
                    "capture_evidence": sample.evidence(),
                }
            await asyncio.sleep(0.025)
        if not self._direct_attempted:
            self._transport_fallback_reason = "configured_fresh_frame_unavailable"
            await self._open_frames(direct=True)
            return await self.frame(timeout_s=timeout_s)
        raise PanoramaCaptureError("fresh_frame_unavailable")

    def _return_binding(self) -> dict[str, Any]:
        capabilities = self._capabilities or {}
        identity = capabilities.get("source_identity", {})
        return {
            "configuration_signature": self._signature,
            "camera_id": self.camera_id, "source_id": self.source_id,
            "profile_token": self._profile_token,
            "configuration_token": capabilities.get("configuration_token"),
            "node_token": capabilities.get("node_token"),
            "width": identity.get("width"), "height": identity.get("height"),
            "mount_revision": identity.get("mount_revision"),
        }

    async def _verify_return_binding(self, saved: dict[str, Any]) -> None:
        await self._configuration()
        binding = saved.get("binding")
        if not binding and "role" not in saved:
            return
        if not isinstance(binding, dict):
            raise PanoramaCaptureError("return_binding_unverified")
        if binding != self._return_binding():
            raise PanoramaCaptureError("return_binding_changed")

    @staticmethod
    def _return_error(error: Exception, default: str) -> PanoramaCaptureError:
        # Service errors can contain authenticated URLs. Expose only known codes.
        if isinstance(error, PanoramaCaptureError):
            return error
        status = getattr(error, "status_code", None)
        detail = str(getattr(error, "detail", error)).lower()
        if status in {401, 403} or any(value in detail for value in ("notauthorized", "not authorized", "unauthorized", "failedauthentication")):
            return PanoramaCaptureError("return_authorization_failed")
        if any(value in detail for value in (
            "toomanypresets", "presetfull", "preset limit",
        )):
            return PanoramaCaptureError("return_capacity_unavailable")
        if status in {405, 501} or any(value in detail for value in (
            "actionnotsupported", "not supported", "notsupported", "not implemented",
            "no presets", "nopresets",
        )):
            return PanoramaCaptureError("return_unavailable")
        return PanoramaCaptureError(default)

    async def _preset_inventory(self) -> list[dict[str, Any]]:
        try:
            inventory = await self.services.call(
                "cameras.ptz.list_presets", camera_id=self.camera_id,
                camera_source_id=self.source_id,
            )
        except Exception as error:
            raise self._return_error(error, "return_preset_unverified") from None
        if not isinstance(inventory, list) or any(
            not isinstance(item, dict) or not str(item.get("token") or "") for item in inventory
        ):
            raise PanoramaCaptureError("return_preset_unverified")
        tokens = [str(item["token"]) for item in inventory]
        if len(tokens) != len(set(tokens)):
            raise PanoramaCaptureError("return_preset_ambiguous")
        return inventory

    async def remove_managed_returns(self) -> int:
        """Remove stale return presets from Toposync's reserved namespace only."""
        try:
            inventory = await self._preset_inventory()
        except PanoramaCaptureError as error:
            if error.code == "return_unavailable":
                return 0
            raise
        managed = [
            (str(item["token"]), str(item.get("name") or ""))
            for item in inventory
            if _MANAGED_RETURN_PRESET_NAME.fullmatch(str(item.get("name") or ""))
        ]
        for token, name in managed:
            try:
                await self.services.call(
                    "cameras.ptz.remove_preset",
                    camera_id=self.camera_id,
                    camera_source_id=self.source_id,
                    preset_token=token,
                    expected_preset_name=name,
                )
            except Exception as error:
                failure = self._return_error(error, "return_cleanup_unconfirmed")
                remaining = await self._preset_inventory()
                if any(str(item["token"]) == token for item in remaining):
                    raise failure
        remaining = await self._preset_inventory()
        if any(
            _MANAGED_RETURN_PRESET_NAME.fullmatch(str(item.get("name") or ""))
            for item in remaining
        ):
            raise PanoramaCaptureError("return_cleanup_unconfirmed")
        return len(managed)

    async def save_return(
        self, role: str = "original", *,
        before_create: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Save one immutable destination per role; never retry an uncertain creation."""
        if role not in {"original", "work"}:
            raise PanoramaCaptureError("return_role_invalid")
        await self._guard()
        if role in self._return_destinations:
            saved = self._return_destinations[role]
            await self._verify_return_binding(saved)
            if saved["kind"] == "preset":
                await self._verify_return_preset(saved)
            return dict(saved)
        binding = self._return_binding()
        if role not in self._return_creations:
            try:
                pose = await self.position()
            except PanoramaCaptureError as error:
                if error.code != "position_unavailable":
                    raise
                pose = {"pan": None, "tilt": None, "zoom": None}
            if (
                self._capabilities and self._capabilities.get("absolute_supported")
                and pose.get("pan_tilt_space", "") in {"", self._capabilities.get("defaults", {}).get("absolute")}
                and all(_finite(pose.get(axis)) is not None and -1 <= pose[axis] <= 1 for axis in ("pan", "tilt"))
                and all(not self._capabilities.get("limits", {}).get(axis) or
                        self._capabilities["limits"][axis]["min"] <= pose[axis] <= self._capabilities["limits"][axis]["max"]
                        for axis in ("pan", "tilt"))
            ):
                saved = {
                    "kind": "absolute", "role": role, "binding": binding,
                    "pan": pose["pan"], "tilt": pose["tilt"], "zoom": pose["zoom"],
                    "space": self._capabilities.get("defaults", {}).get("absolute"),
                    "zoom_space": pose.get("zoom_space") or self._capabilities.get("defaults", {}).get("absolute_zoom"),
                }
                self._return_destinations[role] = saved
                return dict(saved)
            inventory = await self._preset_inventory()
            name = self._return_name(role)
            if any(item.get("name") == name for item in inventory):
                raise PanoramaCaptureError("return_owner_mismatch")
            maximum = (self._capabilities or {}).get("presets", {}).get("maximum_count")
            reserve_original = int(role == "work" and "original" not in self._return_destinations)
            if isinstance(maximum, int) and not isinstance(maximum, bool) and len(inventory) + reserve_original >= maximum:
                raise PanoramaCaptureError("return_capacity_unavailable")
            if len(self._temporary_presets) >= 2:
                raise PanoramaCaptureError("return_capacity_unavailable")
            pending = {
                "kind": "pending_preset", "role": role, "binding": binding,
                "preset_name": name, "owner_id": self.owner_id,
                "baseline_tokens": [str(item["token"]) for item in inventory],
                "zoom": pose["zoom"],
            }
            self._return_creations[role] = pending
            if before_create is not None:
                await before_create(dict(pending))
            await self._guard()
            try:
                result = await self.services.call(
                    "cameras.ptz.set_preset", camera_id=self.camera_id,
                    camera_source_id=self.source_id, preset_name=name,
                    idempotency_key=f"panorama-return:{self.owner_id}:{role}",
                )
                token = str(result.get("token") or "") if isinstance(result, dict) else ""
                if token in pending["baseline_tokens"]:
                    raise PanoramaCaptureError("return_preset_changed")
                pending["resolved_token"] = token
            except Exception as error:
                pending["error_code"] = self._return_error(error, "return_creation_unconfirmed").code
        pending = self._return_creations[role]
        await self._verify_return_binding(pending)
        if pending.get("error_code") in {"return_preset_changed", "return_authorization_failed"}:
            raise PanoramaCaptureError(pending["error_code"])
        inventory = await self._preset_inventory()
        candidates = [item for item in inventory if item.get("name") == pending["preset_name"]]
        if len(candidates) > 1:
            raise PanoramaCaptureError("return_preset_ambiguous")
        if not candidates:
            raise PanoramaCaptureError(pending.get("error_code", "return_creation_unconfirmed"))
        token = str(candidates[0]["token"])
        if token in pending["baseline_tokens"] or token in self._temporary_presets:
            raise PanoramaCaptureError("return_preset_changed")
        if pending.get("resolved_token") and token != pending["resolved_token"]:
            raise PanoramaCaptureError("return_preset_changed")
        saved = {key: pending[key] for key in ("role", "binding", "preset_name", "owner_id", "zoom")}
        saved.update(kind="preset", preset_token=token)
        pending["resolved_token"] = token
        await self._guard()
        # No await may split ownership removal from delivery to the caller.
        self._temporary_presets.add(token)
        self._return_destinations[role] = saved
        del self._return_creations[role]
        return dict(saved)

    def pending_return_destinations(self) -> list[dict[str, Any]]:
        """Persist these ownership intents if a creation cannot be reconciled."""
        return [dict(value) for value in self._return_creations.values()]

    async def return_to(self, saved: dict[str, Any]) -> dict[str, Any]:
        await self._verify_return_binding(saved)
        if saved.get("role", "original") not in {"original", "work"}:
            raise PanoramaCaptureError("return_role_invalid")
        if saved.get("kind") == "absolute":
            if "space" in saved and saved["space"] != (self._capabilities or {}).get("defaults", {}).get("absolute"):
                raise PanoramaCaptureError("return_binding_changed")
            zoom = _finite(saved.get("zoom"))
            commanded_zoom = None
            if zoom is not None:
                position = await self.position()
                zoom_space = (self._capabilities or {}).get("defaults", {}).get("absolute_zoom")
                saved_space = saved.get("zoom_space") or zoom_space
                current_space = position.get("zoom_space") or zoom_space
                unchanged = (
                    saved_space == current_space and position.get("zoom") is not None
                    and abs(position["zoom"] - zoom) <= 1e-3
                )
                if not unchanged:
                    if (
                        not (self._capabilities or {}).get("absolute_zoom_supported")
                        or saved_space != zoom_space or current_space != zoom_space
                    ):
                        raise PanoramaCaptureError("return_optical_state_mismatch")
                    commanded_zoom = zoom
            return await self.move_absolute(pan=saved["pan"], tilt=saved["tilt"], zoom=commanded_zoom)
        if saved.get("kind") == "preset":
            await self._verify_return_preset(saved)
            self._return_may_change_zoom = True
            return await self._submit({"kind": "goto_preset", "preset_token": saved["preset_token"]})
        raise PanoramaCaptureError("return_unavailable")

    def _return_name(self, role: str = "original") -> str:
        # Role comes first so device truncation cannot turn work into original.
        digest = hashlib.sha256(self.owner_id.encode()).hexdigest()[:16]
        return f"Pano {'O' if role == 'original' else 'W'} {digest}"

    def _verify_return_owner(self, saved: dict[str, Any]) -> None:
        role = saved.get("role", "original")
        names = {self._return_name(role)}
        if role == "original":
            names.add(f"Panorama {self.owner_id[:20]}")
        if role not in {"original", "work"} or saved.get("owner_id") != self.owner_id or saved.get("preset_name") not in names:
            raise PanoramaCaptureError("return_owner_mismatch")

    async def _verify_return_preset(self, saved: dict[str, Any], *, allow_absent: bool = False) -> bool:
        self._verify_return_owner(saved)
        await self._verify_return_binding(saved)
        inventory = await self._preset_inventory()
        matches = [item for item in inventory if item.get("token") == saved.get("preset_token")]
        if not matches and allow_absent:
            return False
        if len(matches) != 1 or matches[0].get("name") != saved["preset_name"]:
            raise PanoramaCaptureError("return_preset_changed")
        if sum(item.get("name") == saved["preset_name"] for item in inventory) != 1:
            raise PanoramaCaptureError("return_preset_ambiguous")
        return True

    def _forget_return_preset(self, saved: dict[str, Any]) -> None:
        token = saved.get("preset_token") or saved.get("resolved_token")
        self._temporary_presets.discard(token)
        role = saved.get("role", "original")
        pending = self._return_creations.get(role)
        if pending and all(pending.get(key) == saved.get(key) for key in ("owner_id", "preset_name", "binding")):
            if not pending.get("resolved_token") or pending["resolved_token"] == token:
                del self._return_creations[role]

    async def remove_return(self, saved: dict[str, Any]) -> None:
        if saved.get("kind") == "pending_preset":
            self._verify_return_owner(saved)
            if "role" not in saved:
                raise PanoramaCaptureError("return_binding_unverified")
            await self._verify_return_binding(saved)
            inventory = await self._preset_inventory()
            candidates = [item for item in inventory if item.get("name") == saved.get("preset_name")]
            resolved = str(saved.get("resolved_token") or "")
            absent = resolved and not any(item["token"] == resolved for item in inventory)
            if not candidates and (not resolved or absent):
                self._forget_return_preset(saved)
                return
            elif (
                len(candidates) != 1
                or candidates[0]["token"] in saved.get("baseline_tokens", [])
                or (resolved and candidates[0]["token"] != resolved)
            ):
                raise PanoramaCaptureError("return_cleanup_unconfirmed")
            else:
                saved = {**saved, "kind": "preset", "preset_token": candidates[0]["token"]}
        token = str(saved.get("preset_token") or "")
        if saved.get("kind") != "preset" or not token:
            return
        if saved.get("owner_id") != self.owner_id:
            raise PanoramaCaptureError("return_owner_mismatch")
        if not await self._verify_return_preset(saved, allow_absent=True):
            self._forget_return_preset(saved)
            return
        try:
            await self.services.call(
                "cameras.ptz.remove_preset", camera_id=self.camera_id,
                camera_source_id=self.source_id, preset_token=token,
                expected_preset_name=saved["preset_name"],
            )
        except Exception as error:
            failure = self._return_error(error, "return_cleanup_unconfirmed")
            if await self._verify_return_preset(saved, allow_absent=True):
                raise failure from None
        if await self._verify_return_preset(saved, allow_absent=True):
            raise PanoramaCaptureError("return_cleanup_unconfirmed")
        self._forget_return_preset(saved)

    async def release(self) -> None:
        """Release resources only; callers choose Stop/return based on job outcome."""
        renewal, self._renewal = self._renewal, None
        if renewal is not None:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        lease, self.lease = self.lease, None
        try:
            if lease:
                await self.services.call(
                    "cameras.control.release", lease_id=lease["lease_id"], fence=lease["fence"],
                )
        except Exception:
            # A replacement owner must never be stopped to clean up our lease.
            pass
        finally:
            if self._capture_lease is not None:
                capture, self._capture_lease = self._capture_lease, None
                await self.capture_service.release(capture.lease_id)

    async def close(self) -> None:
        await self.release()
