"""Pure place/source resolution and graphs built from existing camera and vision operators."""

import math

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.automatic import AutomaticScope
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2

from .pipelines.postprocess import (
    _parse_mapping_control_point_sets_from_props,
    resolve_composition_area,
)
from .settings import CamerasExtensionSettings, get_default_camera_source
from .visual_region_models import VisualRegionConfig


class ObservationCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    model_id: str = "rfdetr_det_medium"
    categories: list[str] = Field(default_factory=lambda: ["person"], min_length=1, max_length=32)
    minimum_duration_seconds: float = Field(default=1, ge=0, le=3600)
    evidence_timeout_seconds: float = Field(default=5, gt=0, le=60)
    max_speed_mps: float = Field(default=1 / 3.6, ge=0, le=100)
    association: dict | None = None
    resource_parameters: dict = Field(default_factory=dict)
    schedule: dict | None = None


def resolve_sources(scope, configuration):
    settings = CamerasExtensionSettings.model_validate(
        configuration.settings.extensions.get("com.toposync.cameras", {})
    )
    area = None
    candidates = set(scope.camera_ids)
    if scope.kind == "physical_area":
        area = resolve_composition_area(
            configuration.compositions, composition_id=scope.composition_id, area_id=scope.area_id
        )
        if area is None:
            return [], None, "area_unavailable"
        mapped = {
            element.props.get("camera_id")
            for composition in configuration.compositions
            if composition.id == scope.composition_id
            for element in composition.elements
            if any(
                len(item.control_points) >= 4
                for item in _parse_mapping_control_point_sets_from_props(element.props)
            )
        }
        candidates = candidates or mapped
        candidates &= mapped
    selected = []
    for camera in settings.devices:
        if (
            camera.id not in candidates
            or camera.id in scope.excluded_camera_ids
            or not camera.enabled
        ):
            continue
        source = get_default_camera_source(
            camera.model_dump(mode="json"), kind="video", enabled_only=True
        )
        if source:
            selected.append(
                {
                    "camera_id": camera.id,
                    "camera_name": camera.name or camera.id,
                    "source_id": source["id"],
                    "source_name": source.get("name") or source["id"],
                    "clock_domain": camera.clock_domain,
                    "coverage": "mapped_candidate" if area else "whole_frame",
                }
            )
    return selected, area, None if selected else "source_unavailable"


def plan_observation(*, intent, configuration):
    condition = ObservationCondition.model_validate(intent.condition)
    sources, area, reason = resolve_sources(intent.scope, configuration)
    limitations = ["continuous_full_frame_observation", "no_motion_gate"]
    if area:
        limitations.append("mapping_is_not_a_coverage_certificate")

    def unavailable(reason):
        return {
            "sources": sources,
            "state": "unavailable",
            "reason": reason,
            "limitations": limitations,
        }

    if reason:
        return unavailable(reason)
    if (
        intent.capability in {"vision.entry", "vision.stationary", "vision.route"}
        and intent.scope.kind == "camera"
    ):
        return unavailable("spatial_scope_required")
    if intent.capability in {"vision.stationary", "vision.route"} and area is None:
        return unavailable("metric_area_required")
    if intent.capability == "vision.route" and len(sources) < 2:
        return unavailable("multiple_sources_required")
    association = condition.association
    if len(sources) > 1:
        if not association:
            return unavailable("association_preparation_required")
        declared = {
            (source.get("camera_id"), source.get("source_id")): source
            for source in association.get("sources", [])
        }
        if any((source["camera_id"], source["source_id"]) not in declared for source in sources):
            return unavailable("association_sources_mismatch")
        selected = {(source["camera_id"], source["source_id"]) for source in sources}
        association = {
            **association,
            "sources": [source for key, source in declared.items() if key in selected],
        }
        cameras = {source["camera_id"] for source in sources}
        association["transitions"] = [
            item
            for item in association.get("transitions", [])
            if item.get("source_camera") in cameras and item.get("target_camera") in cameras
        ]
        if any(
            source.get("identity_policy") != "anonymous_continuity"
            for source in association["sources"]
        ):
            return unavailable("anonymous_continuity_required")
        profiles = association["sources"]
        if any(
            not profile.get("clock_domain") or profile.get("clock_domain") != source["clock_domain"]
            for source in sources
            for profile in profiles
            if (source["camera_id"], source["source_id"])
            == (profile["camera_id"], profile["source_id"])
        ):
            return unavailable("association_clock_unverified")
        if len({profile["clock_domain"] for profile in profiles}) != 1:
            return unavailable("association_clock_unverified")
        for profile in profiles:
            for field, maximum in (
                ("clock_uncertainty_seconds", 60),
                ("position_error_metres", 100),
            ):
                value = profile.get(field)
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not 0 <= value <= maximum
                ):
                    return unavailable("association_uncertainty_unverified")
            if profile.get("coordinate_reference") != intent.scope.composition_id:
                return unavailable("association_coordinate_mismatch")
        connected = {sources[0]["camera_id"]}
        for _ in sources:
            for transition in association["transitions"]:
                ends = {transition.get("source_camera"), transition.get("target_camera")}
                if connected.intersection(ends):
                    connected.update(ends)
        if not cameras <= connected:
            return unavailable("association_transitions_required")
    nodes, edges, endpoints = [], [], []

    def add(identifier, operator, config, upstream=None, port="out"):
        nodes.append({"id": identifier, "operator": operator, "config": config})
        if upstream:
            edges.append(
                {"from": {"node": upstream, "port": port}, "to": {"node": identifier, "port": "in"}}
            )
        return identifier

    for index, source in enumerate(sources):
        prefix = f"camera_{index}_"
        tail = add(
            prefix + "source",
            "camera.source",
            {"camera_id": source["camera_id"], "source_id": source["source_id"], "fps": 4},
        )
        # Detection is deliberately independent of rule categories and duration so M2 can share it.
        tail = add(
            prefix + "detect",
            "vision.detect",
            {
                "model_id": condition.model_id,
                "categories": [],
                "confidence_threshold": 0.25,
                "emit_mode": "annotate",
            },
            tail,
        )
        if area:
            tail = add(
                prefix + "mapping",
                "camera.camera_mapping",
                {
                    "camera_id": source["camera_id"],
                    "composition_id": intent.scope.composition_id,
                    "ptz_state_fetch": {"enabled": False},
                },
                tail,
            )
        tail = add(
            prefix + "track",
            "vision.track",
            {
                "tracker_id": "byte_world",
                "pause_when_gate_closed": False,
                "default_interval_seconds": 0.25,
                "use_world_anchor": "auto" if area else "never",
            },
            tail,
        )
        endpoints.append(tail)
    tail = endpoints[0]
    for index, endpoint in enumerate(endpoints[1:]):
        merged = add(f"merge_{index}", "core.merge_streams", {})
        edges.extend(
            [
                {"from": {"node": current, "port": "out"}, "to": {"node": merged, "port": port}}
                for current, port in [(tail, "left"), (endpoint, "right")]
            ]
        )
        tail = merged
    if len(sources) > 1:
        tail = add("association", "vision.associate_entities", association, tail)
    tail = add("categories", "core.category_gate", {"categories": condition.categories}, tail)
    occurrence = {
        "minimum_duration_seconds": condition.minimum_duration_seconds,
        "evidence_timeout_seconds": condition.evidence_timeout_seconds,
    }
    if intent.capability == "vision.stationary":
        tail = add(
            "velocity",
            "camera.velocity_estimation",
            {"filter_mode": "annotate", "stopped_speed_threshold": condition.max_speed_mps},
            tail,
        )
        tail = add(
            "area",
            "camera.area_restriction",
            {
                "composition_id": intent.scope.composition_id,
                "areas": [area.model_dump()],
                "include_area_names": [area.name],
                "drop_when_unmapped": True,
            },
            tail,
        )
        tail = add(
            "stationary",
            "core.stationary_event",
            {
                "min_stationary_seconds": condition.minimum_duration_seconds,
                "max_speed_mps": condition.max_speed_mps,
                "evidence_timeout_seconds": condition.evidence_timeout_seconds,
                "observation_started_field": "payload.area_observation_started_at",
            },
            tail,
        )
    elif intent.capability == "vision.route":
        tail = add(
            "area",
            "camera.area_restriction",
            {
                "composition_id": intent.scope.composition_id,
                "areas": [area.model_dump()],
                "include_area_names": [area.name],
                "drop_when_unmapped": True,
            },
            tail,
        )
    else:
        if area:
            tail = add(
                "occurrence",
                "camera.area_occurrence",
                {
                    **occurrence,
                    "mode": "entry" if intent.capability == "vision.entry" else "presence",
                    "categories": condition.categories,
                    "area": area.model_dump(),
                    "composition_id": intent.scope.composition_id,
                },
                tail,
            )
        elif intent.scope.kind == "image_region":
            region = VisualRegionConfig.model_validate(intent.scope.region)
            region.require_region()
            if (region.camera_id, region.source_id) != (
                sources[0]["camera_id"],
                sources[0]["source_id"],
            ) or region.framing != "passive":
                raise ValueError(
                    "The selected region must belong to this source and use passive observation"
                )
            tail = add("region", "camera.panorama_region", region.model_dump(), tail)
            tail = add(
                "occurrence",
                "camera.region_occurrence",
                {
                    **occurrence,
                    "mode": "entry" if intent.capability == "vision.entry" else "presence",
                    "categories": condition.categories,
                    "region_name": region.name,
                },
                tail,
                port="observations",
            )
        else:
            tail = add(
                "occurrence",
                "camera.frame_occurrence",
                {**occurrence, "categories": condition.categories},
                tail,
            )
    if len(sources) > 1:
        tail = add(
            "entity_activity",
            "vision.entity_activity",
            {
                "scope": association["scope"],
                "mode": "route" if intent.capability == "vision.route" else "occurrence",
                "evidence_timeout_seconds": condition.evidence_timeout_seconds,
                "continuity_seconds": association.get("horizon_seconds", 120),
            },
            tail,
        )
    if intent.capability == "vision.recognition":
        tail = add("identity_context", "vision.identity_context", {"enabled": True}, tail)
        tail = add("identity_evidence", "vision.identity_evidence", {"enabled": True}, tail)
        tail = add(
            "recognize",
            "vision.recognize_identity",
            {**condition.resource_parameters, "enabled": True},
            tail,
        )
    elif intent.capability in {"vision.pose", "vision.gestures", "vision.segmentation"}:
        operator = (
            "vision.segment_instances"
            if intent.capability == "vision.segmentation"
            else "vision.pose_estimate"
        )
        tail = add("detail", operator, condition.resource_parameters, tail)
        if intent.capability == "vision.gestures":
            tail = add("gesture", "vision.gesture_recognize", {"output_mode": "events"}, tail)
    elif intent.capability == "ai.context":
        tail = add(
            "condition",
            "ai.condition_filter",
            {
                **condition.resource_parameters,
                "emit_mode": "occurrence",
                "failure_policy": "drop",
                "fallback_profile_ids": [],
                "fallback_on_low_confidence": False,
                "reuse_last_decision_seconds": 0,
                "bind_subject_context": True,
            },
            tail,
        )
    contract = {
        "type": "packet",
        "version": 1,
        "payload_keys": ["subject", "camera_id"],
        "artifacts": [],
    }
    operators = {node["id"]: node["operator"] for node in nodes}
    for edge in edges:
        if operators[edge["from"]["node"]] not in {
            "camera.source",
            "vision.detect",
            "camera.camera_mapping",
        }:
            # Association emits batches; discarding their intermediate observations loses duration.
            edge["queue"] = {"max_items": 64, "drop_policy": "block"}
            edge["backpressure"] = {"mode": "block"}
            edge["lifecycle"] = {"compact_updates": False}
    graph = build_pipeline_graph_v2(
        graph_uid=intent.id,
        nodes=nodes,
        edges=edges,
        meta={
            "outputs": [
                {
                    "key": "automatic." + intent.id,
                    "name": intent.name,
                    "node_uid": tail,
                    "contract": contract,
                    "production_scope": "automatic-observation",
                    "state_owner": "automatic-observation",
                    "state_partition": "source-or-place",
                }
            ]
        },
    )
    return {"sources": sources, "graph": graph, "limitations": limitations}


def observation_targets(*, configuration):
    settings = CamerasExtensionSettings.model_validate(
        configuration.settings.extensions.get("com.toposync.cameras", {})
    )
    targets = []
    for camera in settings.devices:
        scope = AutomaticScope(camera_ids=[camera.id])
        sources, _, reason = resolve_sources(scope, configuration)
        targets.append(
            {
                "id": "camera:" + camera.id,
                "name": camera.name or camera.id,
                "scope": scope.model_dump(mode="json"),
                "sources": sources,
                "reason": reason,
            }
        )
        if sources:
            targets.append(
                {
                    "id": "region:" + camera.id,
                    "name": camera.name or camera.id,
                    "scope": {
                        **scope.model_dump(mode="json"),
                        "kind": "image_region",
                        "region": {
                            "camera_id": camera.id,
                            "source_id": sources[0]["source_id"],
                            "framing": "passive",
                        },
                    },
                    "sources": sources,
                    "reason": reason,
                    "scope_operator": "camera.panorama_region",
                }
            )
    for composition in configuration.compositions:
        for element in composition.elements:
            area = resolve_composition_area(
                configuration.compositions, composition_id=composition.id, area_id=element.id
            )
            if area is None:
                continue
            scope = AutomaticScope(
                kind="physical_area", composition_id=composition.id, area_id=element.id
            )
            sources, _, reason = resolve_sources(scope, configuration)
            targets.append(
                {
                    "id": "area:" + composition.id + ":" + element.id,
                    "name": composition.name + " · " + area.name,
                    "preparations": {
                        "vision.associate_entities": {
                            "sources": [
                                {
                                    "camera_id": source["camera_id"],
                                    "source_id": source["source_id"],
                                    "identity_policy": "no_identification",
                                    "clock_domain": source["clock_domain"],
                                    "clock_uncertainty_seconds": None,
                                    "coordinate_reference": composition.id,
                                    "position_error_metres": None,
                                }
                                for source in sources
                            ],
                            "transitions": [],
                        }
                    },
                    "scope": scope.model_dump(mode="json"),
                    "sources": sources,
                    "reason": reason,
                }
            )
    return targets


def register_automatic_planner(services):
    services.register("automatic.plan.camera_observation", plan_observation)
    services.register("automatic.targets.cameras", observation_targets)
