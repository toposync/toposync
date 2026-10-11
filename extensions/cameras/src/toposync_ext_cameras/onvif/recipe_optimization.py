"""Recipe-specific admission; camera events remain preliminary evidence."""
from pydantic import BaseModel, ConfigDict, Field

from ..pipeline_templates import build_pipeline_graph_v2


class CameraOnvifOptimization(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    topic: str = Field(min_length=1, max_length=512)
    qualified_topic: str = Field(min_length=1, max_length=2048)
    item_name: str = Field(min_length=1, max_length=200)
    source: dict[str, str] = Field(default_factory=dict, max_length=16)
    key: dict[str, str] = Field(default_factory=dict, max_length=16)
    coverage_confirmed: bool = False
    hold_seconds: float = Field(default=30.0, ge=0.0, le=300.0, allow_inf_nan=False)


RECIPE_OPTIMIZATION = {
    "people_simple": "qualification_required",
    "doorbell_visitor": "continuous_visitor_lifecycle",
    "area_presence": "stationary_presence",
    "area_entry": "outside_before_entry",
    "area_group_entry": "outside_before_group_entry",
    "people_individual": "tracking_continuity",
    "people_quiet": "multiple_categories_and_continuity",
    "presence_area": "multiple_categories_and_continuity",
    "vehicle_stopped": "stationary_presence",
    "person_stopped": "stationary_presence",
    "person_vehicle_interaction": "multiple_categories_and_continuity",
    "cat_litter_use": "small_motion_and_behavior",
    "cat_drinking": "small_motion_and_behavior",
    "audio_anomaly": "visual_event_does_not_cover_audio",
}


def add_onvif_optimization(graph: dict, *, camera_id: str, selection: CameraOnvifOptimization) -> dict:
    nodes = graph["nodes"]
    nodes.append({"id": "camera_event_gate", "operator": "camera.onvif_state_gate", "config": {
        "camera_id": camera_id, "topic": selection.topic, "qualified_topic": selection.qualified_topic,
        "item_name": selection.item_name, "source": selection.source, "key": selection.key,
        "fail_open": True, "hold_seconds": selection.hold_seconds,
    }})
    source = next(node for node in nodes if node["id"] == "source")
    source["config"]["gate_min_observation_seconds"] = 5.0
    edges = [*graph["edges"], {"from": {"node": "camera_event_gate", "port": "out"},
             "to": {"node": "source", "port": "gate"}, "maxsize": 1, "drop_policy": "drop_oldest"}]
    return build_pipeline_graph_v2(nodes=nodes, edges=edges, graph_uid=str(graph.get("uid") or "camera_recipe"))
