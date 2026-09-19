"""Small, opt-in Reolink CGI adapter for PTZ presets and position telemetry.

The Cameras extension keeps ONVIF as its generic PTZ transport.  A few Reolink
firmwares expose their preset inventory and recall only through the local CGI
endpoint even while advertising ONVIF PTZ.  This module is deliberately scoped
to named preset slots and read-only telemetry; arbitrary motion remains ONVIF.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


REOLINK_PRESET_TOKEN_PREFIX = "reolink:"


class ReolinkCgiError(RuntimeError):
    """The local Reolink CGI endpoint could not confirm a PTZ operation."""


@dataclass(frozen=True, slots=True)
class ReolinkPtzPosition:
    """Native motor readings; absent axes are unknown, and units are not degrees."""

    pan: float | None = None
    tilt: float | None = None
    channel: int = 0


@dataclass(frozen=True, slots=True)
class ReolinkCgiPreset:
    slot: int
    name: str
    enabled: bool

    @property
    def token(self) -> str:
        return f"{REOLINK_PRESET_TOKEN_PREFIX}{self.slot}"


def parse_reolink_preset_token(value: str) -> int:
    token = str(value or "").strip()
    if not token.startswith(REOLINK_PRESET_TOKEN_PREFIX):
        raise ReolinkCgiError("Reolink preset token is required")
    raw_slot = token.removeprefix(REOLINK_PRESET_TOKEN_PREFIX)
    try:
        slot = int(raw_slot)
    except (TypeError, ValueError) as exc:
        raise ReolinkCgiError("Reolink preset slot is invalid") from exc
    if not 0 <= slot <= 63:
        raise ReolinkCgiError("Reolink preset slot is outside the supported range")
    return slot


def _cgi_base_url(device_xaddr: str) -> str:
    raw = str(device_xaddr or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception as exc:  # noqa: BLE001
        raise ReolinkCgiError("Reolink CGI device endpoint is invalid") from exc
    if str(parsed.scheme or "").lower() not in {"http", "https"} or not parsed.netloc:
        raise ReolinkCgiError("Reolink CGI device endpoint is unavailable")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _response_entry(payload: Any, command: str) -> dict[str, Any]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ReolinkCgiError(f"Reolink CGI {command} returned an invalid response")
    entry = payload[0]
    if str(entry.get("cmd") or "") != command:
        raise ReolinkCgiError(f"Reolink CGI {command} returned an unexpected response")
    try:
        code = int(entry.get("code"))
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        raise ReolinkCgiError(f"Reolink CGI {command} was rejected")
    return entry


def _post_json(*, url: str, payload: list[dict[str, Any]], timeout_s: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, float(timeout_s))) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        raise ReolinkCgiError("Reolink CGI request failed") from exc


@dataclass(slots=True)
class ReolinkCgiClient:
    device_xaddr: str
    username: str = ""
    password: str = ""
    timeout_s: float = 3.0

    async def get_current_position(self, *, channel: int = 0) -> ReolinkPtzPosition:
        if isinstance(channel, bool) or not isinstance(channel, int) or channel < 0:
            raise ReolinkCgiError("Reolink channel is invalid")

        async def _read(token: str) -> ReolinkPtzPosition:
            entry = await self._command(
                token,
                command="GetPtzCurPos",
                action=1,
                param={"PtzCurPos": {"channel": channel}},
            )
            value = (entry.get("value") or {}).get("PtzCurPos")
            if not isinstance(value, dict) or value.get("channel") != channel:
                raise ReolinkCgiError("Reolink position channel could not be verified")

            def axis(name: str) -> float | None:
                raw = value.get(name)
                if raw is None:
                    return None
                if isinstance(raw, bool):
                    raise ReolinkCgiError("Reolink motor position is invalid")
                try:
                    result = float(raw)
                except (TypeError, ValueError):
                    raise ReolinkCgiError("Reolink motor position is invalid") from None
                if not math.isfinite(result):
                    raise ReolinkCgiError("Reolink motor position is invalid")
                return result

            return ReolinkPtzPosition(pan=axis("Ppos"), tilt=axis("Tpos"), channel=channel)

        return await self._with_session(_read)

    async def get_motion_automation(self, *, channel: int = 0) -> dict[str, bool | None]:
        """Read tracking/guard independently; unavailable fields remain unknown."""
        if isinstance(channel, bool) or not isinstance(channel, int) or channel < 0:
            raise ReolinkCgiError("Reolink channel is invalid")

        async def _read(token: str) -> dict[str, bool | None]:
            result: dict[str, bool | None] = {"auto_tracking": None, "automatic_return": None}
            for command, container, field, output in (
                ("GetAiCfg", "AiCfg", "bSmartTrack", "auto_tracking"),
                ("GetPtzGuard", "PtzGuard", "benable", "automatic_return"),
            ):
                try:
                    entry = await self._command(
                        token, command=command, action=0, param={"channel": channel}
                    )
                    value = (entry.get("value") or {}).get(container)
                    if not isinstance(value, dict) or value.get("channel", channel) != channel:
                        continue
                    raw = value.get(field)
                    if type(raw) in {int, bool} and raw in (0, 1):
                        result[output] = bool(raw)
                except ReolinkCgiError:
                    continue
            return result

        return await self._with_session(_read)

    async def list_presets(self, *, include_disabled: bool = False) -> list[ReolinkCgiPreset]:
        async def _read(token: str) -> list[ReolinkCgiPreset]:
            entry = await self._command(
                token,
                command="GetPtzPreset",
                action=0,
                param={"channel": 0},
            )
            raw_presets = (entry.get("value") or {}).get("PtzPreset") or []
            if not isinstance(raw_presets, list):
                raise ReolinkCgiError("Reolink CGI returned an invalid preset list")
            presets: list[ReolinkCgiPreset] = []
            for item in raw_presets:
                if not isinstance(item, dict):
                    continue
                try:
                    slot = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if not 0 <= slot <= 63:
                    continue
                enabled = bool(int(item.get("enable") or 0))
                if enabled or include_disabled:
                    presets.append(
                        ReolinkCgiPreset(
                            slot=slot,
                            name=str(item.get("name") or "").strip(),
                            enabled=enabled,
                        )
                    )
            return sorted(presets, key=lambda preset: preset.slot)

        return await self._with_session(_read)

    async def set_current_position_preset(self, *, name: str) -> ReolinkCgiPreset:
        preset_name = str(name or "").strip()
        if not preset_name:
            raise ReolinkCgiError("Reolink preset name is required")
        if len(preset_name) > 31:
            raise ReolinkCgiError("Reolink preset name exceeds the camera limit")

        async def _set(token: str) -> ReolinkCgiPreset:
            presets = await self._list_presets_with_token(token, include_disabled=True)
            existing = next(
                (preset for preset in presets if preset.enabled and preset.name == preset_name),
                None,
            )
            if existing is not None:
                return existing
            free = next((preset for preset in presets if not preset.enabled), None)
            if free is None:
                raise ReolinkCgiError("Reolink has no free PTZ preset slots")
            await self._command(
                token,
                command="SetPtzPreset",
                action=0,
                param={
                    "PtzPreset": {
                        "channel": 0,
                        "enable": 1,
                        "id": free.slot,
                        "name": preset_name,
                    }
                },
            )
            reconciled = await self._list_presets_with_token(token, include_disabled=True)
            confirmed = next(
                (
                    preset
                    for preset in reconciled
                    if preset.slot == free.slot and preset.enabled and preset.name == preset_name
                ),
                None,
            )
            if confirmed is None:
                raise ReolinkCgiError("Reolink preset creation could not be confirmed")
            return confirmed

        return await self._with_session(_set)

    async def goto_preset(self, *, preset_token: str, speed: int = 4) -> None:
        slot = parse_reolink_preset_token(preset_token)
        safe_speed = min(32, max(1, int(speed)))

        async def _goto(token: str) -> None:
            presets = await self._list_presets_with_token(token, include_disabled=True)
            selected = next((preset for preset in presets if preset.slot == slot), None)
            if selected is None or not selected.enabled:
                raise ReolinkCgiError("Reolink preset is not enabled")
            await self._command(
                token,
                command="PtzCtrl",
                action=0,
                param={"channel": 0, "op": "ToPos", "id": slot, "speed": safe_speed},
            )

        await self._with_session(_goto)

    async def _list_presets_with_token(
        self,
        token: str,
        *,
        include_disabled: bool,
    ) -> list[ReolinkCgiPreset]:
        entry = await self._command(
            token,
            command="GetPtzPreset",
            action=0,
            param={"channel": 0},
        )
        raw_presets = (entry.get("value") or {}).get("PtzPreset") or []
        if not isinstance(raw_presets, list):
            raise ReolinkCgiError("Reolink CGI returned an invalid preset list")
        presets: list[ReolinkCgiPreset] = []
        for item in raw_presets:
            if not isinstance(item, dict):
                continue
            try:
                slot = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if not 0 <= slot <= 63:
                continue
            enabled = bool(int(item.get("enable") or 0))
            if enabled or include_disabled:
                presets.append(
                    ReolinkCgiPreset(
                        slot=slot,
                        name=str(item.get("name") or "").strip(),
                        enabled=enabled,
                    )
                )
        return sorted(presets, key=lambda preset: preset.slot)

    async def _with_session(self, callback: Any) -> Any:
        token = await self._login()
        try:
            return await callback(token)
        finally:
            with contextlib.suppress(ReolinkCgiError):
                await self._command(token, command="Logout", action=0, param={})

    async def _login(self) -> str:
        base = _cgi_base_url(self.device_xaddr)
        payload = [
            {
                "cmd": "Login",
                "action": 0,
                "param": {"User": {"userName": self.username, "password": self.password}},
            }
        ]
        response = await asyncio.to_thread(
            _post_json,
            url=f"{base}/cgi-bin/api.cgi?cmd=Login",
            payload=payload,
            timeout_s=self.timeout_s,
        )
        entry = _response_entry(response, "Login")
        token = str(((entry.get("value") or {}).get("Token") or {}).get("name") or "").strip()
        if not token:
            raise ReolinkCgiError("Reolink CGI did not return a session token")
        return token

    async def _command(
        self,
        token: str,
        *,
        command: str,
        action: int,
        param: dict[str, Any],
    ) -> dict[str, Any]:
        base = _cgi_base_url(self.device_xaddr)
        query = urllib.parse.urlencode({"cmd": command, "token": token})
        response = await asyncio.to_thread(
            _post_json,
            url=f"{base}/cgi-bin/api.cgi?{query}",
            payload=[{"cmd": command, "action": int(action), "param": param}],
            timeout_s=self.timeout_s,
        )
        return _response_entry(response, command)
