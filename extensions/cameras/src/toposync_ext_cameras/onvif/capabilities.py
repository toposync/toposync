"""Read-only device inventory; advertised support is not physical verification."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from urllib.parse import urlsplit

from .client import OnvifClient, OnvifError
from .events import OnvifCameraEventContext, OnvifEventsClient


def camera_service_address(device_address: str, service_address: str) -> str:
    device, service = urlsplit(device_address), urlsplit(service_address)
    if (service.scheme not in {"http", "https"} or not service.hostname
            or service.hostname.lower() != (device.hostname or "").lower()
            or service.username is not None or service.password is not None):
        raise OnvifError("Service address does not belong to the configured camera")
    return service_address


class OnvifCapabilitiesCatalog:
    def __init__(self, resolve_context, event_manager):
        self._resolve_context = resolve_context
        self._event_manager = event_manager
        self._cache = {}
        self._locks = {}

    async def inspect(self, camera_id: str, *, refresh: bool = False) -> dict:
        lock = self._locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            context = await self._resolve_context(camera_id)
            cached = self._cache.get(camera_id)
            if not refresh and cached and cached[0] == context and time.time() < cached[1]["valid_until"]:
                return cached[1]
            result = await self._inspect(context, refresh=refresh)
            self._cache[camera_id] = (context, result)
            return result

    async def matches_stream(self, camera_id: str, *, profile_token: str, rtsp_url: str, binding: dict | None = None) -> bool:
        context = await self._resolve_context(camera_id)
        client = OnvifClient(xaddr=context.xaddr, username=context.username,
                             password=context.password, timeout_s=context.timeout_s)
        media_address, _ = await client.get_capabilities()
        if not media_address:
            return False
        discovered = await client.get_stream_uri(camera_service_address(context.xaddr, media_address),
                                                 profile_token=profile_token)
        def identity(raw):
            parsed = urlsplit(raw)
            return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 554,
                    parsed.path, parsed.query)
        candidate = rtsp_url
        if (isinstance(binding, dict) and binding.get("profile_token") == profile_token
                and isinstance(binding.get("rtsp_url"), str)
                and isinstance(binding.get("upstream_rtsp_url"), str)
                and identity(binding["rtsp_url"]) == identity(rtsp_url)):
            candidate = binding["upstream_rtsp_url"]
        return identity(discovered) == identity(candidate)

    async def _inspect(self, context: OnvifCameraEventContext, *, refresh: bool) -> dict:
        client = OnvifClient(xaddr=context.xaddr, username=context.username,
                             password=context.password, timeout_s=context.timeout_s)
        now = time.time()
        result = {"camera_id": context.camera_id, "camera_name": context.camera_name,
                  "queried_at": now, "valid_until": now + 300,
                  "identification": {}, "services": [], "profiles": [], "events": [],
                  "sections": {}, "warnings": []}

        async def read(section, operation):
            try:
                value = await asyncio.wait_for(operation(), timeout=20)
                result["sections"][section] = "consulted"
                return value
            except Exception:
                result["sections"][section] = "unknown"
                result["warnings"].append(section)
                return None

        identification, services, addresses = await asyncio.gather(
            read("identification", client.get_device_information),
            read("services", OnvifEventsClient(client).get_services),
            read("media", client.get_capabilities),
        )
        result["identification"] = identification or {}
        result["services"] = [{"namespace": item.namespace, "status": "announced",
                               "integrated": item.namespace in {
                                   "http://www.onvif.org/ver10/media/wsdl",
                                   "http://www.onvif.org/ver20/ptz/wsdl",
                                   "http://www.onvif.org/ver10/events/wsdl"}}
                              for item in services or []]
        if addresses:
            media_address, ptz_address = addresses
            if media_address:
                profiles = await read("profiles", lambda: client.get_profiles(
                    camera_service_address(context.xaddr, media_address)))
                for profile in (profiles or [])[:32]:
                    row = asdict(profile)
                    row["ptz"] = None
                    if profile.has_ptz and ptz_address:
                        row["ptz"] = await read(f"ptz:{profile.token}", lambda: client.get_ptz_capabilities(
                            camera_service_address(context.xaddr, ptz_address),
                            profile_token=profile.token, configuration_token=profile.ptz_configuration_token))
                    result["profiles"].append(row)
        events = await read("events", lambda: self._event_manager.list_descriptors(
            context.camera_id, refresh=refresh))
        if events:
            result["events"] = events.get("events", [])[:256]
            result["observed_states"] = events.get("observed_states", [])[:256]
            result["observation"] = events.get("observation", "not_observed")
        else:
            result["observed_states"] = []
            result["observation"] = "unavailable"
        result["valid_until"] = time.time() + (30 if result["warnings"] else 300)
        result["status"] = "partial" if result["warnings"] else "consulted"
        return result
