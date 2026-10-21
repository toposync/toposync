from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic, payload_path_hint

from ..registry import build_default_model_registry
from .runtime import VisualStateRuntime
from .schemas import VisualStateConfig


def visual_state_cadence_diagnostics(config, context):
    diagnostics = []
    intervals = [("visual_state", config.evaluation_interval_ms)]
    limits = []

    def warning(code, message, suggestion, node_id):
        diagnostics.append(OperatorDiagnostic(severity="warning", code=code, message=message,
            suggestion=suggestion, details={"upstream_node_id": node_id, "rapid_response_qualified": False}))

    for node in context.get("upstream_nodes", []):
        operator = node["operator_id"]
        settings = node.get("normalized_config", {})
        identifier = node["node_id"]
        if operator == "camera.source":
            if settings.get("fps"):
                intervals.append((identifier, 1000 / settings["fps"]))
            else:
                warning("visual_state_cadence_unverified", "The camera frame cadence is not explicitly configured.",
                        "Choose a frame rate compatible with confirmation and measure the delivered cadence.", identifier)
            if settings.get("observation_window_id"):
                warning("visual_state_event_dependency", "The source depends on an observation window.",
                        "Use a continuous source for periodic reconciliation; keeping capture alive does not guarantee delivery.", identifier)
        elif operator in {"camera.motion_gate", "camera.motion_bgsub_adaptive", "camera.motion_sample_bg"}:
            if not settings.get("emit_when_idle", False):
                warning("visual_state_motion_dependency", "Motion filtering can hide a state change indefinitely.",
                        "Enable idle-frame delivery and bound its cadence with the existing velocity throttle.", identifier)
        elif operator in {"camera.onvif_state_gate", "camera.onvif_event_source", "core.filter", "core.debounce"}:
            warning("visual_state_event_dependency", "An upstream gate or filter can suppress reconciliation frames.",
                    "Keep event triggers optional; verify a continuous path for initialization and recovery.", identifier)
        if operator == "core.fps_reducer":
            intervals.append((identifier, 1000 / settings.get("target_fps", 5)))
            limits.append(identifier)
        elif operator == "core.throttle":
            intervals.append((identifier, settings.get("interval_seconds", 15) * 1000))
            limits.append(identifier)
        elif operator == "core.velocity_throttle":
            intervals.append((identifier, max(settings.get("moving_interval_seconds", 2),
                                               settings.get("stopped_interval_seconds", 300)) * 1000))
            limits.append(identifier)
            if settings.get("moving_field") != "metadata.motion_gate_open" or settings.get("key_field") != "stream_id":
                warning("visual_state_cadence_unverified", "The movement throttle is not bound to the camera stream and motion flag.",
                        "For motion-aware image cadence, use stream_id and metadata.motion_gate_open.", identifier)
    if len(limits) > 1:
        warning("visual_state_serial_limits", "Several upstream rate limits can compound delays.",
                "Review the combined cadence; keep one upstream limiter and verify actual observation gaps.", limits[-1])
    identifier, interval = max(intervals, key=lambda item: item[1])
    if interval > config.temporal.maximum_gap_ms or interval >= config.temporal.validity_ms:
        warning("visual_state_cadence_too_slow",
                f"A configured observation interval of {interval:g} ms exceeds confirmation or validity limits.",
                "Reduce the interval or explicitly revise the temporal policy; fast response remains unqualified.", identifier)
    if config.strategy == "direct":
        warning("visual_state_ai_latency_unverified", "Direct AI response time has not been qualified for this sensor.",
                "Measure the selected profile; the effective deadline cannot exceed the image's remaining useful age.", "visual_state")
        for period, duration_ms in (("minute", 60_000), ("hour", 3_600_000), ("day", 86_400_000)):
            count = getattr(config.ai_limits, f"requests_per_{period}")
            if count is not None and (duration_ms / count > config.temporal.maximum_gap_ms
                                      or duration_ms / count >= config.temporal.validity_ms):
                warning("visual_state_ai_budget_too_low", "The sensor AI budget cannot sustain the configured confirmation and validity intervals.",
                        "Choose a compatible temporal policy and explicit budget; shared profile limits may reduce the effective cadence further.", "visual_state")
                break
    return diagnostics


def visual_state_diagnostics(config, context):
    diagnostics = []
    try:
        parsed = VisualStateConfig.model_validate(config)
        diagnostics = visual_state_cadence_diagnostics(parsed, context)
        parsed.validate_activation()
        if context.get("processing_server_id", "local") != "local":
            raise ValueError("visual sensors currently require local execution")
        if parsed.strategy != "direct":
            manifest = build_default_model_registry().get_manifest(parsed.model_id)
            if (manifest is None or manifest.task != "embedding" or manifest.postprocess.type != "image_embedding"
                    or not manifest.resolve_artifact_path().is_file()):
                raise ValueError("the configured image embedding model is unavailable")
    except (ValueError, OSError) as error:
        return [*diagnostics, OperatorDiagnostic(severity="error", code="visual_state_not_ready", message=str(error),
                                   suggestion="Configure compatible references, a verified model and its calibrated comparison policy.")]
    return diagnostics


def register_visual_state_operator(registry):
    if registry.get("vision.visual_state") is not None:
        return
    registry.register_operator(
        operator_id="vision.visual_state", config_model=VisualStateConfig,
        description="Experimental visual state with private examples, optional multimodal analysis, temporal confirmation and expiring evidence.",
        defaults={"sensor_id": "unconfigured", "reference_revision": "unconfigured", "model_revision": "unconfigured",
                  "classes": [{"class_id": key, "key": key, "label": label}
                              for key, label in (("state_1", "State 1"), ("state_2", "State 2"))]},
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out", "preserves_input_contract": False}],
        input_modalities=["video"], output_modalities=["data"],
        default_input_policy={"queue": {"max_items": 1, "drop_policy": "latest_only"}},
        default_output_policy={"queue": {"max_items": 1, "drop_policy": "latest_only"}},
        execution_mode="thread_pool", max_concurrency=1, share_strategy="never",
        state_kind="stateful_per_stream", resource_kind="vision_model", ordering="strict",
        preserves_lifecycle=False, capabilities=["vision"], produces_payload_keys=["visual_state"],
        expression_hints=[payload_path_hint("payload.visual_state", value_type="object",
                                           description="Visual sensor state, evidence revision and remaining validity.")],
        ui={"pipeline_group": "vision", "pipeline_level": "advanced"}, owner="com.toposync.vision",
        runtime_factory=VisualStateRuntime, diagnostics_factory=visual_state_diagnostics,
    )
