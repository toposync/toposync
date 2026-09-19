from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import math
import re
import secrets
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Literal


SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"

TDS_NS = "http://www.onvif.org/ver10/device/wsdl"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"

ONVIF_ALTERNATE_DEVICE_SERVICE_PORTS = (2020, 8000, 8080, 8899)
NORMALIZED_PAN_TILT_POSITION_SPACE = (
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
)
NORMALIZED_PAN_TILT_TRANSLATION_SPACE = (
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"
)
NORMALIZED_PAN_TILT_VELOCITY_SPACE = (
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
)
NORMALIZED_PAN_TILT_SPACES = frozenset(
    {
        NORMALIZED_PAN_TILT_POSITION_SPACE,
        NORMALIZED_PAN_TILT_TRANSLATION_SPACE,
        NORMALIZED_PAN_TILT_VELOCITY_SPACE,
    }
)
NORMALIZED_ZOOM_POSITION_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"


class OnvifError(RuntimeError):
    pass


class OnvifAmbiguousMutationError(OnvifError):
    """The device may have applied a mutating request before the response failed."""


@dataclass(frozen=True, slots=True)
class OnvifProfile:
    token: str
    name: str
    encoding: str = ""
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    has_ptz: bool = False
    ptz_configuration_token: str = ""


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
    pan_tilt_space: str = ""
    zoom_space: str = ""


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


def _response_body(payload: bytes, *, soap_ns: str) -> ET.Element:
    root = _parse_xml(payload)
    body = root.find(f"{{{soap_ns}}}Body")
    if body is None:
        raise OnvifError("Missing ONVIF SOAP Body")
    _raise_if_fault(body, soap_ns=soap_ns)
    return body


def _published_range(element: ET.Element | None) -> dict[str, float] | None:
    if element is None:
        return None
    try:
        minimum = float(_findtext(element, f"{{{TT_NS}}}Min"))
        maximum = float(_findtext(element, f"{{{TT_NS}}}Max"))
    except ValueError:
        return None
    if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum:
        return None
    return {"min": minimum, "max": maximum}


def _published_pan_tilt_space(
    element: ET.Element, *, expected_uri: str
) -> dict | None:
    uri = _findtext(element, f"{{{TT_NS}}}URI")
    x_range = _published_range(element.find(f"{{{TT_NS}}}XRange"))
    y_range = _published_range(element.find(f"{{{TT_NS}}}YRange"))
    if (
        (uri in NORMALIZED_PAN_TILT_SPACES and uri != expected_uri)
        or x_range is None
        or y_range is None
    ):
        return None
    normalized = uri == expected_uri and all(
        -1.0 <= axis["min"] <= axis["max"] <= 1.0 for axis in (x_range, y_range)
    )
    return {"uri": uri, "x": x_range, "y": y_range, "normalized": normalized}


def _published_zoom_space(element: ET.Element) -> dict | None:
    uri = _findtext(element, f"{{{TT_NS}}}URI")
    x_range = _published_range(element.find(f"{{{TT_NS}}}XRange"))
    if not uri or x_range is None:
        return None
    normalized = (
        uri == NORMALIZED_ZOOM_POSITION_SPACE
        and 0.0 <= x_range["min"] <= x_range["max"] <= 1.0
    )
    return {"uri": uri, "x": x_range, "normalized": normalized}


def _populate_absolute_zoom_capabilities(
    result: dict, configuration: ET.Element, spaces: ET.Element,
) -> None:
    advertised = spaces.findall(f"{{{TT_NS}}}AbsoluteZoomPositionSpace")
    parsed = [_published_zoom_space(space) for space in advertised]
    valid = [space for space in parsed if space is not None]
    result["spaces"]["absolute_zoom"] = valid
    result["absolute_zoom"] = bool(valid) if len(valid) == len(parsed) else None
    default_uri = _findtext(configuration, f"{{{TT_NS}}}DefaultAbsoluteZoomPositionSpace") or None
    result["defaults"]["absolute_zoom"] = default_uri
    if len(valid) != len(parsed):
        result["reasons"].append("invalid_absolute_zoom_space")
    if result["absolute_zoom"] is not True:
        return
    candidates = [space for space in valid if space["uri"] == default_uri]
    if len(candidates) != 1:
        result["reasons"].append("absolute_zoom_default_space_not_unique")
        return
    effective = candidates[0]
    limit_element = configuration.find(f"{{{TT_NS}}}ZoomLimits")
    if limit_element is not None:
        range_element = limit_element.find(f"{{{TT_NS}}}Range")
        limits = _published_zoom_space(range_element) if range_element is not None else None
        if limits is None or limits["uri"] != default_uri:
            result["reasons"].append("invalid_configuration_zoom_limits")
            return
        bounds = {
            "min": max(effective["x"]["min"], limits["x"]["min"]),
            "max": min(effective["x"]["max"], limits["x"]["max"]),
        }
        if bounds["min"] > bounds["max"]:
            result["reasons"].append("configuration_zoom_limits_outside_space")
            return
        effective = {**effective, "x": bounds}
    result["limits"]["zoom"] = {
        **effective["x"], "space": default_uri, "normalized": effective["normalized"],
    }


def _duration_seconds(value: str) -> float | None:
    # Calendar years/months have no fixed duration; never guess their seconds.
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if match is None or not any(match.groups()):
        return None
    seconds = sum(float(part or 0) * unit for part, unit in zip(match.groups(), (86400, 3600, 60, 1)))
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _published_ptz_timeout(options: ET.Element) -> dict | None:
    elements = options.findall(f"{{{TT_NS}}}PTZTimeout")
    if len(elements) != 1:
        return None
    minimum = _duration_seconds(_findtext(elements[0], f"{{{TT_NS}}}Min"))
    maximum = _duration_seconds(_findtext(elements[0], f"{{{TT_NS}}}Max"))
    if minimum is None or maximum is None or not 0 <= minimum <= maximum or maximum <= 0:
        return None
    return {"min": minimum, "max": maximum}


def _populate_pan_tilt_capabilities(
    result: dict, configuration: ET.Element, options_response: ET.Element,
) -> None:
    options = options_response.findall(f"{{{PTZ_NS}}}PTZConfigurationOptions")
    if len(options) != 1:
        result["reasons"].append("configuration_options_not_unique")
        return
    result["continuous_timeout_s"] = _published_ptz_timeout(options[0])
    spaces = options[0].find(f"{{{TT_NS}}}Spaces")
    if spaces is None:
        result["reasons"].append("missing_configuration_spaces")
        return
    _populate_absolute_zoom_capabilities(result, configuration, spaces)
    for mode, space_tag, default_tag, expected_uri in (
        (
            "absolute",
            "AbsolutePanTiltPositionSpace",
            "DefaultAbsolutePantTiltPositionSpace",
            NORMALIZED_PAN_TILT_POSITION_SPACE,
        ),
        (
            "continuous",
            "ContinuousPanTiltVelocitySpace",
            "DefaultContinuousPanTiltVelocitySpace",
            NORMALIZED_PAN_TILT_VELOCITY_SPACE,
        ),
        (
            "relative",
            "RelativePanTiltTranslationSpace",
            "DefaultRelativePanTiltTranslationSpace",
            NORMALIZED_PAN_TILT_TRANSLATION_SPACE,
        ),
    ):
        advertised = spaces.findall(f"{{{TT_NS}}}{space_tag}")
        parsed = [
            _published_pan_tilt_space(space, expected_uri=expected_uri) for space in advertised
        ]
        valid = [space for space in parsed if space is not None]
        result["spaces"][mode] = valid
        result[mode] = bool(valid) if len(valid) == len(parsed) else None
        result["defaults"][mode] = _findtext(configuration, f"{{{TT_NS}}}{default_tag}") or None
        if len(valid) != len(parsed):
            result["reasons"].append(f"invalid_{mode}_space")
    if result["absolute"] is not True:
        return
    default_uri = result["defaults"]["absolute"]
    candidates = [space for space in result["spaces"]["absolute"] if space["uri"] == default_uri]
    if len(candidates) != 1:
        result["reasons"].append("absolute_default_space_not_unique")
        return
    effective = candidates[0]
    limit_element = configuration.find(f"{{{TT_NS}}}PanTiltLimits")
    if limit_element is not None:
        range_element = limit_element.find(f"{{{TT_NS}}}Range")
        limits = (
            _published_pan_tilt_space(
                range_element, expected_uri=NORMALIZED_PAN_TILT_POSITION_SPACE
            )
            if range_element is not None
            else None
        )
        if limits is None or limits["uri"] != default_uri:
            result["reasons"].append("invalid_configuration_limits")
            return
        effective = {
            **effective,
            **{
                axis: {
                    "min": max(effective[axis]["min"], limits[axis]["min"]),
                    "max": min(effective[axis]["max"], limits[axis]["max"]),
                }
                for axis in ("x", "y")
            },
        }
        if any(effective[axis]["min"] > effective[axis]["max"] for axis in ("x", "y")):
            result["reasons"].append("configuration_limits_outside_space")
            return
        result["limits_kind"] = "configuration"
    else:
        result["limits_kind"] = "space"
    result["limits"].update({
        axis: {**effective[coordinate], "space": default_uri, "normalized": effective["normalized"]}
        for axis, coordinate in (("pan", "x"), ("tilt", "y"))
    })


def _parse_capabilities(payload: bytes, *, soap_ns: str) -> tuple[str | None, str | None]:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    media = _findtext(root, f".//{{{TT_NS}}}Media/{{{TT_NS}}}XAddr", default="") or None
    ptz = _findtext(root, f".//{{{TT_NS}}}PTZ/{{{TT_NS}}}XAddr", default="") or None
    return media, ptz


def _parse_profiles(payload: bytes, *, soap_ns: str) -> list[OnvifProfile]:
    root = _response_body(payload, soap_ns=soap_ns)
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
        ptz_configuration = el.find(f"{{{TT_NS}}}PTZConfiguration")
        has_ptz = ptz_configuration is not None
        out.append(
            OnvifProfile(
                token=token,
                name=name,
                encoding=encoding,
                width=width,
                height=height,
                fps=fps,
                has_ptz=has_ptz,
                ptz_configuration_token=(
                    str(ptz_configuration.get("token") or "").strip()
                    if ptz_configuration is not None else ""
                ),
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


def _ptz_preset_token_from_root(root: ET.Element) -> str:
    token = _findtext(root, f".//{{{PTZ_NS}}}PresetToken", default="")
    if token:
        return token

    # A few ONVIF implementations omit the expected namespace on response children.
    for element in root.iter():
        if str(element.tag).rsplit("}", 1)[-1] != "PresetToken":
            continue
        value = str(element.text or "").strip()
        if value:
            return value
    return ""


def _parse_ptz_preset_token(payload: bytes, *, soap_ns: str) -> str:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    return _ptz_preset_token_from_root(root)


def _parse_ptz_mutation_response(payload: bytes, *, soap_ns: str) -> ET.Element:
    try:
        root = _parse_xml(payload)
    except OnvifError as exc:
        raise OnvifAmbiguousMutationError(str(exc)) from exc
    _raise_if_fault(root, soap_ns=soap_ns)
    return root


def _parse_ptz_status(payload: bytes, *, soap_ns: str) -> OnvifPtzStatus:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)

    status = root.find(f".//{{{PTZ_NS}}}PTZStatus")
    position = status.find(f".//{{{TT_NS}}}Position") if status is not None else None
    pan_tilt = position.find(f".//{{{TT_NS}}}PanTilt") if position is not None else None
    zoom_el = position.find(f".//{{{TT_NS}}}Zoom") if position is not None else None

    move_status = ""
    if status is not None:
        move_status_element = status.find(f".//{{{TT_NS}}}MoveStatus")
        if move_status_element is not None:
            direct_status = str(move_status_element.text or "").strip().upper()
            axis_statuses = [
                str(child.text or "").strip().upper()
                for child in list(move_status_element)
                if str(child.text or "").strip()
            ]
            if direct_status:
                move_status = direct_status
            elif any(value == "MOVING" for value in axis_statuses):
                move_status = "MOVING"
            elif axis_statuses and all(value == "IDLE" for value in axis_statuses):
                move_status = "IDLE"
            elif axis_statuses:
                move_status = "UNKNOWN"
    error = _findtext(status, f".//{{{TT_NS}}}Error", default="") if status is not None else ""
    utc_time = _findtext(status, f".//{{{TT_NS}}}UtcTime", default="") if status is not None else ""

    return OnvifPtzStatus(
        pan=_attr_float(pan_tilt, "x"),
        tilt=_attr_float(pan_tilt, "y"),
        zoom=_attr_float(zoom_el, "x"),
        move_status=str(move_status or "").strip(),
        error=str(error or "").strip(),
        utc_time=str(utc_time or "").strip(),
        pan_tilt_space=str(pan_tilt.get("space") or "").strip() if pan_tilt is not None else "",
        zoom_space=str(zoom_el.get("space") or "").strip() if zoom_el is not None else "",
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


def _tptz_set_preset_body(
    profile_token: str,
    *,
    preset_name: str = "",
    preset_token: str = "",
) -> str:
    profile = _xml_escape(profile_token)
    name = str(preset_name or "").strip()
    token = str(preset_token or "").strip()
    name_xml = f"<tptz:PresetName>{_xml_escape(name)}</tptz:PresetName>" if name else ""
    token_xml = f"<tptz:PresetToken>{_xml_escape(token)}</tptz:PresetToken>" if token else ""
    return (
        f'<tptz:SetPreset xmlns:tptz="{PTZ_NS}">'
        f"<tptz:ProfileToken>{profile}</tptz:ProfileToken>"
        f"{name_xml}"
        f"{token_xml}"
        "</tptz:SetPreset>"
    )


def _tptz_remove_preset_body(profile_token: str, preset_token: str) -> str:
    profile = _xml_escape(profile_token)
    preset = _xml_escape(preset_token)
    return (
        f'<tptz:RemovePreset xmlns:tptz="{PTZ_NS}">'
        f"<tptz:ProfileToken>{profile}</tptz:ProfileToken>"
        f"<tptz:PresetToken>{preset}</tptz:PresetToken>"
        "</tptz:RemovePreset>"
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
    if timeout_s is not None:
        # PTZ ContinuousMove expects an xs:duration; use PT{seconds}S.
        try:
            numeric_timeout = float(timeout_s)
        except (TypeError, ValueError, OverflowError) as exc:
            raise OnvifError("Invalid continuous movement timeout") from exc
        if not math.isfinite(numeric_timeout) or numeric_timeout <= 0.0:
            raise OnvifError("Invalid continuous movement timeout")
        # Decimal(str(float)) retains the finite float's meaningful decimal
        # digits, while fixed-point formatting avoids scientific notation.
        seconds = format(Decimal(str(numeric_timeout)), "f")
        if "." in seconds:
            seconds = seconds.rstrip("0").rstrip(".")
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
    _ptz_transport_cache: dict[
        tuple[str, str],
        tuple[Literal["1.1", "1.2"], str, Literal["none", "digest", "text"]],
    ] = field(default_factory=dict, init=False, repr=False)
    _profile_ptz_configuration_tokens: dict[str, str] = field(
        default_factory=dict, init=False, repr=False,
    )
    _continuous_timeout_ranges: dict[tuple[str, str], dict | None] = field(
        default_factory=dict, init=False, repr=False,
    )

    async def get_device_information(self) -> dict[str, str]:
        """Read the device family without returning serial numbers or hardware IDs."""
        response = await self._call_read_only_response(
            url=normalize_onvif_xaddr(self.xaddr),
            namespace=TDS_NS,
            method="GetDeviceInformation",
        )
        return {
            key: _findtext(response, f"{{{TDS_NS}}}{tag}")
            for key, tag in (
                ("manufacturer", "Manufacturer"),
                ("model", "Model"),
                ("firmware_version", "FirmwareVersion"),
            )
        }

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
        profiles = await self._call_and_parse_profiles(
            url=url,
            body_xml=body_xml,
            soap_action=soap_action,
        )
        self._profile_ptz_configuration_tokens.clear()
        for profile in profiles:
            if sum(candidate.token == profile.token for candidate in profiles) == 1:
                self._profile_ptz_configuration_tokens[profile.token] = profile.ptz_configuration_token
        return profiles

    async def get_ptz_capabilities(
        self,
        ptz_xaddr: str,
        *,
        profile_token: str,
        configuration_token: str = "",
    ) -> dict:
        """Read published PanTilt capabilities for an exactly bound media profile.

        ``configuration_token`` must come from that profile's GetProfiles result.
        When omitted, the binding from this client's last GetProfiles is used.
        Missing/ambiguous information remains unknown; coordinates are never degrees.
        The limits describe accepted coordinates, not independently verified travel.
        """
        url = str(ptz_xaddr or "").strip()
        profile = str(profile_token or "").strip()
        if not url or not profile:
            raise OnvifError("Missing ONVIF PTZ service URL or profile token")
        supplied = str(configuration_token or "").strip()
        known = self._profile_ptz_configuration_tokens.get(profile)
        configuration = supplied or known or ""
        result: dict = {
            "status": "unknown",
            "profile_token": profile,
            "configuration_token": configuration or None,
            "node_token": None,
            "absolute": None, "continuous": None, "relative": None,
            "absolute_zoom": None,
            "spaces": {"absolute": [], "continuous": [], "relative": [], "absolute_zoom": []},
            "defaults": {"absolute": None, "continuous": None, "relative": None, "absolute_zoom": None},
            "limits": {"pan": None, "tilt": None, "zoom": None},
            "limits_kind": None,
            "continuous_timeout_s": None,
            "presets": {"maximum_count": None, "home_supported": None},
            "reasons": [],
        }
        if known is not None and supplied and known != supplied:
            result["reasons"].append("profile_configuration_mismatch")
            return result
        if not configuration:
            result["reasons"].append("missing_profile_configuration")
            return result
        try:
            response = await self._call_read_only_response(
                url=url, namespace=PTZ_NS, method="GetConfigurations",
            )
        except OnvifError:
            result["reasons"].append("configurations_unavailable")
            return result
        matches = [
            element for element in response.findall(f"{{{PTZ_NS}}}PTZConfiguration")
            if element.get("token") == configuration
        ]
        if len(matches) != 1:
            result["reasons"].append("configuration_not_unique")
            return result
        selected = matches[0]
        node_token = _findtext(selected, f"{{{TT_NS}}}NodeToken")
        result["node_token"] = node_token or None
        options, nodes = await asyncio.gather(
            self._call_read_only_response(
                url=url, namespace=PTZ_NS, method="GetConfigurationOptions",
                arguments_xml=(
                    f"<service:ConfigurationToken>{_xml_escape(configuration)}"
                    "</service:ConfigurationToken>"
                ),
            ),
            self._call_read_only_response(url=url, namespace=PTZ_NS, method="GetNodes"),
            return_exceptions=True,
        )
        if isinstance(options, BaseException):
            if not isinstance(options, OnvifError):
                raise options
            result["reasons"].append("configuration_options_unavailable")
        else:
            _populate_pan_tilt_capabilities(result, selected, options)
        if isinstance(nodes, BaseException):
            if not isinstance(nodes, OnvifError):
                raise nodes
            result["reasons"].append("nodes_unavailable")
        else:
            matching_nodes = [
                element for element in nodes.findall(f"{{{PTZ_NS}}}PTZNode")
                if node_token and element.get("token") == node_token
            ]
            if len(matching_nodes) != 1:
                result["reasons"].append("node_not_unique")
            else:
                node = matching_nodes[0]
                maximum = _findtext(node, f"{{{TT_NS}}}MaximumNumberOfPresets")
                home = _findtext(node, f"{{{TT_NS}}}HomeSupported")
                result["presets"] = {
                    "maximum_count": int(maximum) if maximum.isdigit() else None,
                    "home_supported": {"true": True, "1": True, "false": False, "0": False}.get(home),
                }
        if any(result[mode] is not None for mode in ("absolute", "continuous", "relative", "absolute_zoom")):
            result["status"] = "partial" if result["reasons"] else "verified"
        self._continuous_timeout_ranges[(url, profile)] = result["continuous_timeout_s"]
        return result

    async def continuous_move_timeout(
        self, ptz_xaddr: str, *, profile_token: str, requested_s: float,
    ) -> float | None:
        """Device failsafe timeout, independent of the controller's earlier Stop.

        Unknown bounds omit the optional value and use the device default. Only
        the fenced controller, which supplies its own finite Stop, uses this.
        """
        if not math.isfinite(requested_s) or requested_s <= 0:
            raise OnvifError("Invalid continuous movement duration")
        key = (ptz_xaddr, profile_token)
        if key not in self._continuous_timeout_ranges:
            bounds = None
            try:
                if profile_token not in self._profile_ptz_configuration_tokens:
                    media, _ = await self.get_capabilities()
                    if media:
                        await self.get_profiles(media)
                configuration = self._profile_ptz_configuration_tokens.get(profile_token)
                if configuration:
                    response = await self._call_read_only_response(
                        url=ptz_xaddr, namespace=PTZ_NS, method="GetConfigurationOptions",
                        arguments_xml=(f"<service:ConfigurationToken>{_xml_escape(configuration)}"
                                       "</service:ConfigurationToken>"),
                    )
                    options = response.findall(f"{{{PTZ_NS}}}PTZConfigurationOptions")
                    if len(options) == 1:
                        bounds = _published_ptz_timeout(options[0])
            except OnvifError:
                pass
            self._continuous_timeout_ranges[key] = bounds
        bounds = self._continuous_timeout_ranges[key]
        if bounds is None:
            return None
        if bounds["min"] > 30:
            raise OnvifError("Device minimum movement timeout exceeds the safety budget")
        clamped_timeout = min(max(requested_s, bounds["min"]), bounds["max"], 30.0)

        # Although xs:duration permits fractional seconds, some ONVIF firmware
        # rejects them. Prefer a whole-second device failsafe when rounding up
        # remains inside the advertised range and safety budget. The controller
        # still owns the exact pulse duration and sends Stop independently.
        whole_second_timeout = float(math.ceil(clamped_timeout))
        if whole_second_timeout <= bounds["max"] and whole_second_timeout <= 30.0:
            return whole_second_timeout
        return clamped_timeout

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
        return await self._call_and_parse_ptz_presets(
            url=url,
            body_xml=body_xml,
            soap_action=soap_action,
            transport_profile_token=token,
        )

    async def set_preset(
        self,
        ptz_xaddr: str,
        *,
        profile_token: str,
        preset_name: str = "",
        preset_token: str = "",
    ) -> str:
        url = str(ptz_xaddr or "").strip()
        profile = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not profile:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_set_preset_body(
            profile,
            preset_name=preset_name,
            preset_token=preset_token,
        )
        soap_action = _action(PTZ_NS, "SetPreset")
        payload, soap_ns = await self._call_ptz_mutation_once(
            url=url,
            profile_token=profile,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="SetPreset",
            preflight="presets",
        )
        root = _parse_ptz_mutation_response(payload, soap_ns=soap_ns)
        token = _ptz_preset_token_from_root(root)
        if not token:
            raise OnvifAmbiguousMutationError("ONVIF returned an empty preset token")
        return token

    async def remove_preset(
        self,
        ptz_xaddr: str,
        *,
        profile_token: str,
        preset_token: str,
    ) -> None:
        url = str(ptz_xaddr or "").strip()
        profile = str(profile_token or "").strip()
        preset = str(preset_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not profile:
            raise OnvifError("Missing ONVIF profile token")
        if not preset:
            raise OnvifError("Missing ONVIF preset token")

        body_xml = _tptz_remove_preset_body(profile, preset)
        soap_action = _action(PTZ_NS, "RemovePreset")
        payload, soap_ns = await self._call_ptz_mutation_once(
            url=url,
            profile_token=profile,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="RemovePreset",
            preflight="presets",
        )
        _parse_ptz_mutation_response(payload, soap_ns=soap_ns)

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
        payload, soap_ns = await self._call_ptz_mutation_once(
            url=url,
            profile_token=profile,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="GotoPreset",
        )
        _parse_ptz_mutation_response(payload, soap_ns=soap_ns)

    async def get_ptz_status(self, ptz_xaddr: str, *, profile_token: str) -> OnvifPtzStatus:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_get_status_body(token)
        soap_action = _action(PTZ_NS, "GetStatus")
        return await self._call_and_parse_ptz_status(
            url=url,
            body_xml=body_xml,
            soap_action=soap_action,
            transport_profile_token=token,
        )

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
        payload, soap_ns = await self._call_ptz_mutation_once(
            url=url,
            profile_token=token,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="AbsoluteMove",
        )
        _parse_ptz_mutation_response(payload, soap_ns=soap_ns)

    async def continuous_move(
        self,
        ptz_xaddr: str,
        *,
        profile_token: str,
        pan: float,
        tilt: float,
        zoom: float,
        timeout_s: float | None = None,
    ) -> dict[str, float]:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_continuous_move_body(token, pan=pan, tilt=tilt, zoom=zoom, timeout_s=timeout_s)
        soap_action = _action(PTZ_NS, "ContinuousMove")
        dispatched_at: float | None = None

        def dispatched() -> None:
            nonlocal dispatched_at
            dispatched_at = time.monotonic()

        payload, soap_ns = await self._call_ptz_mutation_once(
            url=url,
            profile_token=token,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="ContinuousMove",
            on_dispatch=dispatched,
        )
        _parse_ptz_mutation_response(payload, soap_ns=soap_ns)
        return (
            {"transport_elapsed_seconds": max(0.0, time.monotonic() - dispatched_at)}
            if dispatched_at is not None else {}
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
        payload, soap_ns = await self._call_ptz_mutation_once(
            url=url,
            profile_token=token,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="RelativeMove",
        )
        _parse_ptz_mutation_response(payload, soap_ns=soap_ns)

    async def stop(self, ptz_xaddr: str, *, profile_token: str, pan_tilt: bool = True, zoom: bool = True) -> None:
        url = str(ptz_xaddr or "").strip()
        token = str(profile_token or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PTZ service URL")
        if not token:
            raise OnvifError("Missing ONVIF profile token")

        body_xml = _tptz_stop_body(token, pan_tilt=bool(pan_tilt), zoom=bool(zoom))
        soap_action = _action(PTZ_NS, "Stop")
        payload, soap_ns = await self._call_ptz_mutation_once(
            url=url,
            profile_token=token,
            body_xml=body_xml,
            soap_action=soap_action,
            operation="Stop",
        )
        _parse_ptz_mutation_response(payload, soap_ns=soap_ns)

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

    async def _call_read_only_response(
        self, *, url: str, namespace: str, method: str, arguments_xml: str = "",
    ) -> ET.Element:
        """Use the existing read retry policy; inspect only the expected SOAP Body."""
        if not url:
            raise OnvifError("Missing ONVIF service URL")
        body_xml = (
            f'<service:{method} xmlns:service="{namespace}">'
            f"{arguments_xml}</service:{method}>"
        )
        last_error: Exception | None = None
        for version, namespace_soap in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(
                        url=url, body_xml=body_xml, soap_action=_action(namespace, method),
                        soap_ns=namespace_soap, soap_version=version, auth=auth,  # type: ignore[arg-type]
                    )
                    body = _response_body(payload, soap_ns=namespace_soap)
                    responses = body.findall(f"{{{namespace}}}{method}Response")
                    if len(responses) != 1:
                        raise OnvifError("Missing or ambiguous ONVIF response body")
                    return responses[0]
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise OnvifError(str(last_error) if last_error else f"ONVIF {method} failed")

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
        transport_profile_token: str,
    ) -> list[OnvifPtzPreset]:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    presets = _parse_ptz_presets(payload, soap_ns=ns)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
                self._ptz_transport_cache[(url, transport_profile_token)] = (version, ns, auth)  # type: ignore[assignment]
                return presets
        raise OnvifError(str(last_error) if last_error else "ONVIF GetPresets failed")

    async def _resolve_ptz_transport(
        self,
        *,
        url: str,
        profile_token: str,
        preflight: Literal["presets", "status"],
    ) -> tuple[Literal["1.1", "1.2"], str, Literal["none", "digest", "text"]]:
        cache_key = (url, profile_token)
        cached = self._ptz_transport_cache.get(cache_key)
        if cached is not None:
            return cached

        if preflight == "presets":
            await self._call_and_parse_ptz_presets(
                url=url,
                body_xml=_tptz_get_presets_body(profile_token),
                soap_action=_action(PTZ_NS, "GetPresets"),
                transport_profile_token=profile_token,
            )
        else:
            await self._call_and_parse_ptz_status(
                url=url,
                body_xml=_tptz_get_status_body(profile_token),
                soap_action=_action(PTZ_NS, "GetStatus"),
                transport_profile_token=profile_token,
            )
        resolved = self._ptz_transport_cache.get(cache_key)
        if resolved is None:
            raise OnvifError("ONVIF PTZ transport preflight did not resolve a transport")
        return resolved

    async def _call_ptz_mutation_once(
        self,
        *,
        url: str,
        profile_token: str,
        body_xml: str,
        soap_action: str,
        operation: str,
        preflight: Literal["presets", "status"] = "status",
        on_dispatch: Callable[[], None] | None = None,
    ) -> tuple[bytes, str]:
        soap_version, soap_ns, auth = await self._resolve_ptz_transport(
            url=url,
            profile_token=profile_token,
            preflight=preflight,
        )
        try:
            if on_dispatch is not None:
                on_dispatch()
            payload = await self._call(
                url=url,
                body_xml=body_xml,
                soap_action=soap_action,
                soap_ns=soap_ns,
                soap_version=soap_version,
                auth=auth,
            )
        except Exception as exc:  # noqa: BLE001
            raise OnvifAmbiguousMutationError(
                str(exc) or f"ONVIF {operation} response failed"
            ) from exc
        return payload, soap_ns

    async def _call_and_parse_ptz_status(
        self,
        *,
        url: str,
        body_xml: str,
        soap_action: str,
        transport_profile_token: str,
    ) -> OnvifPtzStatus:
        last_error: Exception | None = None
        for version, ns in (("1.2", SOAP12_NS), ("1.1", SOAP11_NS)):
            for auth in self._auth_attempts():
                try:
                    payload = await self._call(url=url, body_xml=body_xml, soap_action=soap_action, soap_ns=ns, soap_version=version, auth=auth)  # type: ignore[arg-type]
                    status = _parse_ptz_status(payload, soap_ns=ns)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
                self._ptz_transport_cache[(url, transport_profile_token)] = (version, ns, auth)  # type: ignore[assignment]
                return status
        raise OnvifError(str(last_error) if last_error else "ONVIF GetStatus failed")

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
