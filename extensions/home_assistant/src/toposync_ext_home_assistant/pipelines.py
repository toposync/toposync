from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, SinkRuntime
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Lifecycle, Packet


_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


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


def _default_tag(packet: Packet, *, node_id: str) -> str:
    event_token = _first_non_empty(
        packet.payload.get("event_id"),
        packet.payload.get("correlation_id"),
        packet.payload.get("tracking_id"),
        packet.stream_id,
    )
    camera_id = _first_non_empty(packet.payload.get("camera_id"), "-")
    raw = f"ha_notify:{node_id}:camera:{camera_id}:event:{event_token}"
    if len(raw) <= 240:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"ha_notify:{node_id}:camera:{camera_id}:event:{digest}"


class HomeAssistantNotifyRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = HomeAssistantNotifyConfig.model_validate(config)
        self._dependencies = dependencies

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
        tag = self._tag(packet, context)
        lifecycle = packet.lifecycle
        should_send = self._should_send(lifecycle)

        if lifecycle == Lifecycle.CLOSE and not should_send:
            if self._config.close_behavior == "clear":
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
            return []

        title = _render_template(packet, self._config.title).strip() or _default_title(packet)
        message = _render_template(packet, self._config.message).strip() or _default_message(packet) or title
        payload: dict[str, Any] = {
            "message": message,
            "data": {"tag": tag},
        }
        if title:
            payload["title"] = title

        await services.call(
            "home_assistant.call_service",
            server_id=self._config.server_id,
            domain=domain,
            service_name=service,
            data=payload,
        )
        return []

    def _should_send(self, lifecycle: Lifecycle) -> bool:
        mode = self._config.notify_when
        if mode == "all":
            return True
        if mode == "open":
            return lifecycle == Lifecycle.OPEN
        if mode == "open_update":
            return lifecycle in {Lifecycle.OPEN, Lifecycle.UPDATE}
        return lifecycle == Lifecycle.CLOSE

    def _tag(self, packet: Packet, context) -> str:
        if self._config.tag_template:
            rendered = _render_template(packet, self._config.tag_template).strip()
            if rendered:
                return rendered[:512]
        node_id = str(getattr(context, "node_id", "") or "home_assistant_notify").strip() or "home_assistant_notify"
        return _default_tag(packet, node_id=node_id)


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
