from __future__ import annotations

from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature, WebRTCAnswer, WebRTCSendMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ToposyncClient
from .const import CONF_ENABLE_NATIVE_WEBRTC, DATA_CLIENT, DATA_MANIFEST, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client: ToposyncClient = data[DATA_CLIENT]
    manifest: dict[str, Any] = data[DATA_MANIFEST]
    enable_native_webrtc = bool(entry.data.get(CONF_ENABLE_NATIVE_WEBRTC, False))
    entities: list[Camera] = []
    for item in manifest.get("cameras", []):
        if not isinstance(item, dict):
            continue
        if enable_native_webrtc and item.get("webrtc_offer_url"):
            entities.append(ToposyncCameraWebRTC(client, item))
        else:
            entities.append(ToposyncCamera(client, item))
    async_add_entities(entities)


class ToposyncCamera(Camera):
    _attr_should_poll = False

    def __init__(self, client: ToposyncClient, camera: dict[str, Any]) -> None:
        super().__init__()
        self._client = client
        self._camera = dict(camera)
        self._attr_name = str(camera.get("name") or camera.get("id") or "Toposync Camera")
        self._attr_unique_id = f"toposync_{str(camera.get('id') or '').replace(':', '_')}"
        has_stream = bool(camera.get("rtsp_url"))
        self._attr_is_streaming = has_stream
        self._attr_supported_features = CameraEntityFeature.STREAM if has_stream else CameraEntityFeature(0)

    async def stream_source(self) -> str | None:
        await self._client.heartbeat(self._camera)
        rtsp_url = str(self._camera.get("rtsp_url") or "").strip()
        return rtsp_url or None

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return await self._client.get_still(self._camera)


class ToposyncCameraWebRTC(ToposyncCamera):
    async def async_handle_async_webrtc_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        await self._client.heartbeat(self._camera)
        answer_sdp = await self._client.webrtc_offer(self._camera, offer_sdp)
        send_message(WebRTCAnswer(answer_sdp))

    async def async_on_webrtc_candidate(self, session_id: str, candidate: Any) -> None:
        return None
