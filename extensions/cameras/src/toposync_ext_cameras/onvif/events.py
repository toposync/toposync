from __future__ import annotations

import asyncio
import contextlib
import io
import http.client
from datetime import datetime
import secrets
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .client import (
    SOAP11_NS,
    SOAP12_NS,
    TDS_NS,
    OnvifClient,
    OnvifError,
    _action,
    _build_wsse_header,
    _findtext,
    _soap_fault_text,
    _parse_xml,
    _read_soap_payload,
    _NoSoapRedirects,
    _raise_if_fault,
    _wrap_envelope,
    _xml_escape,
)


TEV_NS = "http://www.onvif.org/ver10/events/wsdl"
WSA_NS = "http://www.w3.org/2005/08/addressing"
WSA_2004_NS = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WSNT_NS = "http://docs.oasis-open.org/wsn/b-2"
WSTOP_NS = "http://docs.oasis-open.org/wsn/t-1"
# Source operators may poll every 60 seconds; allow two intervals before releasing.
_WATCH_IDLE_SECONDS = 120.0
_PULL_RECOVERY_SECONDS = 120.0


class OnvifTransientTransportError(OnvifError):
    """A failed read does not necessarily invalidate the subscription."""


def _transient_transport_error(error: BaseException) -> bool:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, urllib.error.HTTPError):
            return False
        if isinstance(error, (ConnectionError, TimeoutError, http.client.RemoteDisconnected)):
            return True
        reason = getattr(error, "reason", None)
        error = reason if isinstance(reason, BaseException) else error.__cause__
    return False


@dataclass(frozen=True, slots=True)
class OnvifService:
    namespace: str
    xaddr: str


@dataclass(frozen=True, slots=True)
class OnvifEventItemDescription:
    name: str
    type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type}


@dataclass(frozen=True, slots=True)
class OnvifEventDescriptor:
    topic: str
    qualified_topic: str = ""
    item_name: str = ""
    item_type: str = ""
    is_property: bool = False
    label: str = ""
    source_items: tuple[OnvifEventItemDescription, ...] = ()
    key_items: tuple[OnvifEventItemDescription, ...] = ()
    data_items: tuple[OnvifEventItemDescription, ...] = ()

    @property
    def is_boolean(self) -> bool:
        return _is_boolean_type(self.item_type)

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "qualified_topic": self.qualified_topic,
            "item_name": self.item_name,
            "item_type": self.item_type,
            "is_property": self.is_property,
            "is_boolean": self.is_boolean,
            "label": self.label or humanize_onvif_event_label(self.topic, self.item_name),
            "source_items": [item.as_dict() for item in self.source_items],
            "key_items": [item.as_dict() for item in self.key_items],
            "data_items": [item.as_dict() for item in self.data_items],
        }


@dataclass(frozen=True, slots=True)
class OnvifPullPointSubscription:
    address: str
    current_time: str = ""
    termination_time: str = ""


@dataclass(frozen=True, slots=True)
class OnvifEventMessage:
    sequence: int
    topic: str
    qualified_topic: str = ""
    operation: str = ""
    utc_time: str = ""
    source: dict[str, str] = field(default_factory=dict)
    key: dict[str, str] = field(default_factory=dict)
    data: dict[str, str] = field(default_factory=dict)
    received_at_ts: float = 0.0
    received_monotonic: float | None = None
    is_property: bool | None = None
    state_changed: bool = False
    state_accepted: bool = False
    session: int = 0

    def boolean_value(self, item_name: str = "") -> bool | None:
        wanted = str(item_name or "").strip()
        if wanted:
            return _parse_bool(self.data.get(wanted))
        for value in self.data.values():
            parsed = _parse_bool(value)
            if parsed is not None:
                return parsed
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": int(self.sequence),
            "topic": self.topic,
            "qualified_topic": self.qualified_topic,
            "operation": self.operation,
            "utc_time": self.utc_time,
            "source": dict(self.source),
            "key": dict(self.key),
            "data": dict(self.data),
            "received_at_ts": float(self.received_at_ts),
            "received_age_seconds": (max(0.0, time.monotonic() - self.received_monotonic)
                                     if self.received_monotonic is not None else None),
            "is_property": self.is_property,
            "state_changed": self.state_changed,
            "state_accepted": self.state_accepted,
            "session": self.session,
        }


@dataclass(frozen=True, slots=True)
class OnvifCameraEventContext:
    camera_id: str
    camera_name: str
    xaddr: str
    username: str = ""
    password: str = ""
    event_xaddr: str = ""
    timeout_s: float = 3.5


@dataclass(slots=True)
class _OnvifBooleanState:
    descriptor: OnvifEventDescriptor
    value: bool | None = None
    last_event_ts: float = 0.0
    last_changed_ts: float = 0.0
    last_active_ts: float = 0.0
    last_active_monotonic: float | None = None
    last_inactive_monotonic: float | None = None
    last_camera_time: datetime | None = None
    operation: str = ""
    source: dict[str, str] = field(default_factory=dict)
    key: dict[str, str] = field(default_factory=dict)

    def as_dict(self, *, camera_id: str, available: bool, error: str = "") -> dict[str, Any]:
        return {
            "camera_id": camera_id,
            "topic": self.descriptor.topic,
            "qualified_topic": self.descriptor.qualified_topic,
            "item_name": self.descriptor.item_name,
            "item_type": self.descriptor.item_type,
            "label": self.descriptor.label
            or humanize_onvif_event_label(self.descriptor.topic, self.descriptor.item_name),
            "known": self.value is not None,
            "value": self.value,
            "last_event_ts": self.last_event_ts or None,
            "last_changed_ts": self.last_changed_ts or None,
            "last_active_ts": self.last_active_ts or None,
            "last_active_age_seconds": (max(0.0, time.monotonic() - self.last_active_monotonic)
                                        if self.last_active_monotonic is not None else None),
            "last_inactive_age_seconds": (max(0.0, time.monotonic() - self.last_inactive_monotonic)
                                          if self.last_inactive_monotonic is not None else None),
            "operation": self.operation,
            "source": dict(self.source),
            "key": dict(self.key),
            "available": bool(available),
            "error": str(error or ""),
        }


@dataclass(slots=True)
class _CameraEventRuntime:
    context: OnvifCameraEventContext
    descriptors: list[OnvifEventDescriptor] = field(default_factory=list)
    event_xaddr: str = ""
    states: dict[tuple[Any, ...], _OnvifBooleanState] = field(default_factory=dict)
    events: deque[OnvifEventMessage] = field(default_factory=lambda: deque(maxlen=512))
    sequence: int = 0
    available: bool = False
    watch_available: bool = False
    session: int = 0
    successful_pulls: int = 0
    failed_pulls: int = 0
    subscriptions: int = 0
    transitions: int = 0
    queue_overflows: int = 0
    last_pull_ts: float = 0.0
    recovering: bool = False
    unsubscribe_status: str = "not_attempted"
    error: str = ""
    descriptors_loaded_at_ts: float = 0.0
    descriptors_attempted_at_ts: float = 0.0
    started: bool = False
    last_consumer_monotonic: float = 0.0
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _local_name(tag: str) -> str:
    raw = str(tag or "")
    if "}" in raw:
        return raw.rsplit("}", 1)[1]
    if ":" in raw:
        return raw.rsplit(":", 1)[1]
    return raw


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return str(el.text or "").strip()


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _is_boolean_type(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    tail = text.rsplit(":", 1)[-1]
    return tail in {"boolean", "bool"}


def _attr(el: ET.Element, local_name: str) -> str:
    wanted = str(local_name or "").strip()
    for key, value in el.attrib.items():
        if _local_name(key) == wanted:
            return str(value or "").strip()
    return ""


def _simple_item_descriptions(parent: ET.Element | None) -> tuple[OnvifEventItemDescription, ...]:
    if parent is None:
        return ()
    out: list[OnvifEventItemDescription] = []
    for item in parent.iter():
        if _local_name(item.tag) != "SimpleItemDescription":
            continue
        name = _attr(item, "Name")
        if not name:
            continue
        out.append(OnvifEventItemDescription(name=name, type=_attr(item, "Type")))
    return tuple(out)


def _first_child_by_local(parent: ET.Element | None, local_name: str) -> ET.Element | None:
    if parent is None:
        return None
    wanted = str(local_name or "").strip()
    for child in list(parent):
        if _local_name(child.tag) == wanted:
            return child
    return None


def _simple_items(parent: ET.Element | None) -> dict[str, str]:
    if parent is None:
        return {}
    out: dict[str, str] = {}
    for item in parent.iter():
        if _local_name(item.tag) != "SimpleItem":
            continue
        name = _attr(item, "Name")
        if not name:
            continue
        out[name] = _attr(item, "Value")
    return out


def _topic_text_to_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts: list[str] = []
    for part in raw.split("/"):
        token = part.strip()
        if not token:
            continue
        parts.append(_local_name(token))
    return "/".join(parts)


def humanize_onvif_event_label(topic: str, item_name: str = "") -> str:
    joined = f"{topic}/{item_name}".strip("/").lower()
    if "people" in joined or "person" in joined:
        return "Person"
    if "motion" in joined or "motionalarm" in joined or "cellmotiondetector" in joined:
        return "Motion"
    if "intrusion" in joined:
        return "Intrusion"
    if "linecross" in joined or "line_cross" in joined:
        return "Line crossing"
    if "tamper" in joined:
        return "Tamper"
    if "imagetoodark" in joined or "image_too_dark" in joined:
        return "Image too dark"
    if "profilechanged" in joined:
        return "Profile changed"
    if "configurationchanged" in joined:
        return "Configuration changed"
    tail = str(item_name or "").strip() or str(topic or "").strip().split("/")[-1]
    if tail.lower().startswith("is") and len(tail) > 2:
        tail = tail[2:]
    return " ".join(_split_identifier(tail)) or str(topic or "").strip()


def _split_identifier(value: str) -> list[str]:
    raw = str(value or "").strip().replace("-", "_").replace(".", "_")
    if not raw:
        return []
    tokens: list[str] = []
    for chunk in raw.split("_"):
        if not chunk:
            continue
        current = ""
        for char in chunk:
            if current and char.isupper() and (not current[-1].isupper()):
                tokens.append(current)
                current = char
            else:
                current += char
        if current:
            tokens.append(current)
    return [token[:1].upper() + token[1:].lower() for token in tokens if token]


def parse_onvif_services(payload: bytes, *, soap_ns: str = SOAP12_NS) -> list[OnvifService]:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    out: list[OnvifService] = []
    for service in root.iter():
        if _local_name(service.tag) != "Service":
            continue
        namespace = ""
        xaddr = ""
        for child in list(service):
            local = _local_name(child.tag)
            if local == "Namespace":
                namespace = _text(child)
            elif local == "XAddr":
                xaddr = _text(child)
        if namespace and xaddr:
            out.append(OnvifService(namespace=namespace, xaddr=xaddr))
    return out


def parse_get_event_properties(payload: bytes, *, soap_ns: str = SOAP12_NS) -> list[OnvifEventDescriptor]:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    topic_set: ET.Element | None = None
    for el in root.iter():
        if _local_name(el.tag) == "TopicSet":
            topic_set = el
            break
    if topic_set is None:
        return []

    descriptors: list[OnvifEventDescriptor] = []

    def walk(node: ET.Element, path: list[str], qualified: list[str]) -> None:
        local = _local_name(node.tag)
        current_path = path
        current_qualified = qualified
        if local not in {"TopicSet", "MessageDescription", "Documentation"}:
            current_path = [*path, local]
            current_qualified = [*qualified, node.tag]

        for child in list(node):
            if _local_name(child.tag) == "MessageDescription":
                source_items = _simple_item_descriptions(_first_child_by_local(child, "Source"))
                key_items = _simple_item_descriptions(_first_child_by_local(child, "Key"))
                data_items = _simple_item_descriptions(_first_child_by_local(child, "Data"))
                topic = "/".join(current_path)
                is_property = _parse_bool(_attr(child, "IsProperty")) is True
                if data_items:
                    for item in data_items:
                        descriptors.append(
                            OnvifEventDescriptor(
                                topic=topic,
                                qualified_topic="/".join(current_qualified),
                                item_name=item.name,
                                item_type=item.type,
                                is_property=is_property,
                                label=humanize_onvif_event_label(topic, item.name),
                                source_items=source_items,
                                key_items=key_items,
                                data_items=data_items,
                            )
                        )
                else:
                    descriptors.append(
                        OnvifEventDescriptor(
                            topic=topic,
                            qualified_topic="/".join(current_qualified),
                            is_property=is_property,
                            label=humanize_onvif_event_label(topic, ""),
                            source_items=source_items,
                            key_items=key_items,
                            data_items=data_items,
                        )
                    )
                continue
            walk(child, current_path, current_qualified)

    walk(topic_set, [], [])

    seen: set[tuple[str, str]] = set()
    unique: list[OnvifEventDescriptor] = []
    for descriptor in descriptors:
        key = (descriptor.qualified_topic or descriptor.topic, descriptor.item_name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(descriptor)
    return unique


def _qualified_topics(payload: bytes) -> list[str]:
    scopes: list[dict[str, str]] = []
    pending: dict[str, str] = {}
    topics: list[str] = []
    for kind, value in ET.iterparse(io.BytesIO(payload), events=("start-ns", "start", "end")):
        if kind == "start-ns":
            prefix, uri = value
            pending[prefix] = uri
        elif kind == "start":
            scopes.append({**(scopes[-1] if scopes else {}), **pending})
            pending.clear()
        else:
            if _local_name(value.tag) == "Topic":
                parts = []
                for token in _text(value).split("/"):
                    if ":" in token:
                        prefix, local = token.split(":", 1)
                        uri = scopes[-1].get(prefix)
                        parts.append(f"{{{uri}}}{local}" if uri else token)
                    else:
                        parts.append(token)
                topics.append("/".join(parts))
            scopes.pop()
    return topics


def parse_pull_messages(payload: bytes, *, soap_ns: str = SOAP12_NS) -> list[OnvifEventMessage]:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    out: list[OnvifEventMessage] = []
    now = time.time()
    qualified_topics = iter(_qualified_topics(payload))
    for notification in root.iter():
        if _local_name(notification.tag) != "NotificationMessage":
            continue
        topic = ""
        message_el: ET.Element | None = None
        for child in list(notification):
            local = _local_name(child.tag)
            if local == "Topic":
                topic = _topic_text_to_path(_text(child))
            elif local == "Message":
                message_el = child
        inner_message: ET.Element | None = None
        if message_el is not None:
            for child in list(message_el.iter())[1:]:
                if _local_name(child.tag) == "Message":
                    inner_message = child
                    break
        if inner_message is None:
            inner_message = message_el
        if inner_message is None:
            continue
        out.append(
            OnvifEventMessage(
                sequence=0,
                topic=topic,
                qualified_topic=next(qualified_topics, ""),
                operation=_attr(inner_message, "PropertyOperation"),
                utc_time=_attr(inner_message, "UtcTime"),
                source=_simple_items(_first_child_by_local(inner_message, "Source")),
                key=_simple_items(_first_child_by_local(inner_message, "Key")),
                data=_simple_items(_first_child_by_local(inner_message, "Data")),
                received_at_ts=now,
            )
        )
    return out


def _tds_get_services_body() -> str:
    return (
        f'<tds:GetServices xmlns:tds="{TDS_NS}">'
        "<tds:IncludeCapability>true</tds:IncludeCapability>"
        "</tds:GetServices>"
    )


def _tev_get_event_properties_body() -> str:
    return f'<tev:GetEventProperties xmlns:tev="{TEV_NS}" />'


def _tev_create_pull_point_subscription_body(termination_time: str) -> str:
    termination = _xml_escape(str(termination_time or "PT300S").strip() or "PT300S")
    return (
        f'<tev:CreatePullPointSubscription xmlns:tev="{TEV_NS}">'
        f"<tev:InitialTerminationTime>{termination}</tev:InitialTerminationTime>"
        "</tev:CreatePullPointSubscription>"
    )


def _tev_set_synchronization_point_body() -> str:
    return f'<tev:SetSynchronizationPoint xmlns:tev="{TEV_NS}" />'


def _tev_pull_messages_body(*, timeout_s: float, message_limit: int) -> str:
    timeout = max(0.1, min(30.0, float(timeout_s)))
    limit = max(1, min(256, int(message_limit)))
    return (
        f'<tev:PullMessages xmlns:tev="{TEV_NS}">'
        f"<tev:Timeout>{_duration_seconds(timeout)}</tev:Timeout>"
        f"<tev:MessageLimit>{limit}</tev:MessageLimit>"
        "</tev:PullMessages>"
    )


def _duration_seconds(seconds: float) -> str:
    value = max(0.0, float(seconds))
    if value.is_integer():
        return f"PT{int(value)}S"
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"PT{text}S"


def _wsnt_renew_body(termination_time: str) -> str:
    termination = _xml_escape(str(termination_time or "PT300S").strip() or "PT300S")
    return (
        f'<wsnt:Renew xmlns:wsnt="{WSNT_NS}">'
        f"<wsnt:TerminationTime>{termination}</wsnt:TerminationTime>"
        "</wsnt:Renew>"
    )


def _wsnt_unsubscribe_body() -> str:
    return f'<wsnt:Unsubscribe xmlns:wsnt="{WSNT_NS}" />'


def _parse_subscription(payload: bytes, *, soap_ns: str) -> OnvifPullPointSubscription:
    root = _parse_xml(payload)
    _raise_if_fault(root, soap_ns=soap_ns)
    address = (
        _findtext(root, f".//{{{WSA_NS}}}Address", default="")
        or _findtext(root, f".//{{{WSA_2004_NS}}}Address", default="")
    )
    if not address:
        for el in root.iter():
            if _local_name(el.tag) == "Address":
                address = _text(el)
                if address:
                    break
    if not address:
        raise OnvifError("ONVIF Events did not return a PullPoint subscription address")
    current_time = ""
    termination_time = ""
    for el in root.iter():
        local = _local_name(el.tag)
        if local == "CurrentTime":
            current_time = _text(el)
        elif local == "TerminationTime":
            termination_time = _text(el)
    return OnvifPullPointSubscription(
        address=address,
        current_time=current_time,
        termination_time=termination_time,
    )


async def _call_event_operation(
    client: OnvifClient,
    *,
    url: str,
    body_xml: str,
    soap_action: str,
    operation: str,
    transport_cache: dict[tuple[str, str], tuple[str, str, str]] | None = None,
) -> tuple[bytes, str]:
    from .capabilities import camera_service_address
    camera_service_address(client.xaddr, url)
    last_error: Exception | None = None
    attempts = [(version, auth, "soap") for version in ("1.2", "1.1")
                for auth in client._auth_attempts()]  # noqa: SLF001
    if str(client.username or "").strip() or str(client.password or "").strip():
        attempts.extend((version, auth, transport) for transport in ("http", "http_addressed")
                        for version in ("1.2", "1.1") for auth in ("none", "digest", "text"))
    key = (url, operation)
    cached = transport_cache.get(key) if transport_cache is not None else None
    if cached is None and transport_cache is not None and operation == "PullMessages":
        # Synchronization already proved this subscription's SOAP/authentication route.
        cached = transport_cache.get((url, "SetSynchronizationPoint"))
    if cached in attempts:
        attempts.remove(cached)
        attempts.insert(0, cached)
    for version, auth, transport in attempts:
        ns = SOAP12_NS if version == "1.2" else SOAP11_NS
        try:
            if transport == "soap":
                payload = await client._call(  # noqa: SLF001
                    url=url,
                    body_xml=body_xml,
                    soap_action=soap_action,
                    soap_ns=ns,
                    soap_version=version,  # type: ignore[arg-type]
                    auth=auth,  # type: ignore[arg-type]
                )
            else:
                payload = await _call_with_http_auth(
                    client, url=url, body_xml=body_xml, soap_action=soap_action,
                    soap_ns=ns, soap_version=version, envelope_auth=auth,
                    wsa_action=_wsa_action_for_operation(operation, soap_action) if transport == "http_addressed" else "",
                )
            root = _parse_xml(payload)
            _raise_if_fault(root, soap_ns=ns)
            if transport_cache is not None:
                # A client can visit multiple subscription URLs; retain only bounded preferences.
                if key not in transport_cache and len(transport_cache) >= 32:
                    transport_cache.pop(next(iter(transport_cache)))
                transport_cache[key] = (version, auth, transport)
            return payload, ns
        except Exception as exc:  # noqa: BLE001
            if operation == "PullMessages" and cached == (version, auth, transport) and _transient_transport_error(exc):
                raise OnvifTransientTransportError("ONVIF read connection interrupted") from exc
            last_error = exc
            if transport_cache is not None:
                transport_cache.pop(key, None)
    if operation == "PullMessages" and last_error is not None and _transient_transport_error(last_error):
        raise OnvifTransientTransportError("ONVIF read connection interrupted") from last_error
    raise OnvifError(str(last_error) if last_error else f"ONVIF {operation} failed") from last_error


def _wsa_action_for_operation(operation: str, soap_action: str | None) -> str:
    op = str(operation or "").strip()
    if op in {"PullMessages", "SetSynchronizationPoint", "Seek"}:
        return f"{TEV_NS}/PullPointSubscription/{op}Request"
    if op in {"CreatePullPointSubscription", "GetEventProperties"}:
        return f"{TEV_NS}/EventPortType/{op}Request"
    if op in {"Renew", "Unsubscribe"}:
        return f"{WSNT_NS}/SubscriptionManager/{op}Request"
    return str(soap_action or "").strip()


def _http_post_soap_http_auth_sync(
    *,
    url: str,
    body: bytes,
    timeout_s: float,
    soap_action: str | None,
    soap_version: str,
    username: str,
    password: str,
) -> bytes:
    headers = {
        "User-Agent": "Toposync/0.1 (ONVIF)",
        "Accept": "application/soap+xml, text/xml, */*",
    }
    if soap_version == "1.1":
        headers["Content-Type"] = "text/xml; charset=utf-8"
    else:
        headers["Content-Type"] = "application/soap+xml; charset=utf-8"
    if soap_action:
        headers["SOAPAction"] = f'"{soap_action}"'

    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, url, username, password)
    opener = urllib.request.build_opener(
        _NoSoapRedirects(),
        urllib.request.HTTPDigestAuthHandler(manager),
        urllib.request.HTTPBasicAuthHandler(manager),
    )
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with opener.open(req, timeout=max(0.5, float(timeout_s))) as resp:  # noqa: S310
            return _read_soap_payload(resp)
    except urllib.error.HTTPError as exc:
        payload = _read_soap_payload(exc) if hasattr(exc, "read") else b""
        message = f"ONVIF HTTP error ({exc.code})"
        if payload:
            try:
                root = ET.fromstring(payload)
                fault = _soap_fault_text(root, soap_ns=SOAP12_NS if soap_version == "1.2" else SOAP11_NS)
                if fault:
                    message = fault
            except Exception:
                text = payload.decode("utf-8", errors="ignore").strip()
                compact = " ".join(text.split())
                if compact:
                    message = f"{message}: {compact[:200]}"
        raise OnvifError(message) from exc
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(str(exc) or "ONVIF request failed") from exc


async def _call_with_http_auth(
    client: OnvifClient,
    *,
    url: str,
    body_xml: str,
    soap_action: str | None,
    soap_ns: str,
    soap_version: str,
    envelope_auth: str,
    wsa_action: str = "",
) -> bytes:
    if wsa_action:
        envelope = _wrap_event_envelope(
            body_xml,
            soap_ns=soap_ns,
            username=client.username,
            password=client.password,
            auth_mode=envelope_auth,
            wsa_action=wsa_action,
            to_url=url,
        )
    else:
        envelope = _wrap_envelope(
            body_xml,
            soap_ns=soap_ns,
            username=client.username,
            password=client.password,
            auth_mode=envelope_auth,  # type: ignore[arg-type]
        )
    return await asyncio.to_thread(
        _http_post_soap_http_auth_sync,
        url=url,
        body=envelope,
        timeout_s=client.timeout_s,
        soap_action=soap_action,
        soap_version=soap_version,
        username=client.username,
        password=client.password,
    )


def _wrap_event_envelope(
    body_xml: str,
    *,
    soap_ns: str,
    username: str,
    password: str,
    auth_mode: str,
    wsa_action: str,
    to_url: str,
) -> bytes:
    header_parts: list[str] = []
    action = str(wsa_action or "").strip()
    if action:
        header_parts.append(
            f'<wsa:Action s:mustUnderstand="1" xmlns:wsa="{WSA_NS}">{_xml_escape(action)}</wsa:Action>'
        )
    to = str(to_url or "").strip()
    if to:
        header_parts.append(f'<wsa:To s:mustUnderstand="1" xmlns:wsa="{WSA_NS}">{_xml_escape(to)}</wsa:To>')
    if auth_mode != "none" and (username.strip() or password.strip()):
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        header_parts.append(
            _build_wsse_header(
                username=username,
                password=password,
                auth_mode="digest" if auth_mode == "digest" else "text",
                created=created,
                nonce_bytes=secrets.token_bytes(16),
            )
        )
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{soap_ns}">'
        f"<s:Header>{''.join(header_parts)}</s:Header>"
        f"<s:Body>{body_xml}</s:Body>"
        "</s:Envelope>"
    )
    return envelope.encode("utf-8")


class OnvifEventsClient:
    def __init__(self, client: OnvifClient) -> None:
        self._client = client
        self._transport_cache: dict[tuple[str, str], tuple[str, str, str]] = {}

    async def get_services(self) -> list[OnvifService]:
        url = self._client.xaddr
        if not url:
            raise OnvifError("Missing ONVIF device service URL")
        payload, ns = await _call_event_operation(
            self._client,
            url=url,
            body_xml=_tds_get_services_body(),
            soap_action=_action(TDS_NS, "GetServices"),
            operation="GetServices",
            transport_cache=self._transport_cache,
        )
        return parse_onvif_services(payload, soap_ns=ns)

    async def resolve_event_xaddr(self, configured_xaddr: str = "") -> str:
        configured = str(configured_xaddr or "").strip()
        if configured:
            return configured
        last_error: Exception | None = None
        try:
            services = await self.get_services()
            for service in services:
                if str(service.namespace or "").strip() == TEV_NS and service.xaddr:
                    return service.xaddr
        except Exception as exc:  # noqa: BLE001
            last_error = exc

        for candidate in self._fallback_event_xaddrs():
            try:
                await self.get_event_properties(candidate)
                return candidate
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error is not None:
            raise OnvifError(str(last_error)) from last_error
        raise OnvifError("ONVIF device did not report an Events service URL")

    def _fallback_event_xaddrs(self) -> list[str]:
        raw = str(self._client.xaddr or "").strip()
        if not raw:
            return []
        try:
            parsed = urllib.parse.urlsplit(raw)
        except Exception:
            return []
        if not parsed.scheme or not parsed.netloc:
            return []
        path = parsed.path or "/"
        prefix = path.rsplit("/", 1)[0].rstrip("/")
        if not prefix:
            prefix = "/onvif"
        candidates: list[str] = []
        for leaf in ("event_service", "Event", "events", "service"):
            candidate_path = f"{prefix}/{leaf}"
            url = urllib.parse.urlunsplit(parsed._replace(path=candidate_path))
            if url not in candidates:
                candidates.append(url)
        return candidates

    async def get_event_properties(self, event_xaddr: str) -> list[OnvifEventDescriptor]:
        url = str(event_xaddr or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF Events service URL")
        payload, ns = await _call_event_operation(
            self._client,
            url=url,
            body_xml=_tev_get_event_properties_body(),
            soap_action=_action(TEV_NS, "GetEventProperties"),
            operation="GetEventProperties",
            transport_cache=self._transport_cache,
        )
        return parse_get_event_properties(payload, soap_ns=ns)

    async def create_pull_point_subscription(
        self,
        event_xaddr: str,
        *,
        termination_time: str = "PT300S",
    ) -> OnvifPullPointSubscription:
        url = str(event_xaddr or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF Events service URL")
        payload, ns = await _call_event_operation(
            self._client,
            url=url,
            body_xml=_tev_create_pull_point_subscription_body(termination_time),
            soap_action=_action(TEV_NS, "CreatePullPointSubscription"),
            operation="CreatePullPointSubscription",
            transport_cache=self._transport_cache,
        )
        return _parse_subscription(payload, soap_ns=ns)

    async def set_synchronization_point(self, subscription_xaddr: str) -> None:
        url = str(subscription_xaddr or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PullPoint subscription URL")
        await _call_event_operation(
            self._client,
            url=url,
            body_xml=_tev_set_synchronization_point_body(),
            soap_action=_action(TEV_NS, "SetSynchronizationPoint"),
            operation="SetSynchronizationPoint",
            transport_cache=self._transport_cache,
        )

    async def pull_messages(
        self,
        subscription_xaddr: str,
        *,
        timeout_s: float = 5.0,
        message_limit: int = 32,
    ) -> list[OnvifEventMessage]:
        url = str(subscription_xaddr or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PullPoint subscription URL")
        wait_seconds = max(0.1, min(30.0, float(timeout_s)))
        # Long polling may legitimately return only after the advertised wait.
        # Keep the larger transport budget local to this request, including auth fallbacks.
        pull_client = replace(self._client, timeout_s=max(self._client.timeout_s, wait_seconds + 2.0))
        payload, ns = await _call_event_operation(
            pull_client,
            url=url,
            body_xml=_tev_pull_messages_body(timeout_s=wait_seconds, message_limit=message_limit),
            soap_action=_action(TEV_NS, "PullMessages"),
            operation="PullMessages",
            transport_cache=self._transport_cache,
        )
        return parse_pull_messages(payload, soap_ns=ns)

    async def renew(self, subscription_xaddr: str, *, termination_time: str = "PT300S") -> OnvifPullPointSubscription:
        url = str(subscription_xaddr or "").strip()
        if not url:
            raise OnvifError("Missing ONVIF PullPoint subscription URL")
        payload, ns = await _call_event_operation(
            self._client,
            url=url,
            body_xml=_wsnt_renew_body(termination_time),
            soap_action=f"{WSNT_NS}/Renew",
            operation="Renew",
            transport_cache=self._transport_cache,
        )

        root = _parse_xml(payload)
        return OnvifPullPointSubscription(address=url,
            current_time=_findtext(root, f".//{{{WSNT_NS}}}CurrentTime"),
            termination_time=_findtext(root, f".//{{{WSNT_NS}}}TerminationTime"))

    async def unsubscribe(self, subscription_xaddr: str) -> None:
        url = str(subscription_xaddr or "").strip()
        if not url:
            return
        await _call_event_operation(
            self._client,
            url=url,
            body_xml=_wsnt_unsubscribe_body(),
            soap_action=f"{WSNT_NS}/Unsubscribe",
            operation="Unsubscribe",
            transport_cache=self._transport_cache,
        )


def _renew_delay(subscription: OnvifPullPointSubscription | None) -> float:
    if subscription is None or not subscription.current_time or not subscription.termination_time:
        return 240.0
    try:
        duration = (datetime.fromisoformat(subscription.termination_time.replace("Z", "+00:00"))
                    - datetime.fromisoformat(subscription.current_time.replace("Z", "+00:00"))).total_seconds()
    except ValueError as exc:
        raise OnvifError("Invalid subscription expiration") from exc
    if duration <= 0:
        raise OnvifError("Expired ONVIF subscription")
    return max(0.1, min(240.0, duration * 0.8))


class OnvifEventStateManager:
    def __init__(
        self,
        *,
        resolve_context: Callable[[str], Any],
        pull_timeout_s: float = 5.0,
        reconnect_backoff_s: float = 5.0,
        descriptors_ttl_s: float = 300.0,
    ) -> None:
        self._resolve_context = resolve_context
        self._pull_timeout_s = max(0.5, min(30.0, float(pull_timeout_s)))
        self._reconnect_backoff_s = max(0.5, min(120.0, float(reconnect_backoff_s)))
        self._descriptors_ttl_s = max(1.0, min(3600.0, float(descriptors_ttl_s)))
        self._runtimes: dict[str, _CameraEventRuntime] = {}
        self._manager_lock = asyncio.Lock()

    async def shutdown(self) -> None:
        runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            runtime.stop_event.set()
            if runtime.task is not None:
                runtime.task.cancel()
        for runtime in runtimes:
            if runtime.task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runtime.task

    def cached_boolean_descriptors(self, camera_id: str) -> list[dict[str, Any]]:
        """Read fresh discovery only; never resolve a device or start a watcher."""
        runtime = self._runtimes.get(camera_id)
        if (runtime is None or not runtime.available or runtime.descriptors_loaded_at_ts <= 0
                or not 0 <= time.time() - runtime.descriptors_loaded_at_ts <= self._descriptors_ttl_s):
            return []
        return [descriptor.as_dict() for descriptor in runtime.descriptors
                if descriptor.is_property and descriptor.is_boolean and descriptor.item_name]

    async def list_descriptors(self, camera_id: str, *, refresh: bool = False) -> dict[str, Any]:
        runtime = await self._ensure_runtime(camera_id)
        if refresh:
            runtime.descriptors_loaded_at_ts = 0.0
            runtime.descriptors_attempted_at_ts = 0.0
        await self._ensure_descriptors(runtime)
        return {
            "camera_id": runtime.context.camera_id,
            "camera_name": runtime.context.camera_name,
            "available": bool(runtime.available or runtime.descriptors),
            "error": runtime.error,
            "event_xaddr": runtime.event_xaddr,
            "queried_at": runtime.descriptors_loaded_at_ts,
            "observation": "connected" if runtime.task is not None and not runtime.task.done() and runtime.watch_available else "not_observed",
            "observed_states": [state.as_dict(camera_id=camera_id, available=runtime.watch_available, error="")
                                for state in runtime.states.values()],
            "transport": self._transport_status(runtime),
            "events": [descriptor.as_dict() for descriptor in runtime.descriptors],
            "boolean_states": [
                descriptor.as_dict()
                for descriptor in runtime.descriptors
                if descriptor.is_property and descriptor.is_boolean and descriptor.item_name
            ],
        }

    async def snapshot(self, *, camera_id: str, topic: str, item_name: str,
                       qualified_topic: str = "", source: dict[str, str] | None = None,
                       key: dict[str, str] | None = None) -> dict[str, Any]:
        runtime = await self._ensure_runtime(camera_id)
        await self._ensure_watch(runtime)
        candidates = [state for state in runtime.states.values()
                      if state.descriptor.topic == topic and state.descriptor.item_name == item_name
                      and (not qualified_topic or state.descriptor.qualified_topic == qualified_topic)
                      and (source is None or state.source == source)
                      and (key is None or state.key == key)]
        namespaces = {descriptor.qualified_topic for descriptor in runtime.descriptors
                      if descriptor.topic == topic and descriptor.item_name == item_name}
        if not qualified_topic and len(namespaces) > 1:
            candidates = []
        if len(candidates) == 1:
            return {**candidates[0].as_dict(camera_id=runtime.context.camera_id,
                                           available=runtime.watch_available, error=runtime.error),
                    "session": runtime.session}
        return {"camera_id": camera_id, "topic": topic, "item_name": item_name,
                "known": False, "value": None, "available": runtime.watch_available,
                "session": runtime.session,
                "error": "Ambiguous ONVIF property; select its namespace, source and key" if len(candidates) > 1 or (not qualified_topic and len(namespaces) > 1)
                         else runtime.error or "ONVIF property not synchronized"}

    async def recent_events(
        self,
        *,
        camera_id: str,
        after_sequence: int = 0,
        limit: int = 32,
    ) -> dict[str, Any]:
        runtime = await self._ensure_runtime(camera_id)
        await self._ensure_watch(runtime)
        minimum = max(0, int(after_sequence))
        max_items = max(1, min(256, int(limit)))
        events = [event for event in runtime.events if int(event.sequence) > minimum]
        events = events[:max_items]
        return {
            "camera_id": runtime.context.camera_id,
            "camera_name": runtime.context.camera_name,
            "available": bool(runtime.watch_available),
            "error": runtime.error,
            "last_sequence": int(runtime.sequence),
            "session": runtime.session,
            "transport": self._transport_status(runtime),
            "gap": bool(minimum and runtime.events and minimum < runtime.events[0].sequence - 1),
            "events": [{**event.as_dict(), "ambiguous_topic": len({item.qualified_topic
                        for item in runtime.descriptors if item.topic == event.topic}) > 1} for event in events],
            "occurrence_descriptors": [item.as_dict() for item in runtime.descriptors if not item.is_property],
        }

    @staticmethod
    def _transport_status(runtime: _CameraEventRuntime) -> dict[str, Any]:
        return {"status": "recovering" if runtime.recovering else (
                    "observing" if runtime.watch_available else "waiting"),
                "successful_pulls": runtime.successful_pulls, "failed_pulls": runtime.failed_pulls,
                "subscriptions": runtime.subscriptions, "last_pull_at": runtime.last_pull_ts or None,
                "received_messages": runtime.sequence, "transitions": runtime.transitions,
                "queue_overflows": runtime.queue_overflows,
                "unsubscribe": runtime.unsubscribe_status}

    async def _ensure_runtime(self, camera_id: str) -> _CameraEventRuntime:
        cid = str(camera_id or "").strip()
        if not cid:
            raise OnvifError("camera_id is required")
        async with self._manager_lock:
            runtime = self._runtimes.get(cid)
            resolved = self._resolve_context(cid)
            if asyncio.iscoroutine(resolved):
                resolved = await resolved
            if not isinstance(resolved, OnvifCameraEventContext):
                raise OnvifError("Invalid ONVIF event camera context")
            if runtime is not None and runtime.context == resolved:
                return runtime
            if runtime is not None:
                runtime.stop_event.set()
                if runtime.task is not None:
                    runtime.task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await runtime.task
            sequence = runtime.sequence if runtime is not None else 0
            runtime = _CameraEventRuntime(context=resolved, sequence=sequence)
            self._runtimes[cid] = runtime
            return runtime

    async def _ensure_descriptors(self, runtime: _CameraEventRuntime) -> None:
        now = time.time()
        if (
            runtime.descriptors_loaded_at_ts > 0.0
            and (now - runtime.descriptors_loaded_at_ts) <= self._descriptors_ttl_s
        ):
            return
        async with runtime.lock:
            now = time.time()
            if runtime.error and now - runtime.descriptors_attempted_at_ts < self._reconnect_backoff_s:
                raise OnvifError("ONVIF discovery retry is backing off")
            if (
                runtime.descriptors_loaded_at_ts > 0.0
                and (now - runtime.descriptors_loaded_at_ts) <= self._descriptors_ttl_s
            ):
                return
            runtime.descriptors_attempted_at_ts = time.time()
            try:
                events_client = self._events_client(runtime.context)
                event_xaddr = await events_client.resolve_event_xaddr(runtime.context.event_xaddr)
                descriptors = await events_client.get_event_properties(event_xaddr)
            except Exception as exc:  # noqa: BLE001
                runtime.available = False
                runtime.error = f"{exc.__class__.__name__}: ONVIF event transport unavailable"
                raise
            runtime.event_xaddr = event_xaddr
            runtime.descriptors = descriptors
            valid = {(item.qualified_topic or item.topic, item.item_name) for item in descriptors
                     if item.is_property and item.is_boolean}
            runtime.states = {key: state for key, state in runtime.states.items() if key[:2] in valid}
            runtime.descriptors_loaded_at_ts = time.time()
            runtime.available = True
            runtime.error = ""

    async def _ensure_watch(self, runtime: _CameraEventRuntime) -> None:
        await self._ensure_descriptors(runtime)
        runtime.last_consumer_monotonic = time.monotonic()
        if runtime.started and runtime.task is not None and not runtime.task.done():
            return
        async with runtime.lock:
            if runtime.started and runtime.task is not None and not runtime.task.done():
                return
            runtime.stop_event = asyncio.Event()
            runtime.task = asyncio.create_task(
                self._pull_loop(runtime),
                name=f"onvif-events[{runtime.context.camera_id}]",
            )
            runtime.started = True

    def _events_client(self, context: OnvifCameraEventContext) -> OnvifEventsClient:
        return OnvifEventsClient(
            OnvifClient(
                xaddr=context.xaddr,
                username=context.username,
                password=context.password,
                timeout_s=context.timeout_s,
                auth_mode="auto",
            )
        )

    def _find_descriptor(
        self,
        runtime: _CameraEventRuntime,
        *,
        topic: str,
        item_name: str,
    ) -> OnvifEventDescriptor | None:
        wanted_topic = str(topic or "").strip()
        wanted_item = str(item_name or "").strip()
        for descriptor in runtime.descriptors:
            if descriptor.topic == wanted_topic and descriptor.item_name == wanted_item:
                return descriptor
        return None

    def _watch_has_demand(self, runtime: _CameraEventRuntime) -> bool:
        return time.monotonic() - runtime.last_consumer_monotonic < _WATCH_IDLE_SECONDS

    async def _pull_loop(self, runtime: _CameraEventRuntime) -> None:
        while not runtime.stop_event.is_set() and self._watch_has_demand(runtime):
            subscription_xaddr = ""
            operation = "CreatePullPointSubscription"
            try:
                events_client = self._events_client(runtime.context)
                if not runtime.event_xaddr:
                    runtime.event_xaddr = await events_client.resolve_event_xaddr(runtime.context.event_xaddr)
                subscription = await events_client.create_pull_point_subscription(runtime.event_xaddr)
                subscription_xaddr = subscription.address
                runtime.subscriptions += 1
                runtime.session += 1
                operation = "SetSynchronizationPoint"
                await events_client.set_synchronization_point(subscription_xaddr)
                runtime.available = True
                runtime.watch_available = False
                runtime.error = ""
                renew_at = time.monotonic() + _renew_delay(subscription)
                last_progress = time.monotonic()
                while not runtime.stop_event.is_set() and self._watch_has_demand(runtime):
                    remaining = renew_at - time.monotonic()
                    if remaining <= 0 or (runtime.recovering and
                            time.monotonic() - last_progress >= _PULL_RECOVERY_SECONDS):
                        operation = "Renew"
                        renewed = await events_client.renew(subscription_xaddr)
                        renew_at = time.monotonic() + _renew_delay(renewed)
                        last_progress = time.monotonic()
                        if runtime.recovering:
                            operation = "SetSynchronizationPoint"
                            await events_client.set_synchronization_point(subscription_xaddr)
                        remaining = renew_at - time.monotonic()
                    operation = "PullMessages"
                    try:
                        messages = await events_client.pull_messages(
                            subscription_xaddr,
                            timeout_s=min(self._pull_timeout_s, max(0.1, remaining)),
                            message_limit=32,
                        )
                    except OnvifTransientTransportError:
                        runtime.failed_pulls += 1
                        runtime.watch_available = False
                        if not runtime.recovering:
                            # Invalidate the gap once, before receiving the recovery batch.
                            runtime.states.clear()
                            runtime.session += 1
                        runtime.recovering = True
                        runtime.error = "ONVIF PullMessages connection interrupted; retrying the same subscription"
                        await self._sleep_or_stop(runtime, min(1.0, self._reconnect_backoff_s))
                        continue
                    runtime.successful_pulls += 1
                    runtime.last_pull_ts = time.time()
                    last_progress = time.monotonic()
                    if runtime.recovering:
                        # Keep newly received properties; synchronize the still-unknown ones.
                        self._record_messages(runtime, messages)
                        operation = "SetSynchronizationPoint"
                        await events_client.set_synchronization_point(subscription_xaddr)
                        runtime.recovering = False
                        runtime.watch_available = True
                        runtime.error = ""
                        continue
                    runtime.recovering = False
                    runtime.watch_available = True
                    runtime.error = ""
                    self._record_messages(runtime, messages)
                    if not messages:
                        await self._sleep_or_stop(runtime, 0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                runtime.available = False
                runtime.watch_available = False
                if operation == "PullMessages" and not isinstance(exc, OnvifTransientTransportError):
                    runtime.failed_pulls += 1
                runtime.error = f"{exc.__class__.__name__}: ONVIF {operation} unavailable"
                await self._sleep_or_stop(runtime, self._reconnect_backoff_s)
            finally:
                # A new subscription must synchronize before old boolean state can be used.
                runtime.available = False
                runtime.watch_available = False
                runtime.recovering = False
                runtime.states.clear()
                runtime.events.clear()
                if subscription_xaddr:
                    try:
                        await self._events_client(runtime.context).unsubscribe(subscription_xaddr)
                        runtime.unsubscribe_status = "confirmed"
                    except Exception:
                        runtime.unsubscribe_status = "unconfirmed"

    async def _sleep_or_stop(self, runtime: _CameraEventRuntime, seconds: float) -> None:
        try:
            await asyncio.wait_for(runtime.stop_event.wait(), timeout=max(0.0, float(seconds)))
        except TimeoutError:
            return

    def _record_messages(self, runtime: _CameraEventRuntime, messages: list[OnvifEventMessage]) -> None:
        if messages:
            runtime.available = True
            runtime.watch_available = True
            runtime.error = ""
        for message in messages:
            runtime.sequence += 1
            event = OnvifEventMessage(
                sequence=runtime.sequence,
                topic=message.topic,
                qualified_topic=message.qualified_topic,
                operation=message.operation,
                utc_time=message.utc_time,
                source=dict(message.source),
                key=dict(message.key),
                data=dict(message.data),
                received_at_ts=message.received_at_ts or time.time(),
                received_monotonic=time.monotonic(),
            )
            descriptors = [item for item in runtime.descriptors if item.topic == event.topic
                           and (not item.qualified_topic or item.qualified_topic == event.qualified_topic)]
            property_values = {item.is_property for item in descriptors}
            changed, accepted = self._update_states(runtime, event)
            runtime.transitions += int(changed)
            runtime.queue_overflows += int(len(runtime.events) == runtime.events.maxlen)
            runtime.events.append(replace(event, session=runtime.session, state_changed=changed, state_accepted=accepted,
                                          is_property=next(iter(property_values)) if len(property_values) == 1 else None))

    def _update_states(self, runtime: _CameraEventRuntime, event: OnvifEventMessage) -> tuple[bool, bool]:
        changed = False
        accepted = False
        for descriptor in runtime.descriptors:
            if descriptor.topic != event.topic or (descriptor.qualified_topic
                    and descriptor.qualified_topic != event.qualified_topic):
                continue
            if not descriptor.is_property or not descriptor.is_boolean or not descriptor.item_name:
                continue
            if descriptor.item_name not in event.data and event.operation.lower() != "deleted":
                continue
            value = _parse_bool(event.data.get(descriptor.item_name))
            if value is None and event.operation.lower() != "deleted":
                continue
            key = (descriptor.qualified_topic or descriptor.topic, descriptor.item_name,
                   tuple(sorted(event.source.items())), tuple(sorted(event.key.items())))
            if key not in runtime.states and len(runtime.states) >= 512:
                runtime.states.pop(next(iter(runtime.states)))
            state = runtime.states.setdefault(key, _OnvifBooleanState(descriptor=descriptor))
            now = event.received_at_ts or time.time()
            try:
                camera_time = datetime.fromisoformat(event.utc_time.replace("Z", "+00:00"))
                if camera_time.tzinfo is None:
                    camera_time = None
            except ValueError:
                camera_time = None
            if camera_time is not None:
                if state.last_camera_time is not None and camera_time < state.last_camera_time:
                    continue
                state.last_camera_time = camera_time
            accepted = True
            if state.value is None or state.value != value:
                state.last_changed_ts = now
                changed = changed or state.value is not None
                if value is False:
                    state.last_inactive_monotonic = time.monotonic()
            if event.operation.lower() == "deleted":
                value = None
                state.last_active_ts = 0.0
                state.last_active_monotonic = None
            elif value is True:
                state.last_active_ts = now
                state.last_active_monotonic = time.monotonic()
            state.value = value
            state.last_event_ts = now
            state.operation = event.operation
            state.source = dict(event.source)
            state.key = dict(event.key)
        return changed, accepted
