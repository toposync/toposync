from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import Pipeline, ProcessingServer
import toposync.extensions.manager as ext_manager_mod


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def test_pipelines_api_crud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.get("/api/pipelines")
        assert res.status_code == 200
        assert res.json() == {"pipelines": []}

        payload = Pipeline(
            name="camera1_tracking",
            type="reuse",
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump()
        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 201
        assert res.json()["name"] == "camera1_tracking"

        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 409

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        body = res.json()
        assert len(body["pipelines"]) == 1
        assert body["pipelines"][0]["name"] == "camera1_tracking"

        replacement_payload = Pipeline(
            name="camera1_alerts",
            type="final",
            graph={"schema_version": 2, "nodes": [], "edges": []},
        ).model_dump()
        res = client.put("/api/pipelines/camera1_tracking", json=replacement_payload)
        assert res.status_code == 200
        assert res.json()["name"] == "camera1_alerts"
        assert res.json()["type"] == "final"

        res = client.get("/api/pipelines/camera1_alerts")
        assert res.status_code == 200
        assert res.json()["graph"]["schema_version"] == 2

        res = client.delete("/api/pipelines/camera1_alerts")
        assert res.status_code == 200
        assert res.json()["name"] == "camera1_alerts"

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        assert res.json() == {"pipelines": []}


def test_pipelines_api_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="camera1_tracking",
            type="reuse",
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump()
        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 201

        res = client.post("/api/pipelines/camera1_tracking/duplicate", json={"new_name": "camera1_tracking_2"})
        assert res.status_code == 201
        assert res.json()["name"] == "camera1_tracking_2"
        assert res.json()["type"] == "reuse"
        assert res.json()["graph"]["schema_version"] == 1

        res = client.post("/api/pipelines/camera1_tracking/duplicate")
        assert res.status_code == 201
        assert res.json()["name"] == "camera1_tracking_3"

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        names = {item["name"] for item in res.json()["pipelines"]}
        assert names == {"camera1_tracking", "camera1_tracking_2", "camera1_tracking_3"}

        res = client.post("/api/pipelines/camera1_tracking/duplicate", json={"new_name": "camera1_tracking_2"})
        assert res.status_code == 409

        res = client.post("/api/pipelines/unknown_pipeline/duplicate", json={"new_name": "unknown_pipeline_2"})
        assert res.status_code == 404


def test_pipelines_api_duplicate_python_pipeline_adds_pipeline_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="dsl_pipeline",
            type="final",
            editor_mode="python",
            python_source='dsl_pipeline = core.demo_frame_sequence_source(_id="source") | core.notify(_id="notify")',
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump(mode="json")
        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 201

        res = client.post("/api/pipelines/dsl_pipeline/duplicate", json={"new_name": "dsl_pipeline_2"})
        assert res.status_code == 201
        duplicated = res.json()
        assert duplicated["name"] == "dsl_pipeline_2"
        assert duplicated["editor_mode"] == "python"
        assert "PIPELINE = dsl_pipeline" in str(duplicated.get("python_source") or "")

        res = client.post("/api/pipelines/compile-python", json={"pipeline": duplicated})
        assert res.status_code == 200
        compiled = res.json()
        assert compiled["graph"]["schema_version"] == 1
        assert {node["id"] for node in compiled["graph"]["nodes"]} == {"source", "notify"}


def test_pipeline_payload_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        invalid_name = {
            "name": "bad-name",
            "type": "reuse",
            "graph": {"schema_version": 1},
        }
        res = client.post("/api/pipelines", json=invalid_name)
        assert res.status_code == 422

        missing_graph_schema_version = {
            "name": "camera1_tracking",
            "type": "reuse",
            "graph": {"nodes": []},
        }
        res = client.post("/api/pipelines", json=missing_graph_schema_version)
        assert res.status_code == 422


def test_pipeline_publish_video_host_mismatch_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        patch_res = client.patch(
            "/api/settings/extensions/com.toposync.streaming",
            json={
                "transmissions": [
                    {
                        "id": "tx_edge",
                        "name": "Edge transmission",
                        "host_server_id": "edge_gpu",
                        "path": "edge-cam",
                        "outputs": [],
                    }
                ]
            },
        )
        assert patch_res.status_code == 200

        payload = {
            "name": "pipeline_with_publish_video",
            "type": "final",
            "processing_server_id": "local",
            "graph": {
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "stream_sink",
                        "operator": "stream.publish_video",
                        "config": {"transmission_id": "tx_edge"},
                    }
                ],
                "edges": [],
            },
        }
        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 400
        assert "stream.publish_video host mismatch" in str(res.json().get("detail") or "")


def test_processing_servers_api_crud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.get("/api/processing-servers")
        assert res.status_code == 200
        body = res.json()
        assert isinstance(body.get("servers"), list)
        assert any(item.get("id") == "local" for item in body["servers"])

        server = ProcessingServer(
            id="remote_gpu",
            name="Garage GPU",
            kind="http",
            url="http://192.168.1.50:9001",
            username="mateus",
            password="secret",
        ).model_dump()
        res = client.put("/api/processing-servers/remote_gpu", json=server)
        assert res.status_code == 200
        assert res.json()["id"] == "remote_gpu"
        assert res.json()["url"] == "http://192.168.1.50:9001"
        assert res.json()["username"] == "mateus"
        assert res.json()["password"] == "secret"

        res = client.get("/api/processing-servers")
        assert res.status_code == 200
        servers = res.json()["servers"]
        assert any(item.get("id") == "remote_gpu" for item in servers)

        res = client.get("/api/processing-servers/local/status")
        assert res.status_code == 200
        status_body = res.json()
        assert status_body["ok"] is True
        assert isinstance(status_body.get("status"), dict)
        assert "system" in status_body["status"]
        assert "vision" in status_body["status"]
        assert "cameras" in status_body["status"]

        res = client.delete("/api/processing-servers/remote_gpu")
        assert res.status_code == 200
        assert res.json()["id"] == "remote_gpu"


def test_pipeline_compile_returns_recommendations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        pipeline = Pipeline(
            name="demo_pipeline",
            type="final",
            graph={
                "schema_version": 1,
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {"id": "notify", "operator": "core.notify", "config": {}},
                ],
                "edges": [
                    {"from": {"node": "source", "port": "out"}, "to": {"node": "notify", "port": "in"}},
                ],
            },
        ).model_dump(mode="json")
        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200
        body = res.json()
        assert isinstance(body.get("alerts"), list)
        assert any(item.get("code") == "notify_missing_store_images" for item in body["alerts"])


def test_pipeline_telemetry_endpoints_return_numeric_and_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="telemetry_pipeline",
            type="final",
            graph={
                "schema_version": 1,
                "nodes": [],
                "edges": [],
            },
        ).model_dump(mode="json")
        created = client.post("/api/pipelines", json=payload)
        assert created.status_code == 201

        telemetry_store = getattr(client.app.state, "pipeline_telemetry_store", None)
        assert telemetry_store is not None
        now = time.time()
        telemetry_store.observe_numeric("telemetry_pipeline", "gate", "motion.score", 0.021, now_s=now)
        telemetry_store.observe_numeric("telemetry_pipeline", "gate", "motion.score", 0.037, now_s=now + 1.0)
        telemetry_store.record_image_marker(
            "telemetry_pipeline",
            node_id="store",
            metric_id="store.image",
            rel_path="pipelines/telemetry_pipeline/frame_1.png",
            ts_s=now - 120.0,
        )
        telemetry_store.record_image_marker(
            "telemetry_pipeline",
            node_id="store",
            metric_id="store.image",
            rel_path="pipelines/telemetry_pipeline/frame_2.png",
            ts_s=now - 5.0,
        )

        numeric_res = client.get(
            "/api/pipelines/telemetry_pipeline/telemetry/numeric",
            params={"node_id": "gate", "metric_id": "motion.score", "point_limit": 200},
        )
        assert numeric_res.status_code == 200
        numeric_body = numeric_res.json()
        assert numeric_body["pipeline_name"] == "telemetry_pipeline"
        assert numeric_body["node_id"] == "gate"
        assert numeric_body["metric_id"] == "motion.score"
        assert int(numeric_body["total_count"]) == 2
        assert isinstance(numeric_body.get("histogram_bins"), list)

        markers_res = client.get(
            "/api/pipelines/telemetry_pipeline/telemetry/image-markers",
            params={"metric_id": "store.image", "limit": 10},
        )
        assert markers_res.status_code == 200
        markers_body = markers_res.json()
        assert markers_body["pipeline_name"] == "telemetry_pipeline"
        assert isinstance(markers_body.get("markers"), list)
        assert len(markers_body["markers"]) == 2
        assert markers_body["markers"][0]["rel_path"] == "pipelines/telemetry_pipeline/frame_1.png"

        recent_markers_res = client.get(
            "/api/pipelines/telemetry_pipeline/telemetry/image-markers",
            params={"metric_id": "store.image", "limit": 10, "window_seconds": 30},
        )
        assert recent_markers_res.status_code == 200
        recent_markers_body = recent_markers_res.json()
        assert [item["rel_path"] for item in recent_markers_body["markers"]] == [
            "pipelines/telemetry_pipeline/frame_2.png"
        ]


def test_pipelines_telemetry_overview_endpoints_aggregate_all_pipelines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        for pipeline_name in ("alpha_pipeline", "beta_pipeline"):
            payload = Pipeline(
                name=pipeline_name,
                type="final",
                graph={"schema_version": 1, "nodes": [], "edges": []},
            ).model_dump(mode="json")
            created = client.post("/api/pipelines", json=payload)
            assert created.status_code == 201

        telemetry_store = getattr(client.app.state, "pipeline_telemetry_store", None)
        assert telemetry_store is not None
        now = time.time()

        telemetry_store.observe_numeric("alpha_pipeline", "motion_a", "motion.score", 0.22, now_s=now - 120.0)
        telemetry_store.observe_numeric("beta_pipeline", "motion_b", "motion.score", 0.81, now_s=now - 120.0)
        telemetry_store.observe_numeric("alpha_pipeline", "yolo_a", "vision.confidence", 0.33, now_s=now - 30.0)
        telemetry_store.observe_numeric("beta_pipeline", "yolo_b", "vision.confidence", 0.67, now_s=now - 30.0)

        telemetry_store.record_image_marker(
            "alpha_pipeline",
            node_id="store_a",
            metric_id="store.image",
            rel_path="pipelines/alpha_pipeline/frame_1.png",
            ts_s=now - 90.0,
        )
        telemetry_store.record_image_marker(
            "beta_pipeline",
            node_id="store_b",
            metric_id="store.image",
            rel_path="pipelines/beta_pipeline/frame_2.png",
            ts_s=now - 15.0,
        )

        numeric_res = client.get(
            "/api/pipelines/telemetry/all/numeric",
            params=[("metric_id", "motion.score"), ("metric_id", "vision.confidence"), ("point_limit", "200")],
        )
        assert numeric_res.status_code == 200
        numeric_body = numeric_res.json()
        assert numeric_body["aggregation"] == "max"
        assert [item["metric_id"] for item in numeric_body["series"]] == ["motion.score", "vision.confidence"]
        motion_series = next(item for item in numeric_body["series"] if item["metric_id"] == "motion.score")
        yolo_series = next(item for item in numeric_body["series"] if item["metric_id"] == "vision.confidence")
        assert motion_series["pipeline_count"] == 2
        assert motion_series["series_count"] == 2
        assert motion_series["points"][-1]["avg"] == pytest.approx(0.81)
        assert yolo_series["points"][-1]["avg"] == pytest.approx(0.67)

        markers_res = client.get("/api/pipelines/telemetry/all/image-markers", params={"limit": 20})
        assert markers_res.status_code == 200
        markers_body = markers_res.json()
        assert markers_body["aggregation"] == "max"
        assert markers_body["pipeline_count"] == 2
        assert [item["pipeline_name"] for item in markers_body["markers"]] == ["alpha_pipeline", "beta_pipeline"]
        assert [item["rel_path"] for item in markers_body["markers"]] == [
            "pipelines/alpha_pipeline/frame_1.png",
            "pipelines/beta_pipeline/frame_2.png",
        ]
