from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, SinkRuntime
from toposync.runtime.pipelines.operator_registry import (
    OperatorRegistry,
    metadata_path_hint,
    payload_path_hint,
)
from toposync.runtime.pipelines.runtime import Packet

from .bindings import attention_bindings, live_observer_binding_issue
from .constants import EXTENSION_ID, OPERATOR_ID_REQUEST
from .controller import PtzAttentionController
from .models import (
    AttentionIntent,
    AttentionProfile,
    AttentionTarget,
    PtzAttentionRequestConfig,
    effective_mode,
)


def _packet_value(packet: Packet, path: str) -> Any:
    root, _, tail = str(path or "").partition(".")
    current: Any = (
        packet.payload if root == "payload" else packet.metadata if root == "metadata" else None
    )
    for part in tail.split("."):
        if not part:
            continue
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _target_from_packet(
    packet: Packet, config: PtzAttentionRequestConfig
) -> AttentionTarget | None:
    candidates = (
        ("world_envelope", _packet_value(packet, config.world_envelope_field)),
        ("world_anchor", _packet_value(packet, config.world_anchor_field)),
        ("bbox01", _packet_value(packet, config.bbox01_field)),
    )
    for key, raw in candidates:
        if raw is None:
            continue
        try:
            return AttentionTarget.model_validate({key: raw})
        except ValidationError:
            continue
    return None


class PtzAttentionRequestRuntime(SinkRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        controller: PtzAttentionController,
    ) -> None:
        self.config = PtzAttentionRequestConfig.model_validate(config)
        self.dependencies = dependencies
        self.controller = controller
        self._event_types: dict[str, str] = {}

    async def process_packet(self, packet: Packet, context: Any) -> list[Packet]:
        profile = self.controller.store.get_profile(self.config.profile_id)
        event_id = str(
            _packet_value(packet, self.config.event_id_field) or packet.stream_id
        ).strip()
        lifecycle = packet.lifecycle.value
        cached_event_type = self._event_types.get(event_id, "")
        event_type = str(
            self.config.event_type
            or (cached_event_type if lifecycle != "open" else "")
            or _packet_value(packet, self.config.event_type_field)
            or ""
        ).strip()
        if profile is None:
            self.controller.record_operator_rejection(
                profile_id=self.config.profile_id,
                pipeline_name=context.pipeline_name,
                node_id=context.node_id,
                event_id=event_id,
                event_type=event_type,
                reason="profile_not_found",
            )
            return []
        observer_issue = await self._live_observer_binding_issue(profile, context)
        if observer_issue is not None:
            self.controller.record_operator_rejection(
                profile_id=profile.id,
                pipeline_name=context.pipeline_name,
                node_id=context.node_id,
                event_id=event_id,
                event_type=event_type,
                reason=observer_issue,
            )
            event_key = (
                f"{profile.ptz_device_id}:{context.pipeline_name}:{context.node_id}:{event_id}"
            )
            cached_event_known = bool(event_id and event_id in self._event_types)
            close_event_type = cached_event_type or event_type
            event_was_active = bool(
                event_id
                and any(
                    status.active_event_key == event_key
                    for status in self.controller.status(profile_id=profile.id)
                )
            )
            should_forward_close = bool(
                event_id
                and close_event_type
                and (lifecycle == "close" or cached_event_known or event_was_active)
            )
            if should_forward_close:
                now = time.time()
                safe_close = AttentionIntent(
                    key=event_key,
                    event_id=event_id,
                    event_type=close_event_type,
                    profile_id=profile.id,
                    ptz_device_id=profile.ptz_device_id,
                    pipeline_name=context.pipeline_name,
                    node_id=context.node_id,
                    lifecycle="close",
                    priority=0,
                    target=None,
                    preferred_view_id="",
                    event_at=float(packet.created_at or now),
                    received_at=now,
                )
                await self.controller.submit_intent(safe_close)
                self._event_types.pop(event_id, None)
                if event_was_active:
                    current = self.controller.status(profile_id=profile.id)
                    if current and current[0].state in {"ACQUIRING", "FOCUSED", "GRACE"}:
                        await self.controller.return_home(
                            profile.id,
                            reason="observer_binding_safety_interlock",
                        )
            return []
        if not event_id:
            self.controller.record_operator_rejection(
                profile_id=profile.id,
                pipeline_name=context.pipeline_name,
                node_id=context.node_id,
                event_id="",
                event_type=event_type,
                reason="event_id_missing",
            )
            return []
        if not event_type:
            self.controller.record_operator_rejection(
                profile_id=profile.id,
                pipeline_name=context.pipeline_name,
                node_id=context.node_id,
                event_id=event_id,
                event_type="",
                reason="event_type_missing",
            )
            return []

        if lifecycle == "open":
            if event_id not in self._event_types and len(self._event_types) >= 4096:
                self._event_types.pop(next(iter(self._event_types)))
            self._event_types[event_id] = event_type
        target = _target_from_packet(packet, self.config) if lifecycle == "open" else None
        if lifecycle == "open" and target is None:
            self.controller.record_operator_rejection(
                profile_id=profile.id,
                pipeline_name=context.pipeline_name,
                node_id=context.node_id,
                event_id=event_id,
                event_type=event_type,
                reason="target_missing_or_invalid",
            )
            return []
        preferred_view_id = str(
            _packet_value(packet, self.config.preferred_view_id_field) or ""
        ).strip()
        now = time.time()
        intent = AttentionIntent(
            key=f"{profile.ptz_device_id}:{context.pipeline_name}:{context.node_id}:{event_id}",
            event_id=event_id,
            event_type=event_type,
            profile_id=profile.id,
            ptz_device_id=profile.ptz_device_id,
            pipeline_name=context.pipeline_name,
            node_id=context.node_id,
            lifecycle=lifecycle,
            priority=0,
            target=target,
            preferred_view_id=preferred_view_id,
            event_at=float(packet.created_at or now),
            received_at=now,
        )
        await self.controller.submit_intent(intent)
        if lifecycle == "close":
            self._event_types.pop(event_id, None)
        return []

    async def _live_observer_binding_issue(
        self,
        profile: AttentionProfile,
        context: Any,
    ) -> str | None:
        if effective_mode(profile) != "live_preset":
            return None
        get_config = getattr(self.dependencies.config_store, "get_config", None)
        if not callable(get_config):
            return "observer_binding_check_unavailable"
        try:
            app_config = await get_config()
        except Exception:
            return "observer_binding_check_unavailable"
        return live_observer_binding_issue(
            profile,
            attention_bindings(list(getattr(app_config, "pipelines", []) or [])),
            pipeline_name=str(context.pipeline_name or "").strip(),
            node_id=str(context.node_id or "").strip(),
            require_effective_binding=True,
        )


def register_pipeline_operators(
    registry: OperatorRegistry,
    controller: PtzAttentionController,
) -> None:
    if registry.get(OPERATOR_ID_REQUEST) is not None:
        return
    defaults = PtzAttentionRequestConfig(profile_id="attention_profile").model_dump(mode="json")
    registry.register_operator(
        operator_id=OPERATOR_ID_REQUEST,
        description="Submits lifecycle-aware semantic attention intents to the global PTZ arbiter.",
        config_model=PtzAttentionRequestConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["sink", "origin_only", "ptz", "lifecycle"],
        defaults=defaults,
        state_kind="external_side_effect",
        resource_kind="camera",
        pressure_behavior="block",
        default_key_path="payload.event_id",
        idempotency_key_hint="payload.event_id",
        share_strategy="never",
        ordering="strict",
        preserves_lifecycle=True,
        can_drop_updates=True,
        expression_hints=[
            payload_path_hint(
                "payload.event_type",
                value_type="string",
                description="Event type governed by the profile policy.",
            ),
            payload_path_hint(
                "payload.world_envelope",
                value_type="object",
                description="World-space group envelope.",
            ),
            payload_path_hint(
                "payload.world_anchor",
                value_type="object",
                description="World-space target anchor.",
            ),
            payload_path_hint(
                "payload.subject.bbox01",
                value_type="array",
                description="Normalized target bounding box.",
            ),
            metadata_path_hint(
                "metadata.event_type",
                value_type="string",
                description="Optional event type source.",
            ),
        ],
        owner=EXTENSION_ID,
        runtime_factory=lambda config, dependencies: PtzAttentionRequestRuntime(
            config,
            dependencies,
            controller,
        ),
    )
