from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import secrets
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal


SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"

TDS_NS = "http://www.onvif.org/ver10/device/wsdl"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"

ONVIF_ALTERNATE_DEVICE_SERVICE_PORTS = (2020, 8000, 8080, 8899)


class OnvifError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OnvifProfile:
    token: str
    name: str
    encoding: str = ""
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    has_ptz: bool = False


@dataclass(frozen=True, slots=True)
class OnvifPtzPreset:
    token: str
    name: str = ""
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None


@dataclass(frozen=True, slots=True)
class OnvifPtzStatus:
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    move_status: str = ""
    error: str = ""
    utc_time: str = ""


def normalize_onvif_xaddr(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""

    # Accept plain host/ip[:port] and "upgrade" to a common ONVIF Device Service path.
    if "://" not in value:
        return f"http://{value.rstrip('/')}/onvif/device_service"

    try:
        parsed = urllib.parse.urlsplit(value)
    except Exception:
        return value

    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        return value

    path = parsed.path or ""
    if not path or path == "/":
        path = "/onvif/device_service"
    return urllib.parse.urlunsplit(parsed._replace(path=path))


def onvif_xaddr_candidates(raw: str) -> list[str]:
    primary = normalize_onvif_xaddr(raw)
    if not primary:
        return []

    value = str(raw or "").strip()
    parsed_input = _parse_onvif_input(value)
    if parsed_input is None:
        return [primary]

    if _safe_parsed_port(parsed_input) is not None:
        return [primary]

    host = parsed_input.hostname
    if not host:
        return [primary]

    if "://" in value:
        scheme = str(parsed_input.scheme or "").lower()
        if scheme != "http":
            return [primary]
        path = parsed_input.path or ""
        if path not in {"", "/", "/onvif/device_service"}:
            return [primary]
    else:
        scheme = "http"
        path = parsed_input.path or ""
        if path not in {"", "/"}:
            return [primary]

    primary_parsed = urllib.parse.urlsplit(primary)
    candidate_path = primary_parsed.path or "/onvif/device_service"
    candidates = [primary]
    for port in ONVIF_ALTERNATE_DEVICE_SERVICE_PORTS:
        candidate = urllib.parse.urlunsplit(
            (
                scheme,
                _format_host_port(host, port),
                candidate_path,
                "",
                "",
            )
        )
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _parse_onvif_input(value: str) -> urllib.parse.SplitResult | None:
    try:
        if "://" in value:
            parsed = urllib.parse.urlsplit(value)
        else:
            parsed = urllib.parse.urlsplit(f"//{value}")
    except Exception:
        return None
    return parsed if parsed.netloc else None


def _safe_parsed_port(parsed: urllib.parse.SplitResult) -> int | None:
    try:
        return parsed.port
    except ValueError:
        return None


def _format_host_port(host: str, port: int) -> str:
    value = str(host or "").strip()
    if ":" in value and not value.startswith("["):
        value = f"[{value}]"
    return f"{value}:{int(port)}"


def normalize_rtsp_url(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except Exception:
        return value

    if str(parsed.scheme or "").lower() != "rtsp" or not parsed.netloc:
        return value

    # Drop userinfo if present: rtsp://user:pass@host -> rtsp://host
    if "@" in parsed.netloc:
        host = parsed.netloc.split("@", 1)[1]
        return urllib.parse.urlunsplit(parsed._replace(netloc=host))
    return value


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _utc_timestamp() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    # Many devices are picky about fractional seconds.
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_wsse_header(
    *,
    username: str,
    password: str,
    auth_mode: Literal["digest", "text"],
    created: str,
    nonce_bytes: bytes,
) -> str:
    user = _xml_escape(username)
    created_xml = _xml_escape(created)
    nonce_b64 = base64.b64encode(nonce_bytes).decode("ascii")

    if auth_mode == "digest":
        digest_raw = hashlib.sha1(nonce_bytes + created.encode("utf-8") + password.encode("utf-8")).digest()  # noqa: S324
        pwd_value = base64.b64encode(digest_raw).decode("ascii")
        pwd_type = (
            "http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
        )
    else:
        pwd_value = _xml_escape(password)
        pwd_type = (
            "http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-username-token-profile-1.0#PasswordText"
        )

    nonce_type = (
        "http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
    )

    return (
        f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE_NS}" xmlns:wsu="{WSU_NS}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{user}</wsse:Username>"
        f'<wsse:Password Type="{pwd_type}">{pwd_value}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{nonce_type}">{nonce_b64}</wsse:Nonce>'
        f"<wsu:Created>{created_xml}</wsu:Created>"
        "</wsse:UsernameToken>"
        "</wsse:Security>"
    )


def _wrap_envelope(
    body_xml: str,
    *,
    soap_ns: str,
    username: str,
    password: str,
    auth_mode: Literal["none", "digest", "text"],
) -> bytes:
    header_xml = ""
    if auth_mode != "none" and (username.strip() or password.strip()):
        created = _utc_timestamp()
        nonce_bytes = secrets.token_bytes(16)
        header_xml = _build_wsse_header(
            username=username,
            password=password,
            auth_mode="digest" if auth_mode == "digest" else "text",
            created=created,
            nonce_bytes=nonce_bytes,
        )

    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{soap_ns}">'
        f"<s:Header>{header_xml}</s:Header>"
        f"<s:Body>{body_xml}</s:Body>"
        "</s:Envelope>"
    )
    return envelope.encode("utf-8")


def _soap_fault_text(root: ET.Element, *, soap_ns: str) -> str | None:
    fault = root.find(f".//{{{soap_ns}}}Body/{{{soap_ns}}}Fault")
    if fault is None:
        return None

    # SOAP 1.2
    text = fault.findtext(f".//{{{soap_ns}}}Reason/{{{soap_ns}}}Text")
    if text:
        return str(text).strip() or "ONVIF SOAP fault"

    # SOAP 1.1
    text = fault.findtext("faultstring")
    if text:
        return str(text).strip() or "ONVIF SOAP fault"

    return "ONVIF SOAP fault"


def _http_post_soap_sync(
    *,
    url: str,
    body: bytes,
    timeout_s: float,
    soap_action: str | None,
    soap_version: Literal["1.1", "1.2"],
) -> bytes:
    headers = {
        "User-Agent": "Toposync/0.1 (ONVIF)",
        "Accept": "application/soap+xml, text/xml, */*",
    }

    if soap_version == "1.1":
        headers["Content-Type"] = "text/xml; charset=utf-8"
        if soap_action:
            headers["SOAPAction"] = f'"{soap_action}"'
    else:
        # SOAP 1.2. Some devices accept SOAPAction too; keep it if given.
        headers["Content-Type"] = "application/soap+xml; charset=utf-8"
        if soap_action:
            headers["SOAPAction"] = f'"{soap_action}"'

    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read() if hasattr(exc, "read") else b""
        message = f"ONVIF HTTP error ({exc.code})"
        if payload:
            try:
                root = ET.fromstring(payload)
                fault = _soap_fault_text(root, soap_ns=SOAP12_NS if soap_version == "1.2" else SOAP11_NS)
                if fault:
                    message = fault
            except Exception:
                try:
                    text = payload.decode("utf-8", errors="ignore").strip()
                except Exception:
                    text = ""
                if text:
                    # Help debugging device-specific failures without dumping huge HTML pages.
                    compact = " ".join(text.split())
                    snippet = compact[:200]
                    if snippet:
                        message = f"{message}: {snippet}"
        raise OnvifError(message) from exc
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(str(exc) or "ONVIF request failed") from exc


async def _http_post_soap(
    *,
    url: str,
    body: bytes,
    timeout_s: float,
    soap_action: str | None,
    soap_version: Literal["1.1", "1.2"],
) -> bytes:
    return await asyncio.to_thread(
        _http_post_soap_sync,
        url=url,
        body=body,
        timeout_s=timeout_s,
        soap_action=soap_action,
        soap_version=soap_version,
    )


def _parse_xml(payload: bytes) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except Exception as exc:  # noqa: BLE001
        raise OnvifError("Invalid ONVIF XML response") from exc


def _raise_if_fault(root: ET.Element, *, soap_ns: str) -> None:
    fault = _soap_fault_text(root, soap_ns=soap_ns)
    if fault:
        raise OnvifError(fault)


def _findtext(root: ET.Element, path: str, *, default: str = "") -> str:
    value = root.findtext(path)
    if value is None:
        return default
    return str(value).strip()


def _parse_capabilities(payload: bytes, *, soap_ns: str) -> tuple[str | None, str | None]:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    media = _findtext(root, f".//{{{TT_NS}}}Media/{{{TT_NS}}}XAddr", default="") or None
    ptz = _findtext(root, f".//{{{TT_NS}}}PTZ/{{{TT_NS}}}XAddr", default="") or None
    return media, ptz


def _parse_profiles(payload: bytes, *, soap_ns: str) -> list[OnvifProfile]:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    out: list[OnvifProfile] = []
    for el in root.findall(f".//{{{TRT_NS}}}Profiles"):
        token = str(el.attrib.get("token") or "").strip()
        if not token:
            continue
        name = _findtext(el, f".//{{{TT_NS}}}Name", default="") or token
        encoding = _findtext(el, f".//{{{TT_NS}}}VideoEncoderConfiguration/{{{TT_NS}}}Encoding", default="")
        width_raw = _findtext(
            el,
            f".//{{{TT_NS}}}VideoEncoderConfiguration/{{{TT_NS}}}Resolution/{{{TT_NS}}}Width",
            default="",
        )
        height_raw = _findtext(
            el,
            f".//{{{TT_NS}}}VideoEncoderConfiguration/{{{TT_NS}}}Resolution/{{{TT_NS}}}Height",
            default="",
        )
        fps_raw = _findtext(
            el,
            f".//{{{TT_NS}}}VideoEncoderConfiguration/{{{TT_NS}}}RateControl/{{{TT_NS}}}FrameRateLimit",
            default="",
        )
        width = int(width_raw) if width_raw.isdigit() else None
        height = int(height_raw) if height_raw.isdigit() else None
        fps = int(fps_raw) if fps_raw.isdigit() else None
        has_ptz = el.find(f".//{{{TT_NS}}}PTZConfiguration") is not None
        out.append(
            OnvifProfile(
                token=token,
                name=name,
                encoding=encoding,
                width=width,
                height=height,
                fps=fps,
                has_ptz=has_ptz,
            )
        )
    return out


def _attr_float(el: ET.Element | None, name: str) -> float | None:
    if el is None:
        return None
    raw = el.get(name)
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except Exception:
        return None


def _parse_ptz_presets(payload: bytes, *, soap_ns: str) -> list[OnvifPtzPreset]:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)

    out: list[OnvifPtzPreset] = []
    for preset in root.findall(f".//{{{PTZ_NS}}}Preset"):
        token = str(preset.get("token") or "").strip()
        if not token:
            continue

        name = _findtext(preset, f".//{{{TT_NS}}}Name", default="") or _findtext(preset, ".//Name", default="")
        pan_tilt = preset.find(f".//{{{TT_NS}}}PTZPosition/{{{TT_NS}}}PanTilt")
        zoom_el = preset.find(f".//{{{TT_NS}}}PTZPosition/{{{TT_NS}}}Zoom")
        out.append(
            OnvifPtzPreset(
                token=token,
                name=str(name or "").strip(),
                pan=_attr_float(pan_tilt, "x"),
                tilt=_attr_float(pan_tilt, "y"),
                zoom=_attr_float(zoom_el, "x"),
            )
        )
    return out


def _parse_ptz_status(payload: bytes, *, soap_ns: str) -> OnvifPtzStatus:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)

    status = root.find(f".//{{{PTZ_NS}}}PTZStatus")
    position = status.find(f".//{{{TT_NS}}}Position") if status is not None else None
    pan_tilt = position.find(f".//{{{TT_NS}}}PanTilt") if position is not None else None
    zoom_el = position.find(f".//{{{TT_NS}}}Zoom") if position is not None else None

    move_status = _findtext(status, f".//{{{TT_NS}}}MoveStatus", default="") if status is not None else ""
    error = _findtext(status, f".//{{{TT_NS}}}Error", default="") if status is not None else ""
    utc_time = _findtext(status, f".//{{{TT_NS}}}UtcTime", default="") if status is not None else ""

    return OnvifPtzStatus(
        pan=_attr_float(pan_tilt, "x"),
        tilt=_attr_float(pan_tilt, "y"),
        zoom=_attr_float(zoom_el, "x"),
        move_status=str(move_status or "").strip(),
        error=str(error or "").strip(),
        utc_time=str(utc_time or "").strip(),
    )


def _parse_stream_uri(payload: bytes, *, soap_ns: str) -> str:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    uri = _findtext(root, f".//{{{TRT_NS}}}MediaUri/{{{TT_NS}}}Uri", default="") or _findtext(
        root, f".//{{{TT_NS}}}Uri", default=""
    )
    return normalize_rtsp_url(uri)


def _tds_get_capabilities_body() -> str:
    return (
        f'<tds:GetCapabilities xmlns:tds="{TDS_NS}">'
        "<tds:Category>All</tds:Category>"
        "</tds:GetCapabilities>"
    )


def _trt_get_profiles_body() -> str:
    return f'<trt:GetProfiles xmlns:trt="{TRT_NS}" />'


def _trt_get_stream_uri_body(profile_token: str) -> str:
    token = _xml_escape(profile_token)
    return (
        f'<trt:GetStreamUri xmlns:trt="{TRT_NS}" xmlns:tt="{TT_NS}">'
        "<trt:StreamSetup>"
        "<tt:Stream>RTP-Unicast</tt:Stream>"
        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
        "</trt:StreamSetup>"
        f"<trt:ProfileToken>{token}</trt:ProfileToken>"
        "</trt:GetStreamUri>"
    )


def _tptz_get_presets_body(profile_token: str) -> str:
    token = _xml_escape(profile_token)
    return (
        f'<tptz:GetPresets xmlns:tptz="{PTZ_NS}">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        "</tptz:GetPresets>"
    )


def _tptz_goto_preset_body(profile_token: str, preset_token: str) -> str:
    profile = _xml_escape(profile_token)
    preset = _xml_escape(preset_token)
    return (
        f'<tptz:GotoPreset xmlns:tptz="{PTZ_NS}">'
        f"<tptz:ProfileToken>{profile}</tptz:ProfileToken>"
        f"<tptz:PresetToken>{preset}</tptz:PresetToken>"
        "</tptz:GotoPreset>"
    )


def _tptz_get_status_body(profile_token: str) -> str:
    token = _xml_escape(profile_token)
    return (
        f'<tptz:GetStatus xmlns:tptz="{PTZ_NS}">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        "</tptz:GetStatus>"
    )


def _tptz_absolute_move_body(
    profile_token: str,
    *,
    pan: float | None,
    tilt: float | None,
    zoom: float | None,
) -> str:
    token = _xml_escape(profile_token)
    position_parts: list[str] = []
    if pan is not None and tilt is not None:
        position_parts.append(f'<tt:PanTilt x="{float(pan):.6f}" y="{float(tilt):.6f}" />')
    if zoom is not None:
        position_parts.append(f'<tt:Zoom x="{float(zoom):.6f}" />')
    position_xml = "<tptz:Position>" + "".join(position_parts) + "</tptz:Position>"
    return (
        f'<tptz:AbsoluteMove xmlns:tptz="{PTZ_NS}" xmlns:tt="{TT_NS}">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        f"{position_xml}"
        "</tptz:AbsoluteMove>"
    )


def _tptz_continuous_move_body(
    profile_token: str,
    *,
    pan: float,
    tilt: float,
    zoom: float,
    timeout_s: float | None,
) -> str:
    token = _xml_escape(profile_token)
    timeout_xml = ""
    if timeout_s is not None and timeout_s > 0.0:
        # PTZ ContinuousMove expects an xs:duration; use PT{seconds}S.
        seconds = f"{float(timeout_s):.3f}".rstrip("0").rstrip(".")
        timeout_xml = f"<tptz:Timeout>PT{seconds}S</tptz:Timeout>"

    velocity_parts: list[str] = []
    # Some devices are picky if we include unused components (e.g., Zoom=0 on Pan/Tilt moves).
    if abs(float(pan)) > 1e-6 or abs(float(tilt)) > 1e-6:
        velocity_parts.append(f'<tt:PanTilt x="{float(pan):.6f}" y="{float(tilt):.6f}" />')
    if abs(float(zoom)) > 1e-6:
        velocity_parts.append(f'<tt:Zoom x="{float(zoom):.6f}" />')

    velocity_xml = ""
    if velocity_parts:
        velocity_xml = "<tptz:Velocity>" + "".join(velocity_parts) + "</tptz:Velocity>"
    return (
        f'<tptz:ContinuousMove xmlns:tptz="{PTZ_NS}" xmlns:tt="{TT_NS}">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        f"{velocity_xml}"
        f"{timeout_xml}"
        "</tptz:ContinuousMove>"
    )


def _tptz_stop_body(profile_token: str, *, pan_tilt: bool, zoom: bool) -> str:
    token = _xml_escape(profile_token)
    pan_xml = "true" if bool(pan_tilt) else "false"
    zoom_xml = "true" if bool(zoom) else "false"
    return (
        f'<tptz:Stop xmlns:tptz="{PTZ_NS}">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        f"<tptz:PanTilt>{pan_xml}</tptz:PanTilt>"
        f"<tptz:Zoom>{zoom_xml}</tptz:Zoom>"
        "</tptz:Stop>"
    )


def _tptz_relative_move_body(
    profile_token: str,
    *,
    pan: float,
    tilt: float,
    zoom: float,
) -> str:
    token = _xml_escape(profile_token)

    translation_parts: list[str] = []
    if abs(float(pan)) > 1e-6 or abs(float(tilt)) > 1e-6:
        translation_parts.append(f'<tt:PanTilt x="{float(pan):.6f}" y="{float(tilt):.6f}" />')
    if abs(float(zoom)) > 1e-6:
        translation_parts.append(f'<tt:Zoom x="{float(zoom):.6f}" />')

    translation_xml = ""
    if translation_parts:
        translation_xml = "<tptz:Translation>" + "".join(translation_parts) + "</tptz:Translation>"

    return (
        f'<tptz:RelativeMove xmlns:tptz="{PTZ_NS}" xmlns:tt="{TT_NS}">'
        f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
        f"{translation_xml}"
        "</tptz:RelativeMove>"
    )


def _action(ns: str, method: str) -> str:
    return f"{ns}/{method}"


@dataclass(slots=True)
class OnvifClient:
    xaddr: str
    username: str = ""
    password: str = ""
    timeout_s: float = 3.0
    auth_mode: Literal["auto", "digest", "text", "none"] = "auto"

    async def get_capabilities(self) -> tuple[str | None, str | None]:
        xaddr = normalize_onvif_xaddr(self.xaddr)
        if not xaddr:
            raise OnvifError("Missing ONVIF device service URL")

        body_xml = _tds_get_capabilities_body()
        soap_action = _action(TDS_NS, "GetCapabilities")
        return await self._call_and_parse_capabilities(
            url=xaddr,
            body_xml=body_xml,
            soap_action=soap_action,
        )

    async def get_profiles(self, media_xaddr: str) -> list[OnvifProfile]:
        url = str(media_xaddr or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF media service URL")

        body_xml = _trt_get_profiles_body()
        soap_action = _action(TRT_NS, "GetProfiles")
        return await self._call_and_parse_profiles(
            url=url,
            body_xml=body_xml,
            soap_action=soap_action,
        )

    async def get_stream_uri(self, media_xaddr: str, *, profile_token: str) -> str:
        url = str(media_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF media service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _trt_get_stream_uri_body(token)
        soap_action = _action(TRT_NS, "GetStreamUri")
        return await self._call_and_parse_stream_uri(url=url, body_xml=body_xml, soap_action=soap_action)

    async def get_ptz_presets(self, ptz_xaddr: str, *, profile_token: str) -> list[OnvifPtzPreset]:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_get_presets_body(token)
        soap_action = _action(PTZ_NS, "GetPresets")
        return await self._call_and_parse_ptz_presets(url=url, body_xml=body_xml, soap_action=soap_action)

    async def goto_preset(self, ptz_xaddr: str, *, profile_token: str, preset_token: str) -> None:
        url = str(ptz_xaddr or "").strip()
        profile = str(profile_token or "").strip()
        preset = str(preset_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not profile:
            raise OnvifError("Missing ONVIF profile token")
        if not preset:
            raise OnvifError("Missing ONVIF preset token")

        body_xml = _tptz_goto_preset_body(profile, preset)
        soap_action = _action(PTZ_NS, "GotoPreset")
        await self._call_and_raise_if_fault(url=url, body_xml=body_xml, soap_action=soap_action, operation="GotoPreset")

    async def get_ptz_status(self, ptz_xaddr: str, *, profile_token: str) -> OnvifPtzStatus:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_get_status_body(token)
        soap_action = _action(PTZ_NS, "GetStatus")
        return await self._call_and_parse_ptz_status(url=url, body_xml=body_xml, soap_action=soap_action)

    async def absolute_move(
        self,
        ptz_xaddr: str,
        *,
        profile_token: str,
        pan: float | None,
        tilt: float | None,
        zoom: float | None,
    ) -> None:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")
        if (pan is None) != (tilt is None):
            raise OnvifError("ONVIF absolute pan and tilt must be provided together")
        if pan is None and tilt is None and zoom is None:
            raise OnvifError("Missing ONVIF absolute PTZ position")

        body_xml = _tptz_absolute_move_body(token, pan=pan, tilt=tilt, zoom=zoom)
        soap_action = _action(PTZ_NS, "AbsoluteMove")
        await self._call_and_raise_if_fault(
            url=url,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="AbsoluteMove",
        )

    async def continuous_move(
        self,
        ptz_xaddr: str,
        *,
        profile_token: str,
        pan: float,
        tilt: float,
        zoom: float,
        timeout_s: float | None = None,
    ) -> None:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_continuous_move_body(token, pan=pan, tilt=tilt, zoom=zoom, timeout_s=timeout_s)
        soap_action = _action(PTZ_NS, "ContinuousMove")
        await self._call_and_raise_if_fault(
            url=url,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="ContinuousMove",
        )

    async def relative_move(
        self,
        ptz_xaddr: str,
        *,
        profile_token: str,
        pan: float,
        tilt: float,
        zoom: float,
    ) -> None:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_relative_move_body(token, pan=pan, tilt=tilt, zoom=zoom)
        soap_action = _action(PTZ_NS, "RelativeMove")
        await self._call_and_raise_if_fault(
            url=url,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="RelativeMove",
        )

    async def stop(self, ptz_xaddr: str, *, profile_token: str, pan_tilt: bool = True, zoom: bool = True) -> None:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_stop_body(token, pan_tilt=bool(pan_tilt), zoom=bool(zoom))
        soap_action = _action(PTZ_NS, "Stop")
        await self._call_and_raise_if_fault(url=url, body_xml=body_xml, soap_action=soap_action, operation="Stop")

    def _auth_attempts(self) -> list[Literal["none", "digest", "text"]]:
        mode = str(self.auth_mode or "").strip().lower()
        if mode == "none":
            return ["none"]
        if mode == "digest":
            return ["digest"]
        if mode == "text":
            return ["text"]

        # auto: try digest first, then plain text, then no header.
        return ["digest", "text", "none"]

    async def _call_and_parse_capabilities(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str,
    ) -> tuple[str | None, str | None]:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    return _parse_capabilities(payload, soap_ns=ns)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise OnvifError(str(last_error) if last_error else "ONVIF GetCapabilities failed")

    async def _call_and_parse_profiles(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str,
    ) -> list[OnvifProfile]:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    return _parse_profiles(payload, soap_ns=ns)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise OnvifError(str(last_error) if last_error else "ONVIF GetProfiles failed")

    async def _call_and_parse_stream_uri(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str,
    ) -> str:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    uri = _parse_stream_uri(payload, soap_ns=ns)
                    if uri:
                        return uri
                    raise OnvifError("ONVIF returned an empty RTSP URL")
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise OnvifError(str(last_error) if last_error else "ONVIF GetStreamUri failed")

    async def _call_and_parse_ptz_presets(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str,
    ) -> list[OnvifPtzPreset]:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    return _parse_ptz_presets(payload, soap_ns=ns)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise OnvifError(str(last_error) if last_error else "ONVIF GetPresets failed")

    async def _call_and_parse_ptz_status(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str,
    ) -> OnvifPtzStatus:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    return _parse_ptz_status(payload, soap_ns=ns)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise OnvifError(str(last_error) if last_error else "ONVIF GetStatus failed")

    async def _call_and_raise_if_fault(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str,
        operation: str,
    ) -> None:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    root = _parse_xml(payload)
                    _raise_if_fault(root, soap_ns=ns)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise OnvifError(str(last_error) if last_error else f"ONVIF {operation} failed")

    async def _call(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str | None,
        soap_ns: str,
        soap_version: Literal["1.1", "1.2"],
        auth: Literal["none", "digest", "text"],
    ) -> bytes:
        envelope = _wrap_envelope(
            body_xml,
            soap_ns=soap_ns,
            username=self.username,
            password=self.password,
            auth_mode=auth,
        )
        return await _http_post_soap(
            url=url,
            body=envelope,
            timeout_s=self.timeout_s,
            soap_action=soap_action,
            soap_version=soap_version,
        )
