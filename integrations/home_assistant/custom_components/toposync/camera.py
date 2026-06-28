from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature, WebRTCAnswer, WebRTCSendMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ToposyncApiError, ToposyncClient
from .const import CONF_ENABLE_NATIVE_WEBRTC, DATA_CLIENT, DATA_MANIFEST, DATA_MANIFEST_CACHE, DOMAIN
from .manifest import ToposyncManifestCache

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client: ToposyncClient = data[DATA_CLIENT]
    manifest: dict[str, Any] = data[DATA_MANIFEST]
    manifest_cache: ToposyncManifestCache = data[DATA_MANIFEST_CACHE]
    enable_native_webrtc = bool(entry.data.get(CONF_ENABLE_NATIVE_WEBRTC, False))
    entities: list[Camera] = []
    for item in manifest.get("cameras", []):
        if not isinstance(item, dict):
            continue
        if enable_native_webrtc and item.get("webrtc_offer_url"):
            entities.append(ToposyncCameraWebRTC(client, manifest_cache, item))
        else:
            entities.append(ToposyncCamera(client, manifest_cache, item))
    async_add_entities(entities)


class ToposyncCamera(Camera):
    _attr_should_poll = False

    def __init__(
        self,
        client: ToposyncClient,
        manifest_cache: ToposyncManifestCache,
        camera: dict[str, Any],
    ) -> None:
        super().__init__()
        self._client = client
        self._manifest_cache = manifest_cache
        self._camera = dict(camera)
        self._camera_id = str(camera.get("id") or "").strip()
        self._attr_name = str(camera.get("name") or camera.get("id") or "Toposync Camera")
        self._attr_unique_id = f"toposync_{str(camera.get('id') or '').replace(':', '_')}"
        has_stream = bool(camera.get("rtsp_url"))
        self._attr_is_streaming = has_stream
        self._attr_supported_features = CameraEntityFeature.STREAM if has_stream else CameraEntityFeature(0)

    async def _refresh_camera(self, *, force: bool = False) -> dict[str, Any] | None:
        camera = await self._manifest_cache.get_camera(self._camera_id, force=force)
        if camera is None:
            _LOGGER.warning("Toposync camera %s is missing from the refreshed manifest.", self._camera_id)
            self._attr_is_streaming = False
            self._attr_supported_features = CameraEntityFeature(0)
            return None
        self._camera = camera
        blocking_errors = [str(item) for item in camera.get("blocking_errors") or [] if str(item)]
        if blocking_errors:
            _LOGGER.warning(
                "Toposync camera %s is blocked: %s",
                self._camera_id,
                "; ".join(blocking_errors),
            )
            self._attr_is_streaming = False
            self._attr_supported_features = CameraEntityFeature(0)
            return None
        has_stream = bool(str(camera.get("rtsp_url") or "").strip())
        self._attr_is_streaming = has_stream
        self._attr_supported_features = CameraEntityFeature.STREAM if has_stream else CameraEntityFeature(0)
        return camera

    async def stream_source(self) -> str | None:
        camera = await self._refresh_camera()
        if camera is None:
            return None
        try:
            await self._client.heartbeat(camera)
        except ToposyncApiError as exc:
            _LOGGER.warning("Failed to renew Toposync heartbeat for camera %s: %s", self._camera_id, exc)
        rtsp_url = str(camera.get("rtsp_url") or "").strip()
        return rtsp_url or None

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        camera = await self._refresh_camera()
        if camera is None:
            return None
        try:
            return await self._client.get_still(camera)
        except ToposyncApiError as exc:
            _LOGGER.warning("Failed to fetch Toposync still for camera %s: %s", self._camera_id, exc)
            return None


class ToposyncCameraWebRTC(ToposyncCamera):
    async def async_handle_async_webrtc_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        camera = await self._refresh_camera()
        if camera is None:
            return
        await self._client.heartbeat(camera)
        answer_sdp = await self._client.webrtc_offer(camera, offer_sdp)
        send_message(WebRTCAnswer(answer_sdp))

    async def async_on_webrtc_candidate(self, session_id: str, candidate: Any) -> None:
        return None
