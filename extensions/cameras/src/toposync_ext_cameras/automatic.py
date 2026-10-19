"""Pure place/source resolution and graphs built from existing camera and vision operators."""

import math
import hashlib
import json
from dataclasses import asdict
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.automatic import AutomaticScope
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2

from .pipelines.postprocess import (
    _parse_mapping_control_point_sets_from_props,
    resolve_composition_area,
)
from .settings import CamerasExtensionSettings, get_default_camera_source
from .visual_region_models import VisualRegionConfig
from .onvif.recipe_optimization import CameraOnvifOptimization


class NativeObservationSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)
    profile_token: str = Field(min_length=1, max_length=256)
    signal: CameraOnvifOptimization


class NativeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    sources: dict[str, NativeObservationSource] = Field(default_factory=dict, max_length=64)
    fallback_to_video: bool = False
    categories: list[str] = Field(default_factory=list, max_length=32)


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
    native_observation: NativeObservation | None = None


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


def observation_preparation(*, intent, configuration):
    sources, area, reason = resolve_sources(intent.scope, configuration)
    selected = {source["camera_id"] for source in sources}
    settings = CamerasExtensionSettings.model_validate(configuration.settings.extensions.get("com.toposync.cameras", {}))
    locators = []
    for camera in settings.devices:
        for source in camera.sources:
            if not any(item["camera_id"] == camera.id and item["source_id"] == source.id for item in sources):
                continue
            urls = [source.origin.rtsp_url]
            if camera.onvif:
                urls.extend([camera.onvif.xaddr, camera.onvif.media_xaddr or ""])
            # Credential rotation does not change the selected physical view.
            locations = [urlsplit(url)._replace(netloc=urlsplit(url).netloc.rsplit("@", 1)[-1]).geturl() for url in urls]
            locators.append([camera.id, source.id, source.view_id, source.role,
                             source.origin.type, source.origin.profile_token, locations])
    mapping = [
        [element.id, [asdict(item) for item in _parse_mapping_control_point_sets_from_props(element.props)]]
        for composition in configuration.compositions if composition.id == intent.scope.composition_id
        for element in composition.elements if element.props.get("camera_id") in selected
    ]
    value = {
        "sources": [{key: source[key] for key in ("camera_id", "source_id", "clock_domain", "coverage")} for source in sources],
        "area": area.model_dump(mode="json") if area else None,
        "mapping": mapping, "reason": reason, "locators": locators,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate_observation_preparation(*, intent, configuration, preparation):
    return preparation == observation_preparation(intent=intent, configuration=configuration)


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
    native = (
        condition.native_observation
        if condition.native_observation and condition.native_observation.enabled
        else None
    )
    if native is not None:
        if set(native.categories) != set(condition.categories):
            return unavailable("native_categories_qualification_required")
        if set(native.sources) != {source["camera_id"] for source in sources}:
            return unavailable("native_sources_mismatch")
        settings = CamerasExtensionSettings.model_validate(
            configuration.settings.extensions.get("com.toposync.cameras", {})
        )
        for source in sources:
            selection = native.sources[source["camera_id"]]
            camera = next(camera for camera in settings.devices if camera.id == source["camera_id"])
            selected_source = next(
                item for item in camera.sources if item.id == source["source_id"]
            )
            if (
                selection.source_id != selected_source.id
                or selection.profile_token != selected_source.origin.profile_token
                or not selection.signal.coverage_confirmed
                or camera.control.type != "onvif"
            ):
                return unavailable("native_source_qualification_required")
            if "PeopleDetector" in selection.signal.topic and condition.categories != ["person"]:
                return unavailable("native_categories_not_covered")
        limitations = [
            "native_window_with_initial_observation",
            "visual_subjects_retain_observation",
            "fallback_to_video_allowed"
            if native.fallback_to_video
            else "no_native_failure_fallback",
        ]
        if area:
            limitations.append("mapping_is_not_a_coverage_certificate")
    eligible_categories = {
        "vision.pose": {"person"},
        "vision.gestures": {"person"},
        "vision.recognition": {"person", "dog", "cat"},
    }.get(intent.capability)
    if eligible_categories is not None and not set(condition.categories) <= eligible_categories:
        return unavailable("category_not_supported")
    if intent.capability == "ai.context":
        if not all(
            str(condition.resource_parameters.get(field) or "").strip()
            for field in ("profile_id", "condition_description")
        ):
            return unavailable("ai_question_and_profile_required")
        if condition.resource_parameters.get("concurrency_policy") == "fallback":
            raise ValueError(
                "Automatic contextual annotation does not authorize fallback providers"
            )
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
            {
                "camera_id": source["camera_id"],
                "source_id": source["source_id"],
                "fps": 4,
                **(
                    {
                        "observation_window_id": "automatic",
                        "gate_min_observation_seconds": max(
                            5, native.sources[source["camera_id"]].signal.hold_seconds
                        ),
                        "observe_on_start": True,
                    }
                    if native
                    else {}
                ),
            },
        )
        if native:
            signal = native.sources[source["camera_id"]].signal
            gate = add(
                prefix + "native",
                "camera.onvif_state_gate",
                {
                    **signal.model_dump(exclude={"coverage_confirmed"}),
                    "camera_id": source["camera_id"],
                    "fail_open": native.fallback_to_video,
                    "stream_id": "automatic-native:"
                    + source["camera_id"]
                    + ":"
                    + source["source_id"],
                },
            )
            edges.append(
                {"from": {"node": gate, "port": "out"}, "to": {"node": tail, "port": "gate"}}
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
        if intent.capability in {"vision.pose", "vision.gestures"}:
            tail = add(
                prefix + "detail", "vision.pose_estimate", condition.resource_parameters, tail
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
        if intent.capability == "vision.gestures":
            tail = add(
                prefix + "gesture", "vision.gesture_recognize", {"output_mode": "annotate"}, tail
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
    if native:
        tail = add(
            "retain_observation",
            "camera.observation_hold",
            {
                "window_id": "automatic",
                "hold_seconds": condition.evidence_timeout_seconds,
            },
            tail,
        )
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
    elif intent.capability == "vision.segmentation":
        tail = add(
            "detail",
            "vision.segment_instances",
            {
                **condition.resource_parameters,
                "attach_polygons": True,
                "attach_mask_artifacts": False,
            },
            tail,
        )
    elif intent.capability == "ai.context":
        tail = add(
            "subject_image",
            "vision.crop_objects",
            {
                "bbox_field": "subject.bbox01",
                "fallback_to_detections": False,
                "missing_policy": "pass_through",
                "preserve_scene": False,
                "padding_ratio": 0,
                "output_artifact_name": "automatic_subject",
            },
            tail,
        )
        tail = add(
            "condition",
            "ai.condition_filter",
            {
                **condition.resource_parameters,
                "emit_mode": "annotate",
                "input_artifact_name": "automatic_subject",
                "failure_policy": "drop",
                "fallback_profile_ids": [],
                "fallback_on_low_confidence": False,
                "reuse_last_decision_seconds": 0,
                "bind_subject_context": True,
            },
            tail,
        )
    if intent.capability in {"vision.pose", "vision.gestures", "vision.segmentation"}:
        tail = add(
            "public_detail",
            "vision.observation_detail",
            {"kind": intent.capability.split(".")[1]},
            tail,
        )
    evidence_keys = [
        field
        for operator, field in (
            ("camera.frame_occurrence", "frame_occurrence"),
            ("camera.region_occurrence", "region_occurrence"),
            ("camera.area_occurrence", "area_occurrence"),
            ("core.stationary_event", "stationary_event"),
            ("vision.entity_activity", "entity_activity"),
            ("vision.recognize_identity", "recognition"),
            ("vision.observation_detail", "vision_observation"),
            ("ai.condition_filter", "condition_annotation"),
        )
        if any(node["operator"] == operator for node in nodes)
    ]
    contract = {
        "type": "packet",
        "version": 1,
        "payload_keys": [
            "subject",
            "camera_id",
            "frame_ts",
            *evidence_keys,
            *(["native_observation"] if native else []),
        ],
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
    preparation = observation_preparation(intent=intent, configuration=configuration)
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
                    "production_scope": "automatic-observation:" + preparation,
                    "state_owner": "automatic-observation",
                    "state_partition": "source-or-place",
                }
            ]
        },
    )
    return {
        "sources": sources,
        "graph": graph,
        "limitations": limitations,
        "preparation": preparation,
    }


def observation_diagnostics(*, intents, configuration, events, health):
    records = health.snapshot()["sources"]
    result = []
    for intent in intents:
        condition = ObservationCondition.model_validate(intent.condition)
        native = condition.native_observation
        sources, _, _ = resolve_sources(intent.scope, configuration)
        observations = []
        for source in sources:
            candidates = [record for record in records if record["camera_id"] == source["camera_id"]
                          and record["camera_source_id"] == source["source_id"]]
            latest = max(candidates, key=lambda record: record["last_seen_at_unix"], default={})
            observation = {"camera_id": source["camera_id"], "source_id": source["source_id"],
                           "camera_name": source["camera_name"], "source_name": source["source_name"],
                           "video": latest.get("status", "unknown")}
            if native and native.enabled:
                binding = native.sources.get(source["camera_id"])
                raw = events.cached_snapshot(camera_id=source["camera_id"],
                    **binding.signal.model_dump(exclude={"coverage_confirmed", "hold_seconds"})) if binding else {}
                if raw.get("observation") == "not_started":
                    state = "not_observed"
                elif raw.get("available") is not True:
                    state = "unavailable"
                elif raw.get("known") is not True or type(raw.get("value")) is not bool:
                    state = "unknown"
                else:
                    state = "active" if raw["value"] else "inactive"
                observation.update(native=state, fallback_to_video=native.fallback_to_video)
            observations.append(observation)
        result.append({"id": intent.id, "diagnostics": {"native_observation": {"sources": observations}}})
    return result


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
    async def plan(**kwargs):
        result = plan_observation(**kwargs)
        condition = ObservationCondition.model_validate(kwargs["intent"].condition)
        if (
            result.get("graph")
            and condition.native_observation
            and condition.native_observation.enabled
        ):
            qualification = hashlib.sha256(json.dumps({
                "preparation": result["preparation"],
                "selection": condition.native_observation.model_dump(exclude={"fallback_to_video"}),
            }, sort_keys=True).encode()).hexdigest()
            previous = next((pipeline for pipeline in kwargs["configuration"].pipelines
                             if pipeline.graph.get("meta", {}).get("automatic", {}).get("intent_id") == kwargs["intent"].id), None)
            if previous and previous.graph["meta"].get("native_observation_qualification") == qualification:
                result["graph"]["meta"]["native_observation_qualification"] = qualification
                return result
            try:
                await services.call(
                    "automatic.qualify.camera_native",
                    configuration=kwargs["configuration"],
                    selection=condition.native_observation,
                )
                result["graph"]["meta"]["native_observation_qualification"] = qualification
            except Exception:
                result.pop("graph")
                result.update(state="unavailable", reason="native_source_qualification_required")
        return result

    services.register("automatic.plan.camera_observation", plan)
    services.register("automatic.validate.camera_observation", validate_observation_preparation)
    services.register("automatic.targets.cameras", observation_targets)
