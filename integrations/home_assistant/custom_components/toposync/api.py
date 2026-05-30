from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientResponseError, ClientSession


class ToposyncApiError(RuntimeError):
    """Raised when Toposync API calls fail."""


class ToposyncClient:
    def __init__(self, session: ClientSession, *, url: str, token: str = "") -> None:
        self._session = session
        self._base_url = str(url or "").strip().rstrip("/") + "/"
        self._token = str(token or "").strip()

    @property
    def base_url(self) -> str:
        return self._base_url.rstrip("/")

    def url_for(self, path_or_url: str) -> str:
        raw = str(path_or_url or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return urljoin(self._base_url, raw.lstrip("/"))

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        return headers

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {**self._headers(), **dict(kwargs.pop("headers", {}) or {})}
        try:
            async with self._session.request(method, self.url_for(path), headers=headers, **kwargs) as response:
                response.raise_for_status()
                return await response.json()
        except ClientResponseError as exc:
            raise ToposyncApiError(f"Toposync API returned HTTP {exc.status}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ToposyncApiError(f"Toposync API request failed: {exc}") from exc

    async def get_cameras_manifest(self) -> dict[str, Any]:
        data = await self._json("GET", "/api/streams/home-assistant/cameras")
        return data if isinstance(data, dict) else {"cameras": []}

    async def heartbeat(self, camera: dict[str, Any], *, ttl_seconds: float = 90.0) -> None:
        transmission_id = str(camera.get("transmission_id") or "").strip()
        if not transmission_id:
            return
        payload = {
            "playback_session_id": f"ha_entity:{str(camera.get('id') or transmission_id)}",
            "output_id": camera.get("output_id"),
            "quality_profile_id": camera.get("quality_profile_id"),
            "transport": "rtsp",
            "source": "home_assistant_entity",
            "ttl_seconds": ttl_seconds,
        }
        await self._json("POST", f"/api/streams/transmissions/{transmission_id}/demand/heartbeat", json=payload)

    async def get_still(self, camera: dict[str, Any]) -> bytes:
        still_url = str(camera.get("still_url") or "").strip()
        if not still_url:
            raise ToposyncApiError("Toposync camera has no still URL")
        headers = self._headers()
        try:
            async with self._session.get(self.url_for(still_url), headers=headers) as response:
                response.raise_for_status()
                return await response.read()
        except ClientResponseError as exc:
            raise ToposyncApiError(f"Toposync still returned HTTP {exc.status}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ToposyncApiError(f"Toposync still request failed: {exc}") from exc

    async def webrtc_offer(self, camera: dict[str, Any], offer_sdp: str) -> str:
        offer_url = str(camera.get("webrtc_offer_url") or "").strip()
        if not offer_url:
            raise ToposyncApiError("Toposync camera has no WebRTC offer URL")
        data = await self._json("POST", offer_url, json={"sdp": offer_sdp})
        answer = str(data.get("answer_sdp") or "").strip() if isinstance(data, dict) else ""
        if not answer:
            raise ToposyncApiError("Toposync WebRTC answer is empty")
        return answer
