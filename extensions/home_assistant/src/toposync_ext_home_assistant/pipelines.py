from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, SinkRuntime
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Lifecycle, Packet


_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
_ENTITY_KEY_SAFE_RE = re.compile(r"[^a-z0-9_]+")


def _normalize_entity_key(value: str) -> str:
    raw = str(value or "").strip().lower()
    raw = raw.replace("-", "_").replace(" ", "_")
    normalized = _ENTITY_KEY_SAFE_RE.sub("_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        return ""
    if normalized[0].isdigit():
        normalized = f"s_{normalized}"
    return normalized[:80]


class HomeAssistantNotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str = ""
    notify_service: str = ""
    notify_when: Literal["open", "open_update", "close", "all"] = "open"
    close_behavior: Literal["ignore", "clear"] = "ignore"
    title: str = ""
    message: str = ""
    tag_template: str = ""

    @field_validator("server_id", "notify_service", "title", "message", "tag_template")
    @classmethod
    def _trim_fields(cls, value: str) -> str:
        return str(value or "").strip()


class HomeAssistantBooleanStateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str = ""
    target_mode: Literal["managed_state", "existing_input_boolean"] = "managed_state"
    managed_name: str = ""
    managed_entity_key: str = ""
    device_class: Literal["", "motion", "occupancy", "presence", "opening", "problem", "tamper"] = "motion"
    existing_entity_id: str = ""
    boolean_path: str = ""
    shutdown_behavior: Literal["off", "unavailable", "keep"] = "off"

    @field_validator("server_id", "managed_name", "existing_entity_id", "boolean_path")
    @classmethod
    def _trim_fields(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("managed_entity_key")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        return _normalize_entity_key(value)


def _deep_get(container: Any, dotted_key: str) -> Any:
    parts = [part for part in str(dotted_key or "").split(".") if part]
    current: Any = container
    for part in parts:
        if not isinstance(current, dict):
            return None
        if part not in current:
            return None
        current = current.get(part)
    return current


def _resolve_template_value(packet: Packet, key: str) -> Any:
    normalized = str(key or "").strip()
    if not normalized:
        return None
    if normalized.startswith("payload."):
        return _deep_get(packet.payload, normalized[len("payload.") :])
    if normalized.startswith("metadata."):
        return _deep_get(packet.metadata, normalized[len("metadata.") :])
    value = _deep_get(packet.payload, normalized)
    if value is not None:
        return value
    return _deep_get(packet.metadata, normalized)


def _resolve_boolean_value(packet: Packet, key: str) -> bool | None:
    value = _resolve_template_value(packet, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return None


def _render_template(packet: Packet, template: str) -> str:
    raw = str(template or "")
    if not raw:
        return ""

    def _replace(match: re.Match[str]) -> str:
        value = _resolve_template_value(packet, match.group(1))
        if value is None:
            return ""
        try:
            return str(value)
        except Exception:
            return ""

    return _TEMPLATE_RE.sub(_replace, raw)


def _parse_notify_service(value: str) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "." in raw:
        domain, service = raw.split(".", 1)
    else:
        domain, service = "notify", raw
    normalized_domain = domain.strip().lower()
    normalized_service = service.strip()
    if normalized_domain != "notify" or not normalized_service:
        return None
    if any(ch.isspace() for ch in normalized_service) or "/" in normalized_service:
        return None
    return normalized_domain, normalized_service


def _humanize_compact(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw[:1].upper() + raw[1:]


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _default_title(packet: Packet) -> str:
    category = _first_non_empty(
        packet.payload.get("object_category_label"),
        _deep_get(packet.payload, "detected_object.category"),
        packet.payload.get("area_label"),
    )
    lifecycle = packet.lifecycle
    if category:
        if lifecycle == Lifecycle.CLOSE:
            return f"{_humanize_compact(category)} cleared"
        return f"{_humanize_compact(category)} detected"
    if lifecycle == Lifecycle.CLOSE:
        return "Pipeline event cleared"
    return "Pipeline event"


def _default_message(packet: Packet) -> str:
    camera_name = _first_non_empty(packet.payload.get("camera_name"), _deep_get(packet.payload, "source.name"))
    area_label = _first_non_empty(packet.payload.get("area_label"))
    source_name = _first_non_empty(camera_name, area_label, packet.payload.get("stream_id"))
    if camera_name and area_label:
        return f"{camera_name} • {area_label}"
    if source_name:
        return source_name
    return packet.stream_id


def _packet_time_iso(packet: Packet) -> str:
    try:
        return datetime.fromtimestamp(float(packet.created_at), tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(tz=timezone.utc).isoformat()


class HomeAssistantNotifyRuntime(SinkRuntime):
    _MAX_EVENT_KEYS = 2048

    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = HomeAssistantNotifyConfig.model_validate(config)
        self._dependencies = dependencies
        self._seen_event_keys: OrderedDict[str, None] = OrderedDict()

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        services = self._dependencies.services
        if services is None:
            raise RuntimeError("home_assistant.notify requires PipelineRuntimeDependencies.services")
        if not self._config.server_id:
            return []
        parsed_service = _parse_notify_service(self._config.notify_service)
        if parsed_service is None:
            return []

        domain, service = parsed_service
        tag = self._tag(packet)
        lifecycle = packet.lifecycle
        event_key = self._event_key(packet)
        first_seen = self._mark_event_seen(event_key) if lifecycle != Lifecycle.CLOSE else False
        should_send = self._should_send(lifecycle, first_seen=first_seen)

        if lifecycle == Lifecycle.CLOSE and not should_send:
            self._forget_event(event_key)
            if self._config.close_behavior == "clear" and tag:
                await services.call(
                    "home_assistant.call_service",
                    server_id=self._config.server_id,
                    domain=domain,
                    service_name=service,
                    data={
                        "message": "clear_notification",
                        "data": {"tag": tag},
                    },
                )
            return []

        if not should_send:
            if lifecycle == Lifecycle.CLOSE:
                self._forget_event(event_key)
            return []

        title = _render_template(packet, self._config.title).strip() or _default_title(packet)
        message = _render_template(packet, self._config.message).strip() or _default_message(packet) or title
        payload: dict[str, Any] = {"message": message}
        if title:
            payload["title"] = title
        if tag:
            payload["data"] = {"tag": tag}

        await services.call(
            "home_assistant.call_service",
            server_id=self._config.server_id,
            domain=domain,
            service_name=service,
            data=payload,
        )
        if lifecycle == Lifecycle.CLOSE:
            self._forget_event(event_key)
        return []

    def _should_send(self, lifecycle: Lifecycle, *, first_seen: bool) -> bool:
        mode = self._config.notify_when
        if mode == "all":
            return True
        if mode == "open":
            return lifecycle == Lifecycle.OPEN or (lifecycle == Lifecycle.UPDATE and first_seen)
        if mode == "open_update":
            return lifecycle in {Lifecycle.OPEN, Lifecycle.UPDATE}
        return lifecycle == Lifecycle.CLOSE

    def _tag(self, packet: Packet) -> str | None:
        rendered = _render_template(packet, self._config.tag_template).strip()
        if rendered:
            return rendered[:512]
        return None

    def _event_key(self, packet: Packet) -> str:
        return _first_non_empty(
            packet.payload.get("event_id"),
            packet.payload.get("correlation_id"),
            packet.payload.get("tracking_id"),
            packet.stream_id,
            packet.packet_id,
        )

    def _mark_event_seen(self, key: str) -> bool:
        token = str(key or "").strip()
        if not token:
            return False
        first_seen = token not in self._seen_event_keys
        if not first_seen:
            self._seen_event_keys.move_to_end(token)
            return False
        self._seen_event_keys[token] = None
        self._seen_event_keys.move_to_end(token)
        while len(self._seen_event_keys) > self._MAX_EVENT_KEYS:
            self._seen_event_keys.popitem(last=False)
        return True

    def _forget_event(self, key: str) -> None:
        token = str(key or "").strip()
        if not token:
            return
        self._seen_event_keys.pop(token, None)


class HomeAssistantBooleanStateRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = HomeAssistantBooleanStateConfig.model_validate(config)
        self._dependencies = dependencies
        self._active_streams: set[str] = set()
        self._last_written_state: str | None = None
        self._last_packet: Packet | None = None
        self._last_context: Any | None = None

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        services = self._dependencies.services
        if services is None:
            raise RuntimeError("home_assistant.boolean_state requires PipelineRuntimeDependencies.services")
        if not self._config.server_id:
            return []

        self._last_packet = packet
        self._last_context = context
        changed = self._apply_packet_state(packet)
        if not changed and self._last_written_state is not None:
            return []

        desired_state = "on" if self._active_streams else "off"
        await self._write_state(desired_state, packet=packet, context=context, reason="packet")
        return []

    async def shutdown(self) -> None:
        behavior = self._config.shutdown_behavior
        if behavior == "keep":
            return
        if self._last_written_state not in {"on", "unavailable"} and not self._active_streams:
            return
        packet = self._last_packet
        context = self._last_context
        if packet is None or context is None:
            return
        state = "unavailable" if behavior == "unavailable" else "off"
        await self._write_state(state, packet=packet, context=context, reason="shutdown", force=True)

    def _apply_packet_state(self, packet: Packet) -> bool:
        stream_id = str(packet.stream_id or packet.packet_id or "").strip()
        if not stream_id:
            return False

        before = set(self._active_streams)
        explicit = _resolve_boolean_value(packet, self._config.boolean_path) if self._config.boolean_path else None
        if explicit is True:
            self._active_streams.add(stream_id)
        elif explicit is False:
            self._active_streams.discard(stream_id)
        elif packet.lifecycle == Lifecycle.OPEN:
            self._active_streams.add(stream_id)
        elif packet.lifecycle == Lifecycle.CLOSE:
            self._active_streams.discard(stream_id)
        elif packet.lifecycle == Lifecycle.UPDATE and self._last_written_state is None and stream_id not in self._active_streams:
            self._active_streams.add(stream_id)

        return before != self._active_streams

    async def _write_state(
        self,
        state: str,
        *,
        packet: Packet,
        context: Any,
        reason: str,
        force: bool = False,
    ) -> None:
        services = self._dependencies.services
        if services is None:
            raise RuntimeError("home_assistant.boolean_state requires PipelineRuntimeDependencies.services")
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in {"on", "off", "unavailable"}:
            return
        if not force and normalized_state == self._last_written_state:
            return

        if self._config.target_mode == "existing_input_boolean":
            entity_id = str(self._config.existing_entity_id or "").strip()
            if not entity_id.startswith("input_boolean."):
                return
            if normalized_state == "unavailable":
                return
            await services.call(
                "home_assistant.call_service",
                server_id=self._config.server_id,
                domain="input_boolean",
                service_name="turn_on" if normalized_state == "on" else "turn_off",
                data={"entity_id": entity_id},
            )
            self._last_written_state = normalized_state
            return

        entity_id = self._managed_entity_id(context)
        if not entity_id:
            return
        await services.call(
            "home_assistant.set_state",
            server_id=self._config.server_id,
            entity_id=entity_id,
            state=normalized_state,
            attributes=self._managed_attributes(packet, context, reason=reason),
        )
        self._last_written_state = normalized_state

    def _managed_entity_id(self, context: Any) -> str:
        fallback = _first_non_empty(
            self._config.managed_entity_key,
            self._config.managed_name,
            f"{getattr(context, 'pipeline_name', '')}_{getattr(context, 'node_id', '')}",
            "boolean_state",
        )
        key = _normalize_entity_key(fallback)
        if not key:
            return ""
        return f"binary_sensor.toposync_{key}"

    def _managed_name(self, context: Any) -> str:
        configured = str(self._config.managed_name or "").strip()
        if configured:
            return configured
        pipeline_name = str(getattr(context, "pipeline_name", "") or "").strip()
        node_id = str(getattr(context, "node_id", "") or "").strip()
        if pipeline_name and node_id:
            return f"{pipeline_name} {node_id}"
        return "Toposync boolean state"

    def _managed_attributes(self, packet: Packet, context: Any, *, reason: str) -> dict[str, Any]:
        camera_id = _first_non_empty(
            packet.payload.get("camera_id"),
            _deep_get(packet.payload, "source.camera_id"),
            packet.metadata.get("camera_id"),
            _deep_get(packet.payload, "onvif_event.camera_id"),
        )
        camera_name = _first_non_empty(
            packet.payload.get("camera_name"),
            _deep_get(packet.payload, "source.name"),
            _deep_get(packet.payload, "onvif_event.camera_name"),
        )
        onvif_topic = _first_non_empty(
            _deep_get(packet.payload, "onvif_event.topic"),
            _deep_get(packet.payload, "onvif.topic"),
        )
        onvif_item = _first_non_empty(
            _deep_get(packet.payload, "onvif_event.item_name"),
            _deep_get(packet.payload, "onvif.item_name"),
        )
        attrs: dict[str, Any] = {
            "friendly_name": self._managed_name(context),
            "toposync_managed": True,
            "toposync_pipeline": str(getattr(context, "pipeline_name", "") or "").strip(),
            "toposync_node": str(getattr(context, "node_id", "") or "").strip(),
            "toposync_operator": "home_assistant.boolean_state",
            "toposync_reason": reason,
            "last_packet_id": packet.packet_id,
            "last_stream_id": packet.stream_id,
            "last_lifecycle": packet.lifecycle.value,
            "last_seen": _packet_time_iso(packet),
            "active_stream_count": len(self._active_streams),
        }
        if self._config.device_class:
            attrs["device_class"] = self._config.device_class
        if camera_id:
            attrs["camera_id"] = camera_id
        if camera_name:
            attrs["camera_name"] = camera_name
        if onvif_topic:
            attrs["onvif_topic"] = onvif_topic
        if onvif_item:
            attrs["onvif_item"] = onvif_item
        return attrs


def register_home_assistant_pipeline_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="home_assistant.notify",
        description="Sends mobile push notifications through a Home Assistant notify service.",
        config_model=HomeAssistantNotifyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["notifications", "origin_only", "sink", "home_assistant"],
        defaults=HomeAssistantNotifyConfig().model_dump(),
        share_strategy="never",
        owner="com.toposync.home_assistant",
        runtime_factory=lambda config, deps: HomeAssistantNotifyRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="home_assistant.boolean_state",
        description="Publishes pipeline lifecycle events as a boolean state in Home Assistant.",
        config_model=HomeAssistantBooleanStateConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["home_assistant", "sink", "realtime"],
        defaults=HomeAssistantBooleanStateConfig().model_dump(),
        produces_payload_keys=[],
        share_strategy="never",
        owner="com.toposync.home_assistant",
        ui={
            "pipeline_group": "output",
            "pipeline_level": "basic",
            "pipeline_order": 35,
            "aliases": ["Home Assistant", "boolean", "binary sensor", "input boolean"],
        },
        runtime_factory=lambda config, deps: HomeAssistantBooleanStateRuntime(config, deps),
    )
