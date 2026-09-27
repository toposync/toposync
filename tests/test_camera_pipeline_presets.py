from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod
import toposync_ext_cameras.plugin as cameras_plugin_mod
from toposync.runtime.pipelines import Lifecycle, Packet
from toposync.runtime.pipelines.operators_core import FPSReducerRuntime, StationaryEventRuntime


CAMERA_PIPELINE_PRESETS = (
    "people_simple",
    "people_individual",
    "people_quiet",
    "presence_area",
    "vehicle_stopped",
    "person_stopped",
    "person_vehicle_interaction",
)
CAMERA_PIPELINE_PRESET_MODEL_CONTRACTS = tuple(
    preset for preset in CAMERA_PIPELINE_PRESETS if preset != "people_quiet"
)
CAMERA_PIPELINE_PRESETS_REQUIRING_MAPPING = {
    "people_individual",
    "people_quiet",
    "presence_area",
    "vehicle_stopped",
    "person_stopped",
    "person_vehicle_interaction",
}


def test_audio_recipe_requires_baseline_and_saves_disabled_without_vision(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch, patch_model_readiness=False) as client:
        _configure_camera(client)
        path = "/api/cameras/cameras/cam1/pipelines/presets"
        body = {"preset": "audio_anomaly", "enabled": True, "source_id": "main"}
        assert client.post(path, json=body).status_code == 400
        body["audio_baseline"] = {"reference": "Normal da garagem.wav", "rms": 0.01}
        invalid = {**body, "audio_baseline": {"reference": "", "rms": 0}}
        assert client.post(path, json=invalid).status_code == 422
        assert client.post(path, json={**body, "enable_ptz_attention": True}).status_code == 400
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        pipeline = client.get(f"/api/pipelines/{response.json()['pipeline_name']}").json()
        assert pipeline["enabled"] is False
        assert _operator_ids(pipeline) == ["camera.audio_source", "camera.audio_features", "camera.audio_occurrence", "core.notify"]
        assert _node_config(pipeline, "camera.audio_features")["baseline"]["rms"] == 0.01
        assert _node_config(pipeline, "camera.audio_features").get("transcribe", False) is False
        assert _node_config(pipeline, "core.notify")["dedupe_by_occurrence"] is True
        assert pipeline["graph"]["meta"]["audio_support"] == "unverified"


@pytest.mark.parametrize("preset", ["person_stopped", "vehicle_stopped"])
def test_stopped_recipe_keeps_observing_beyond_motion_hold(tmp_path, monkeypatch, preset):
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client)
        response = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": preset, "min_stationary_seconds": 10.0, "enabled": False},
        )
        assert response.status_code == 200, response.text
        pipeline = client.get(f"/api/pipelines/{response.json()['pipeline_name']}").json()
        assert "camera.motion_gate" not in _operator_ids(pipeline)
        assert pipeline["enabled"] is False
        limiter = FPSReducerRuntime(_node_config(pipeline, "core.fps_reducer"))
        stationary = StationaryEventRuntime(_node_config(pipeline, "core.stationary_event"))

        async def replay():
            outputs = []
            admitted = 0
            for index in range(241):
                timestamp = index * 0.05
                # Controlled replay clock; detector/world evidence is synthetic here.
                monkeypatch.setattr("toposync.runtime.pipelines.operators_core.time.monotonic", lambda: timestamp + 100)
                packet = Packet.create(stream_id="camera", lifecycle=Lifecycle.UPDATE, payload={
                    "frame_ts": timestamp,
                    "subject": {"id": "observed-1"},
                    "world": {"x": min(timestamp, 1.0), "z": 0.0},
                    "velocity": {"valid": True, "stopped": timestamp >= 1.0,
                                 "speed_mps": 0.0 if timestamp >= 1.0 else 1.0},
                })
                for sample in await limiter.process_packet(packet, None):
                    admitted += 1
                    outputs.extend(await stationary.process_packet(sample, None))
            assert admitted == 49  # Four samples/second, including the initial sample.
            assert outputs[0].lifecycle == Lifecycle.OPEN
            assert outputs[0].payload["stationary_event"]["stationary_seconds"] == 10.0
            assert outputs[-1].lifecycle == Lifecycle.UPDATE
            assert outputs[-1].payload["stationary_event"]["stationary_seconds"] == 11.0

        asyncio.run(replay())


class _ExtensionEntryPoint:
    name = "test_extension"

    def __init__(self, value: str) -> None:
        self.value = value

    def load(self):  # type: ignore[no-untyped-def]
        module_name, class_name = self.value.split(":", 1)
        module = __import__(module_name, fromlist=[class_name])
        return getattr(module, class_name)


def _create_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_model_readiness: bool = True,
    include_ptz_attention: bool = False,
    include_ai: bool = False,
) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    if patch_model_readiness:

        async def _allow_detection_model(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            cameras_plugin_mod,
            "_ensure_camera_preset_detection_model_ready",
            _allow_detection_model,
        )
    entry_points = [
        _ExtensionEntryPoint("toposync_ext_cameras.plugin:CamerasExtension"),
        _ExtensionEntryPoint("toposync_ext_vision.plugin:VisionExtension"),
    ]
    if include_ptz_attention:
        entry_points.append(
            _ExtensionEntryPoint(
                "toposync_ext_ptz_attention.plugin:PtzAttentionExtension"
            )
        )
    if include_ai:
        entry_points.append(_ExtensionEntryPoint("toposync_ext_ai.plugin:AiExtension"))
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: entry_points)
    return TestClient(create_app())


@pytest.mark.parametrize("preset", ["cat_litter_use", "cat_drinking"])
def test_cat_activity_presets_compile_and_persist_without_detection_model(tmp_path, monkeypatch, preset):
    with _create_client(tmp_path, monkeypatch, patch_model_readiness=False, include_ai=True) as client:
        _configure_camera(client)
        response = client.post("/api/cameras/cameras/cam1/pipelines/presets", json={
            "preset": preset, "enabled": False,
        })
        assert response.status_code == 200, response.text
        pipeline = client.get(f"/api/pipelines/{response.json()['pipeline_name']}").json()
        assert pipeline["enabled"] is False
        assert "vision.detect" not in _operator_ids(pipeline)
        condition = _node_config(pipeline, "ai.condition_filter")
        assert condition["emit_mode"] == "occurrence"
        assert condition["reuse_last_decision_seconds"] == 0
        assert condition["failure_policy"] == "drop"
        assert _node_config(pipeline, "core.notify")["dedupe_by_occurrence"] is True
        assert _node_config(pipeline, "ai.smart_crop")["missing_policy"] == "drop"


@pytest.mark.parametrize("preset", ["area_presence", "area_entry", "area_group_entry"])
def test_area_recipe_requires_explicit_area_and_compiles(tmp_path, monkeypatch, preset):
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)
        missing = client.post("/api/cameras/cameras/cam1/pipelines/presets", json={"preset": preset, "enabled": False})
        assert missing.status_code == 400
        response = client.post("/api/cameras/cameras/cam1/pipelines/presets", json={
            "preset": preset, "enabled": False, "area_id": "area-1",
        })
        assert response.status_code == 200, response.text
        pipeline = client.get(f"/api/pipelines/{response.json()['pipeline_name']}").json()
        occurrence = _node_config(pipeline, "camera.area_occurrence")
        assert occurrence["area"]["name"] == "Gate"
        assert occurrence["mode"] == ("entry" if preset in {"area_entry", "area_group_entry"} else "presence")
        expected_categories = ["person"] if preset == "area_group_entry" else ["person", "dog", "cat"]
        assert _node_config(pipeline, "vision.detect")["categories"] == expected_categories
        if preset == "area_group_entry":
            group = _node_config(pipeline, "vision.group_events")
            assert group["minimum_members"] == 2 and group["join_window_seconds"] == 5
            assert occurrence["minimum_duration_seconds"] == 0


def test_area_recipe_respects_composition_and_rejects_ambiguous_area(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)
        first = client.get("/api/composition").json()
        second = client.post("/api/compositions", json={"name": "Other yard"}).json()
        assert client.post(f"/api/compositions/{second['id']}/activate").status_code == 200
        first["id"] = second["id"]
        first["name"] = "Other yard"
        first["elements"][-1]["name"] = "Pool"
        assert client.put("/api/composition", json=first).status_code == 200
        route = "/api/cameras/cameras/cam1/pipelines/presets"
        body = {"preset": "area_presence", "enabled": False, "area_id": "area-1"}
        assert client.post(route, json=body).status_code == 409
        assert client.post(route, json={**body, "composition_id": "missing"}).status_code == 409
        result = client.post(route, json={**body, "composition_id": second["id"]})
        assert result.status_code == 200, result.text
        pipeline = client.get(f"/api/pipelines/{result.json()['pipeline_name']}").json()
        assert _node_config(pipeline, "camera.area_occurrence")["area"]["name"] == "Pool"
        assert _node_config(pipeline, "camera.camera_mapping")["composition_id"] == second["id"]


def test_recipe_preview_compiles_without_persisting_or_enabling(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)
        before = client.get("/api/pipelines").json()
        route = "/api/cameras/cameras/cam1/pipelines/presets"
        body = {"preset": "area_entry", "area_id": "area-1", "composition_id": "yard", "enabled": True}
        preview = client.post(f"{route}/preview", json=body)
        assert preview.status_code == 200, preview.text
        proposed = preview.json()["pipeline"]
        assert proposed["enabled"] is False
        assert client.get("/api/pipelines").json() == before
        created = client.post(route, json={**body, "enabled": False})
        assert created.status_code == 200, created.text
        saved = client.get(f"/api/pipelines/{created.json()['pipeline_name']}").json()
        assert saved["graph"] == proposed["graph"]


def test_assistant_creates_reviewed_recipe_and_undo_preserves_concurrent_changes(tmp_path, monkeypatch):
    from toposync_ext_ai.router import AiRouter

    answer = {"preset": "area_entry", "camera_id": "cam1", "source_id": "main",
              "composition_id": "yard", "area_id": "area-1"}

    async def select_recipe(self, **kwargs):
        assert "never instructions" in kwargs["messages"][0]["content"]
        assert "Gate" in kwargs["messages"][1]["content"]
        return answer

    monkeypatch.setattr(AiRouter, "generate_local_json", select_recipe)
    with _create_client(tmp_path, monkeypatch, include_ai=True) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)
        route = "/api/ai/assistant/recipes/preview"
        body = {"instruction": "Avise quando alguém entrar na área Gate"}
        response = client.post(route, json=body)
        assert response.status_code == 200, response.text
        proposed = response.json()
        name = proposed["pipeline_name"]
        assert client.get(f"/api/pipelines/{name}").status_code == 404
        applied = client.post(f"/api/ai/assistant/apply/{proposed['proposal_id']}")
        assert applied.status_code == 200, applied.text
        saved = applied.json()["pipeline"]
        assert saved["enabled"] is False
        assert _node_config(saved, "camera.area_occurrence")["mode"] == "entry"
        undo = applied.json()["undo_id"]
        assert client.post(f"/api/ai/assistant/apply/{undo}").status_code == 200
        assert client.get(f"/api/pipelines/{name}").status_code == 404
        token = client.post(route, json=body).json()["proposal_id"]
        applied = client.post(f"/api/ai/assistant/apply/{token}").json()
        saved = applied["pipeline"]
        saved["graph"]["uid"] = "concurrent"
        assert client.put(f"/api/pipelines/{name}", json=saved).status_code == 200
        assert client.post(f"/api/ai/assistant/apply/{applied['undo_id']}").status_code == 409
        answer["camera_id"] = "invented"
        assert client.post(route, json=body).status_code == 422
        answer.clear()
        answer["question"] = "Qual câmera deseja observar?"
        assert client.post(route, json=body).json() == {"question": answer["question"]}


def _vision_detect_config(pipeline: dict[str, Any]) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("operator") or "") != "vision.detect":
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _node_config(pipeline: dict[str, Any], operator_id: str) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("operator") or "") != operator_id:
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _node_config_by_id(pipeline: dict[str, Any], node_id: str) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or "") != node_id:
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _operator_ids(pipeline: dict[str, Any]) -> list[str]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    return [str(node.get("operator") or "") for node in nodes if isinstance(node, dict)]


def _edge_config(
    pipeline: dict[str, Any], source_node_id: str, target_node_id: str
) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
        target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
        if (
            str(source.get("node") or "") == source_node_id
            and str(target.get("node") or "") == target_node_id
        ):
            return _normalized_edge(edge)
    return {}


def _normalized_edge(edge: dict[str, Any]) -> dict[str, Any]:
    queue = edge.get("queue") if isinstance(edge.get("queue"), dict) else {}
    return {
        **edge,
        "maxsize": edge.get("maxsize", queue.get("max_items")),
        "drop_policy": edge.get("drop_policy", queue.get("drop_policy")),
    }


def _configure_camera(client: TestClient) -> None:
    res = client.patch(
        "/api/settings/extensions/com.toposync.cameras",
        json={
            "devices": [
                {
                    "id": "cam1",
                    "name": "Entrada Principal",
                    "control": {"type": "none"},
                    "sources": [
                        {
                            "id": "main",
                            "name": "Principal",
                            "enabled": True,
                            "is_default": True,
                            "kind": "video",
                            "role": "main",
                            "origin": {"type": "rtsp", "rtsp_url": "rtsp://example.local/front"},
                            "ingest": {"mode": "direct"},
                        }
                    ],
                }
            ],
        },
    )
    assert res.status_code == 200, res.text


def _configure_ptz_camera(client: TestClient) -> None:
    res = client.patch(
        "/api/settings/extensions/com.toposync.cameras",
        json={
            "devices": [
                {
                    "id": "cam1",
                    "name": "Entrada Principal",
                    "control": {"type": "onvif"},
                    "onvif": {
                        "xaddr": "http://camera.local/onvif/device_service",
                        "username": "operator",
                        "password": "not-used-in-test",
                    },
                    "sources": [
                        {
                            "id": "main",
                            "name": "Principal",
                            "enabled": True,
                            "is_default": True,
                            "kind": "video",
                            "role": "main",
                            "origin": {
                                "type": "onvif_profile",
                                "profile_token": "profile-main",
                                "has_ptz": True,
                            },
                            "ingest": {"mode": "direct"},
                        }
                    ],
                }
            ],
        },
    )
    assert res.status_code == 200, res.text


def _add_mapped_composition(client: TestClient, *, with_area: bool = False) -> None:
    elements: list[dict[str, Any]] = [
        {
            "id": "cam-element",
            "type": "com.toposync.cameras.camera",
            "name": "Front",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "props": {
                "camera_id": "cam1",
                "control_point_sets": [
                    {
                        "id": "main",
                        "label": "Main",
                        "control_points": [
                            {
                                "id": "A",
                                "image": {"x": 0.0, "y": 0.0},
                                "world": {"x": 0.0, "z": 0.0},
                            },
                            {
                                "id": "B",
                                "image": {"x": 1.0, "y": 0.0},
                                "world": {"x": 10.0, "z": 0.0},
                            },
                            {
                                "id": "C",
                                "image": {"x": 1.0, "y": 1.0},
                                "world": {"x": 10.0, "z": 10.0},
                            },
                            {
                                "id": "D",
                                "image": {"x": 0.0, "y": 1.0},
                                "world": {"x": 0.0, "z": 10.0},
                            },
                        ],
                    }
                ],
            },
        }
    ]
    if with_area:
        elements.append(
            {
                "id": "area-1",
                "type": "com.toposync.structural.area",
                "name": "Gate",
                "position": {"x": 0, "y": 0, "z": 0},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "props": {
                    "vertices": [
                        {"x": 0.0, "z": 0.0},
                        {"x": 2.0, "z": 0.0},
                        {"x": 1.0, "z": 2.0},
                    ]
                },
            }
        )

    res = client.put(
        "/api/composition",
        json={
            "id": "yard",
            "name": "Yard",
            "elements": elements,
        },
    )
    assert res.status_code == 200, res.text


def _prepare_preset_prerequisites(client: TestClient, preset: str) -> None:
    _configure_camera(client)
    if preset in CAMERA_PIPELINE_PRESETS_REQUIRING_MAPPING:
        _add_mapped_composition(client)


@pytest.mark.parametrize("preset", CAMERA_PIPELINE_PRESETS)
def test_camera_pipeline_preset_builds_graph_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _prepare_preset_prerequisites(client, preset)

        res = client.post("/api/cameras/cameras/cam1/pipelines/presets", json={"preset": preset})
        assert res.status_code == 200, res.text

        pipeline_name = res.json()["pipeline_name"]
        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200, res.text
        pipeline = res.json()
        graph = pipeline.get("graph")
        assert isinstance(graph, dict)
        assert graph.get("schema_version") == 2
        assert graph.get("uid") == pipeline_name
        assert all(isinstance(node, dict) and node.get("uid") for node in graph.get("nodes", []))
        assert all(
            isinstance(edge, dict) and edge.get("uid") and isinstance(edge.get("queue"), dict)
            for edge in graph.get("edges", [])
        )

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text


def test_camera_pipeline_simple_preset_defaults_detection_to_rfdetr_medium_without_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "devices": [
                    {
                        "id": "cam1",
                        "name": "Entrada Principal",
                        "control": {"type": "none"},
                        "sources": [
                            {
                                "id": "main",
                                "name": "Principal",
                                "enabled": True,
                                "is_default": True,
                                "kind": "video",
                                "role": "main",
                                "origin": {
                                    "type": "rtsp",
                                    "rtsp_url": "rtsp://example.local/front",
                                },
                                "ingest": {"mode": "direct"},
                            }
                        ],
                    }
                ],
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_simple", "enabled": True},
        )
        assert res.status_code == 200
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_deteccao_simples_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _vision_detect_config(pipeline).get("model_id") == "rfdetr_det_medium"
        assert _vision_detect_config(pipeline).get("categories") == ["person"]
        assert _vision_detect_config(pipeline).get("confidence_threshold") == 0.25
        track_config = _node_config(pipeline, "vision.track")
        assert track_config.get("tracker_id") == "byte_world"
        assert track_config.get("open_confidence_threshold") == 0.50
        assert track_config.get("continue_confidence_threshold") == 0.25
        assert track_config.get("close_after_seconds") == 10.0
        assert track_config.get("stitch_gap_seconds") == 30.0
        assert track_config.get("use_world_anchor") == "auto"
        assert "camera.camera_mapping" not in _operator_ids(pipeline)
        assert "vision.group_events" not in _operator_ids(pipeline)
        assert "vision.event_assembler" not in _operator_ids(pipeline)
        assert _node_config(pipeline, "core.throttle").get("interval_seconds") == 10.0
        assert _node_config(pipeline, "core.notify").get("dedupe_key_template") == "{{subject.id}}"
        assert _node_config(pipeline, "core.notify").get("priority") == "medium"

        res = client.get("/api/cameras/cameras/cam1/pipelines")
        assert res.status_code == 200
        overview = res.json()
        assert overview["pipelines"][0]["name"] == pipeline_name
        assert (
            overview["suggested_pipeline_names"]["people_simple"]
            == "entrada_principal_deteccao_simples_de_pessoas_2"
        )
        assert (
            overview["suggested_pipeline_names"]["people_individual"]
            == "entrada_principal_evento_individual_de_pessoas"
        )
        assert (
            overview["suggested_pipeline_names"]["people_quiet"]
            == "entrada_principal_presenca_agrupada_de_pessoas"
        )
        assert (
            overview["suggested_pipeline_names"]["presence_area"]
            == "entrada_principal_presenca_agrupada_em_area"
        )
        assert (
            overview["suggested_pipeline_names"]["vehicle_stopped"]
            == "entrada_principal_veiculo_parou"
        )
        assert (
            overview["suggested_pipeline_names"]["person_stopped"]
            == "entrada_principal_pessoa_parou"
        )
        assert (
            overview["suggested_pipeline_names"]["person_vehicle_interaction"]
            == "entrada_principal_interacao_pessoa_veiculo"
        )

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_simple", "enabled": True},
        )
        assert res.status_code == 200
        assert res.json()["pipeline_name"] == "entrada_principal_deteccao_simples_de_pessoas_2"


@pytest.mark.parametrize("preset", CAMERA_PIPELINE_PRESET_MODEL_CONTRACTS)
def test_camera_pipeline_preset_uses_requested_detection_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _prepare_preset_prerequisites(client, preset)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": preset,
                "model_id": "custom_detector_ready",
            },
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _vision_detect_config(pipeline).get("model_id") == "custom_detector_ready"


@pytest.mark.parametrize("preset", CAMERA_PIPELINE_PRESET_MODEL_CONTRACTS)
def test_camera_pipeline_preset_blocks_missing_detection_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
) -> None:
    async def _missing_model_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "vision": {
                "task_catalogs": {
                    "detection": {
                        "items": [
                            {
                                "model_id": "custom_detector_missing",
                                "display_name": "Custom Detector Missing",
                                "availability": "manifest_only",
                                "artifact_exists": False,
                                "local_build_supported": True,
                                "local_build_reason": "container_runtime_missing",
                            }
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(
        cameras_plugin_mod,
        "_collect_camera_preset_processing_status",
        _missing_model_status,
    )
    with _create_client(tmp_path, monkeypatch, patch_model_readiness=False) as client:
        _prepare_preset_prerequisites(client, preset)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": preset, "model_id": "custom_detector_missing"},
        )
        assert res.status_code == 409
        detail = res.json()["detail"]
        assert "Custom Detector Missing" in detail
        assert "não está pronto" in detail
        assert "Baixe e prepare automaticamente" in detail

        res = client.get("/api/cameras/cameras/cam1/pipelines")
        assert res.status_code == 200, res.text
        assert res.json()["pipelines"] == []


def test_camera_pipeline_individual_preset_requires_and_uses_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_individual"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]

        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_individual"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_evento_individual_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _operator_ids(pipeline)[:5] == [
            "camera.source",
            "camera.motion_gate",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
        ]
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert "vision.group_events" not in _operator_ids(pipeline)
        assert _node_config(pipeline, "core.notify").get("dedupe_key_template") == "{{subject.id}}"


def test_camera_pipeline_preset_option_adds_configured_ptz_attention_operator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch, include_ptz_attention=True) as client:
        _configure_ptz_camera(client)
        _add_mapped_composition(client)

        rejected = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": "person_vehicle_interaction",
                "source_id": "main",
                "composition_id": "yard",
                "enable_ptz_attention": True,
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert "native tracking" in rejected.json()["detail"]

        created = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": "person_vehicle_interaction",
                "source_id": "main",
                "composition_id": "yard",
                "enable_ptz_attention": True,
                "ptz_attention_native_tracking_disabled_confirmed": True,
            },
        )
        assert created.status_code == 200, created.text

        pipeline = client.get(f"/api/pipelines/{created.json()['pipeline_name']}").json()
        assert "ptz_attention.request" in _operator_ids(pipeline)
        assert _node_config_by_id(pipeline, "ptz_attention") == {
            "camera_id": "cam1",
            "source_id": "main",
            "composition_id": "yard",
            "priority": 0,
            "hold_after_close_seconds": 8.0,
            "native_tracking_disabled_confirmed": True,
        }
        assert _edge_config(pipeline, "relation", "ptz_attention")["queue"] == {
            "max_items": 16,
            "drop_policy": "block",
        }
        compiled = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert compiled.status_code == 200, compiled.text


def test_camera_pipeline_quiet_preset_adds_session_grouping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_quiet"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]

        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_quiet"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_presenca_agrupada_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _vision_detect_config(pipeline).get("categories") == ["person", "dog", "cat"]
        assert _vision_detect_config(pipeline).get("confidence_threshold") == 0.25
        assert _operator_ids(pipeline)[:5] == [
            "camera.source",
            "camera.motion_gate",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
        ]
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "vision.group_events").get("mode") == "session"
        assert _node_config(pipeline, "vision.group_events").get("categories") == [
            "person",
            "dog",
            "cat",
        ]
        notify = _node_config(pipeline, "core.notify")
        assert notify.get("description") == ""
        assert notify.get("dedupe_key_template") == "{{subject.id}}"


def test_camera_pipeline_presence_area_preset_adds_mapping_velocity_and_grouping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "devices": [
                    {
                        "id": "cam1",
                        "name": "Entrada Principal",
                        "control": {"type": "none"},
                        "sources": [
                            {
                                "id": "main",
                                "name": "Principal",
                                "enabled": True,
                                "is_default": True,
                                "kind": "video",
                                "role": "main",
                                "origin": {
                                    "type": "rtsp",
                                    "rtsp_url": "rtsp://example.local/front",
                                },
                                "ingest": {"mode": "direct"},
                            }
                        ],
                    }
                ],
            },
        )
        assert res.status_code == 200

        res = client.put(
            "/api/composition",
            json={
                "id": "yard",
                "name": "Yard",
                "elements": [
                    {
                        "id": "cam-element",
                        "type": "com.toposync.cameras.camera",
                        "name": "Front",
                        "position": {"x": 0, "y": 0, "z": 0},
                        "rotation": {"x": 0, "y": 0, "z": 0},
                        "props": {
                            "camera_id": "cam1",
                            "control_point_sets": [
                                {
                                    "id": "main",
                                    "label": "Main",
                                    "control_points": [
                                        {
                                            "id": "A",
                                            "image": {"x": 0.0, "y": 0.0},
                                            "world": {"x": 0.0, "z": 0.0},
                                        },
                                        {
                                            "id": "B",
                                            "image": {"x": 1.0, "y": 0.0},
                                            "world": {"x": 10.0, "z": 0.0},
                                        },
                                        {
                                            "id": "C",
                                            "image": {"x": 1.0, "y": 1.0},
                                            "world": {"x": 10.0, "z": 10.0},
                                        },
                                        {
                                            "id": "D",
                                            "image": {"x": 0.0, "y": 1.0},
                                            "world": {"x": 0.0, "z": 10.0},
                                        },
                                    ],
                                }
                            ],
                        },
                    }
                ],
            },
        )
        assert res.status_code == 200, res.text

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "presence_area"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_presenca_agrupada_em_area"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _operator_ids(pipeline)[:5] == [
            "camera.source",
            "camera.motion_gate",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
        ]
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "vision.track").get("default_interval_seconds") == 0.25
        assert "vision.event_assembler" not in _operator_ids(pipeline)
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.velocity_estimation").get("filter_mode") == "annotate"
        assert _node_config(pipeline, "vision.group_events").get("mode") == "proximity"
        assert _node_config(pipeline, "vision.group_events").get("group_distance_meters") == 10.0
        assert (
            _node_config(pipeline, "vision.group_events").get("include_stationary_members") is True
        )
        assert _node_config(pipeline, "core.throttle").get("interval_seconds") == 10.0
        notify = _node_config(pipeline, "core.notify")
        assert notify.get("description") == ""
        assert notify.get("dedupe_key_template") == "{{subject.id}}"
        assert _edge_config(pipeline, "detect", "map").get("maxsize") == 8

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        alert_codes = {str(alert.get("code") or "") for alert in res.json().get("alerts", [])}
        assert alert_codes <= {"vision_model_artifact_missing"}


def test_camera_pipeline_vehicle_stopped_requires_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "vehicle_stopped"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]


def test_camera_pipeline_vehicle_stopped_builds_confirmed_stop_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "vehicle_stopped"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_veiculo_parou"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()

        assert _operator_ids(pipeline) == [
            "camera.source",
            "core.fps_reducer",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
            "camera.velocity_estimation",
            "core.stationary_event",
            "core.debounce",
            "vision.crop_objects",
            "core.store_images",
            "core.notify",
        ]
        assert _node_config(pipeline, "core.fps_reducer") == {"target_fps": 4.0}
        detect = _vision_detect_config(pipeline)
        assert detect.get("categories") == ["car", "truck", "bus", "motorcycle"]
        assert detect.get("confidence_threshold") == 0.25
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.area_restriction") == {}

        velocity = _node_config(pipeline, "camera.velocity_estimation")
        assert velocity.get("filter_mode") == "annotate"
        assert velocity.get("stopped_speed_threshold") == pytest.approx(1.0 / 3.6)

        stationary = _node_config_by_id(pipeline, "stationary_event")
        assert stationary.get("key_field") == "payload.subject.id"
        assert stationary.get("stopped_field") == "payload.velocity.stopped"
        assert stationary.get("valid_field") == "payload.velocity.valid"
        assert stationary.get("speed_field") == "payload.velocity.speed_mps"
        assert stationary.get("max_speed_mps") == pytest.approx(1.0 / 3.6)
        assert stationary.get("min_stationary_seconds") == pytest.approx(1.25)
        assert stationary.get("min_valid_samples") == 3
        assert stationary.get("require_arrival") is True
        assert stationary.get("merge_moving_gap_seconds") == pytest.approx(15.0)
        assert _node_config_by_id(pipeline, "storage_throttle") == {}
        assert _node_config_by_id(pipeline, "storage_crop") == {}
        assert _node_config_by_id(pipeline, "storage_store") == {}
        assert (
            _node_config_by_id(pipeline, "notify_debounce").get("key_field") == "payload.subject.id"
        )

        notify = _node_config(pipeline, "core.notify")
        assert notify.get("dedupe_key_template") == "{{subject.id}}"
        assert notify.get("title") == "{{camera_name}}: veículo parado"
        assert notify.get("priority") == "high"

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        alert_codes = {str(alert.get("code") or "") for alert in res.json().get("alerts", [])}
        assert alert_codes <= {"vision_model_artifact_missing"}


def test_camera_pipeline_person_stopped_builds_confirmed_stop_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "person_stopped"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_pessoa_parou"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()

        assert _operator_ids(pipeline) == [
            "camera.source",
            "core.fps_reducer",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
            "camera.velocity_estimation",
            "core.stationary_event",
            "core.debounce",
            "vision.crop_objects",
            "core.store_images",
            "core.notify",
        ]
        assert _node_config(pipeline, "core.fps_reducer") == {"target_fps": 4.0}
        detect = _vision_detect_config(pipeline)
        assert detect.get("categories") == ["person"]
        assert detect.get("confidence_threshold") == 0.25
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.area_restriction") == {}

        velocity = _node_config(pipeline, "camera.velocity_estimation")
        assert velocity.get("filter_mode") == "annotate"
        assert velocity.get("stopped_speed_threshold") == pytest.approx(1.0 / 3.6)

        stationary = _node_config_by_id(pipeline, "stationary_event")
        assert stationary.get("key_field") == "payload.subject.id"
        assert stationary.get("stopped_field") == "payload.velocity.stopped"
        assert stationary.get("valid_field") == "payload.velocity.valid"
        assert stationary.get("speed_field") == "payload.velocity.speed_mps"
        assert stationary.get("max_speed_mps") == pytest.approx(1.0 / 3.6)
        assert stationary.get("min_stationary_seconds") == pytest.approx(1.25)
        assert stationary.get("min_valid_samples") == 3
        assert stationary.get("require_arrival") is True
        assert stationary.get("merge_moving_gap_seconds") == pytest.approx(15.0)

        notify = _node_config(pipeline, "core.notify")
        assert notify.get("dedupe_key_template") == "{{subject.id}}"
        assert notify.get("title") == "{{camera_name}}: pessoa parada"
        assert notify.get("priority") == "medium"

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        alert_codes = {str(alert.get("code") or "") for alert in res.json().get("alerts", [])}
        assert alert_codes <= {"vision_model_artifact_missing"}


@pytest.mark.parametrize("preset", ["vehicle_stopped", "person_stopped"])
def test_camera_pipeline_stopped_area_uses_area_composition_and_restriction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": preset,
                "area_id": "area-1",
                "stopped_speed_threshold": 0.5,
                "min_stationary_seconds": 2.0,
            },
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        operators = _operator_ids(pipeline)
        assert operators.index("camera.velocity_estimation") < operators.index(
            "camera.area_restriction"
        )
        assert operators.index("camera.area_restriction") < operators.index("core.stationary_event")
        assert _node_config_by_id(pipeline, "storage_throttle") == {}
        assert _node_config_by_id(pipeline, "storage_crop") == {}
        assert _node_config_by_id(pipeline, "storage_store") == {}

        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.velocity_estimation").get(
            "stopped_speed_threshold"
        ) == pytest.approx(0.5)
        assert _node_config_by_id(pipeline, "stationary_event").get(
            "min_stationary_seconds"
        ) == pytest.approx(2.0)

        area = _node_config(pipeline, "camera.area_restriction")
        assert area.get("include_area_names") == ["Gate"]
        assert area.get("drop_when_unmapped") is True
        assert area.get("areas") == [
            {
                "name": "Gate",
                "points": [
                    {"x": 0.0, "z": 0.0},
                    {"x": 2.0, "z": 0.0},
                    {"x": 1.0, "z": 2.0},
                ],
            }
        ]

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        alert_codes = {str(alert.get("code") or "") for alert in res.json().get("alerts", [])}
        assert alert_codes <= {"vision_model_artifact_missing"}



def test_doorbell_visitor_requires_discovered_signal_and_saves_disabled(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch, include_ai=True) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)
        descriptors = []
        async def discovered(*, camera_id):
            assert camera_id == "cam1"
            return {"available": True, "boolean_states": descriptors}
        client.app.state.services.register("cameras.onvif_events.list", discovered)
        body = {"preset": "doorbell_visitor", "area_id": "area-1", "composition_id": "yard",
                "enabled": True, "event_topic": "fixture/doorbell", "event_item_name": "Pressed"}
        endpoint = "/api/cameras/cameras/cam1/pipelines/presets"
        assert client.post(endpoint, json={**body, "event_topic": ""}).status_code == 400
        assert client.post(endpoint, json=body).status_code == 409
        descriptors.append({"topic": "fixture/doorbell", "item_name": "Pressed", "is_boolean": False, "is_property": True})
        assert client.post(endpoint, json=body).status_code == 409
        descriptors[0]["is_boolean"] = True
        preview = client.post(endpoint + "/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert preview.json()["pipeline"]["enabled"] is False
        saved = client.post(endpoint, json=body)
        assert saved.status_code == 200, saved.text
        pipeline = client.get("/api/pipelines/" + saved.json()["pipeline_name"]).json()
        assert pipeline["enabled"] is False
        assert _node_config(pipeline, "vision.detect")["categories"] == ["person"]
        assert _node_config(pipeline, "camera.area_occurrence")["categories"] == ["person"]
        assert _node_config(pipeline, "camera.recent_onvif_event")["topic"] == "fixture/doorbell"
        assert "onvif_context" in _node_config(pipeline, "core.notify")["include_payload_paths"]
        assert "quem acionou" in _node_config(pipeline, "core.notify")["description"]
        edges = pipeline["graph"]["edges"]
        assert any(edge["from"]["node"] == "track" and edge["to"]["node"] == "event_context" for edge in edges)
        assert any(edge["from"]["node"] == "event_context" and edge["to"]["node"] == "area" for edge in edges)


def test_assistant_doorbell_uses_explicit_cached_signal_without_discovery(tmp_path, monkeypatch):
    import asyncio
    import time
    from types import SimpleNamespace
    from toposync_ext_ai.router import AiRouter
    from toposync_ext_cameras.onvif.events import OnvifEventDescriptor

    answer = {"preset": "doorbell_visitor", "camera_id": "cam1", "source_id": "main",
              "composition_id": "yard", "area_id": "area-1",
              "event_topic": "fixture/doorbell", "event_item_name": "Pressed"}
    async def selection(self, **kwargs):
        return answer
    monkeypatch.setattr(AiRouter, "generate_local_json", selection)
    with _create_client(tmp_path, monkeypatch, include_ai=True) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)
        manager = client.app.state.camera_onvif_event_manager
        async def forbidden(*args, **kwargs):
            raise AssertionError("Assistant must not discover or start camera")
        monkeypatch.setattr(manager, "_ensure_runtime", forbidden)
        route = "/api/ai/assistant/recipes/preview"
        instruction = "Na área Gate, associe Campainha de ensaio à campainha e observe visitantes"
        body = {"instruction": instruction}
        missing = client.post(route, json=body)
        assert missing.status_code == 200
        assert "Descubra" in missing.json()["question"]
        cached = SimpleNamespace(available=True, descriptors_loaded_at_ts=time.time(), task=None,
                                 stop_event=asyncio.Event(), descriptors=[OnvifEventDescriptor(
                                     topic="fixture/doorbell", item_name="Pressed", item_type="xs:boolean",
                                     is_property=True, label="Campainha de ensaio")])
        manager._runtimes["cam1"] = cached
        ambiguous = client.post(route, json={"instruction": "Observe visitantes na campainha"})
        assert "Qual sinal" in ambiguous.json()["question"]
        answer["event_topic"] = "invented"
        assert client.post(route, json=body).status_code == 422
        answer["event_topic"] = "fixture/doorbell"
        preview = client.post(route, json=body)
        assert preview.status_code == 200, preview.text
        proposal = preview.json()
        assert any("Campainha de ensaio" in change for change in proposal["changes"])
        applied = client.post("/api/ai/assistant/apply/" + proposal["proposal_id"])
        assert applied.status_code == 200, applied.text
        assert applied.json()["pipeline"]["enabled"] is False
        assert _node_config(applied.json()["pipeline"], "camera.recent_onvif_event")["topic"] == "fixture/doorbell"
        another = client.post(route, json=body).json()
        cached.descriptors_loaded_at_ts -= 3601
        assert client.post("/api/ai/assistant/apply/" + another["proposal_id"]).status_code == 409
        assert manager.cached_boolean_descriptors("cam1") == []
