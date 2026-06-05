from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import numpy as np
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import Pipeline, ProcessingServer
from toposync.runtime.pipelines.operators_sinks import _encode_image_bytes
from toposync.runtime.pipelines.step_snapshots import build_step_input_snapshot_rel_path
import toposync.extensions.manager as ext_manager_mod
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_vision.pipelines import register_vision_pipeline_operators


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def _register_preview_test_operators(client: TestClient) -> None:
    registry = client.app.state.pipeline_operator_registry
    if registry.get("camera.source") is None:
        register_camera_pipeline_operators(registry)
    if registry.get("vision.segment_instances") is None:
        register_vision_pipeline_operators(registry)


def _png_size(blob: bytes) -> tuple[int, int]:
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    width = int.from_bytes(blob[16:20], "big")
    height = int.from_bytes(blob[20:24], "big")
    return width, height


def test_pipeline_preview_frame_replays_upstream_slice_and_skips_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops

    class _FakeFrameGrabber:
        start_calls = 0
        stop_calls = 0

        def __init__(
            self, rtsp_url: str, *, target_fps: float = 15.0, backend: str = "auto", **_kwargs: Any
        ) -> None:
            self.rtsp_url = rtsp_url
            self.target_fps = float(target_fps)
            self.backend = str(backend)
            self._frame = np.zeros((40, 60, 3), dtype=np.uint8)
            self._frame[:, :30, 1] = 255
            self._frame_ts = 1_710_000_000.0

        def start(self) -> "_FakeFrameGrabber":
            type(self).start_calls += 1
            return self

        def get_latest(self) -> tuple[Any, float]:
            return self._frame, self._frame_ts

        def metrics_snapshot(self) -> dict[str, Any]:
            return {"backend": self.backend}

        def stop(self) -> None:
            type(self).stop_calls += 1

    monkeypatch.setattr(camera_ops, "FrameGrabber", _FakeFrameGrabber)

    with _create_client(tmp_path, monkeypatch) as client:
        _register_preview_test_operators(client)

        payload = {
            "pipeline": {
                "name": "preview_crop_runtime",
                "graph": {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "source",
                            "operator": "camera.source",
                            "config": {"rtsp_url": "rtsp://preview-crop-runtime", "fps": 5.0},
                        },
                        {
                            "id": "filter",
                            "operator": "core.filter",
                            "config": {"expression": "False"},
                        },
                        {
                            "id": "throttle",
                            "operator": "core.throttle",
                            "config": {"interval_seconds": 60.0},
                        },
                        {
                            "id": "crop",
                            "operator": "camera.image_crop",
                            "config": {
                                "units": "percent",
                                "left": 25.0,
                                "top": 10.0,
                                "right": 75.0,
                                "bottom": 60.0,
                                "min_crop_size_px": 8,
                            },
                        },
                    ],
                    "edges": [
                        {
                            "from": {"node": "source", "port": "out"},
                            "to": {"node": "filter", "port": "in"},
                        },
                        {
                            "from": {"node": "filter", "port": "out"},
                            "to": {"node": "throttle", "port": "in"},
                        },
                        {
                            "from": {"node": "throttle", "port": "out"},
                            "to": {"node": "crop", "port": "in"},
                        },
                    ],
                },
            },
            "timeout_seconds": 5.0,
            "format": "png",
        }

        response = client.post("/api/pipelines/preview/frame", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-toposync-pipeline-preview-mode"] == "runtime"
    assert _png_size(response.content) == (30, 20)
    assert _FakeFrameGrabber.start_calls == 1
    assert _FakeFrameGrabber.stop_calls == 1


def test_pipeline_preview_frame_falls_back_to_stored_snapshot_for_upstream_segmentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _register_preview_test_operators(client)

        config_store = client.app.state.config_store
        fallback = {
            "pipeline_name": "preview_segmentation_fallback",
            "node_id": "segment",
            "source_id": "camera:adhoc",
        }
        rel_path = build_step_input_snapshot_rel_path(
            pipeline_name=fallback["pipeline_name"],
            node_id=fallback["node_id"],
            source_id=fallback["source_id"],
            filename="input.png",
        )
        fallback_path = config_store.paths.files_dir / rel_path
        frame = np.full((24, 36, 3), 180, dtype=np.uint8)
        blob, _ext, _mime = _encode_image_bytes(frame, fmt="png", jpeg_quality=85)
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        fallback_path.write_bytes(blob)

        payload = {
            "pipeline": {
                "name": "preview_segmentation_runtime",
                "graph": {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "source",
                            "operator": "camera.source",
                            "config": {
                                "rtsp_url": "rtsp://preview-segmentation-fallback",
                                "fps": 5.0,
                            },
                        },
                        {
                            "id": "segment",
                            "operator": "vision.segment_instances",
                            "config": {},
                        },
                    ],
                    "edges": [
                        {
                            "from": {"node": "source", "port": "out"},
                            "to": {"node": "segment", "port": "in"},
                        },
                    ],
                },
            },
            "fallback_snapshot": fallback,
            "timeout_seconds": 5.0,
            "format": "png",
        }

        response = client.post("/api/pipelines/preview/frame", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-toposync-pipeline-preview-mode"] == "fallback_snapshot"
    assert response.content == blob


def test_pipeline_preview_frame_returns_guided_message_when_fallback_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _register_preview_test_operators(client)

        payload = {
            "pipeline": {
                "name": "preview_segmentation_missing",
                "graph": {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "source",
                            "operator": "camera.source",
                            "config": {
                                "rtsp_url": "rtsp://preview-segmentation-missing",
                                "fps": 5.0,
                            },
                        },
                        {
                            "id": "segment",
                            "operator": "vision.segment_instances",
                            "config": {},
                        },
                    ],
                    "edges": [
                        {
                            "from": {"node": "source", "port": "out"},
                            "to": {"node": "segment", "port": "in"},
                        },
                    ],
                },
            },
            "fallback_snapshot": {
                "pipeline_name": "preview_segmentation_missing",
                "node_id": "segment",
                "source_id": "camera:adhoc",
            },
            "timeout_seconds": 5.0,
            "format": "png",
        }

        response = client.post("/api/pipelines/preview/frame", json=payload)

    assert response.status_code == 409
    assert response.json()["detail"] != "File not found"
    assert "Leave the pipeline running until this point" in str(response.json()["detail"])


def test_pipelines_api_crud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.get("/api/pipelines")
        assert res.status_code == 200
        assert res.json() == {"pipelines": []}

        payload = Pipeline(
            name="camera1_tracking",
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
            graph={"schema_version": 2, "nodes": [], "edges": []},
        ).model_dump()
        res = client.put("/api/pipelines/camera1_tracking", json=replacement_payload)
        assert res.status_code == 200
        assert res.json()["name"] == "camera1_alerts"
        assert "type" not in res.json()

        res = client.get("/api/pipelines/camera1_alerts")
        assert res.status_code == 200
        assert res.json()["graph"]["schema_version"] == 2

        res = client.delete("/api/pipelines/camera1_alerts")
        assert res.status_code == 200
        assert res.json()["name"] == "camera1_alerts"

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        assert res.json() == {"pipelines": []}


def test_pipelines_api_emits_lifecycle_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        bus = client.app.state.bus

        def _record_saved(payload: Any, context: dict[str, Any]) -> None:
            events.append(("saved", dict(payload), dict(context)))

        def _record_deleted(payload: Any, context: dict[str, Any]) -> None:
            events.append(("deleted", dict(payload), dict(context)))

        bus.on("core.pipeline.saved", _record_saved)
        bus.on("core.pipeline.deleted", _record_deleted)

        created = client.post(
            "/api/pipelines",
            json=Pipeline(
                name="manual_publish",
                graph={"schema_version": 1, "nodes": [], "edges": []},
            ).model_dump(),
        )
        assert created.status_code == 201, created.text

        replaced = client.put(
            "/api/pipelines/manual_publish",
            json=Pipeline(
                name="manual_publish_v2",
                graph={"schema_version": 1, "nodes": [], "edges": []},
            ).model_dump(),
        )
        assert replaced.status_code == 200, replaced.text

        deleted = client.delete("/api/pipelines/manual_publish_v2")
        assert deleted.status_code == 200, deleted.text

        assert [event[0] for event in events] == ["saved", "saved", "deleted"]
        assert events[0][1]["operation"] == "create"
        assert events[0][1]["pipeline_name"] == "manual_publish"
        assert events[0][2]["source"] == "core.pipelines.api"
        assert events[1][1]["operation"] == "replace"
        assert events[1][1]["pipeline_name"] == "manual_publish_v2"
        assert events[1][1]["previous_name"] == "manual_publish"
        assert events[2][1]["operation"] == "delete"
        assert events[2][1]["pipeline_name"] == "manual_publish_v2"


def test_pipelines_api_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="camera1_tracking",
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump()
        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 201

        res = client.post(
            "/api/pipelines/camera1_tracking/duplicate", json={"new_name": "camera1_tracking_2"}
        )
        assert res.status_code == 201
        assert res.json()["name"] == "camera1_tracking_2"
        assert "type" not in res.json()
        assert res.json()["graph"]["schema_version"] == 1

        res = client.post("/api/pipelines/camera1_tracking/duplicate")
        assert res.status_code == 201
        assert res.json()["name"] == "camera1_tracking_3"

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        names = {item["name"] for item in res.json()["pipelines"]}
        assert names == {"camera1_tracking", "camera1_tracking_2", "camera1_tracking_3"}

        res = client.post(
            "/api/pipelines/camera1_tracking/duplicate", json={"new_name": "camera1_tracking_2"}
        )
        assert res.status_code == 409

        res = client.post(
            "/api/pipelines/unknown_pipeline/duplicate", json={"new_name": "unknown_pipeline_2"}
        )
        assert res.status_code == 404


def test_pipelines_api_duplicate_python_pipeline_adds_pipeline_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="dsl_pipeline",
            editor_mode="python",
            python_source='dsl_pipeline = core.demo_frame_sequence_source(_id="source") | core.notify(_id="notify")',
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump(mode="json")
        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 201

        res = client.post(
            "/api/pipelines/dsl_pipeline/duplicate", json={"new_name": "dsl_pipeline_2"}
        )
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
            "graph": {"schema_version": 1},
        }
        res = client.post("/api/pipelines", json=invalid_name)
        assert res.status_code == 422

        missing_graph_schema_version = {
            "name": "camera1_tracking",
            "graph": {"nodes": []},
        }
        res = client.post("/api/pipelines", json=missing_graph_schema_version)
        assert res.status_code == 422


def test_pipeline_rejects_edges_from_sink_operators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = {
            "name": "invalid_notify_chain",
            "graph": {
                "schema_version": 1,
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {"id": "notify", "operator": "core.notify", "config": {}},
                    {"id": "debug", "operator": "core.debug", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "notify", "port": "in"},
                    },
                    {
                        "from": {"node": "notify", "port": "out"},
                        "to": {"node": "debug", "port": "in"},
                    },
                ],
            },
        }

        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 400
        assert "has no output port 'out'" in str(res.json().get("detail") or "")


def test_pipeline_publish_video_host_mismatch_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_pipelines_telemetry_aggregate_endpoints_filter_by_pipeline_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        telemetry_store = getattr(client.app.state, "pipeline_telemetry_store", None)
        assert telemetry_store is not None
        now_s = time.time()

        telemetry_store.observe_numeric("pipe_a", "node", "motion.score", 0.2, now_s=now_s)
        telemetry_store.observe_numeric("pipe_b", "node", "motion.score", 0.9, now_s=now_s)
        telemetry_store.record_image_marker(
            "pipe_a",
            node_id="node",
            rel_path="pipelines/a/frame.png",
            metric_id="store.image",
            ts_s=now_s,
        )
        telemetry_store.record_image_marker(
            "pipe_b",
            node_id="node",
            rel_path="pipelines/b/frame.png",
            metric_id="store.image",
            ts_s=now_s,
        )

        numeric_res = client.get(
            "/api/pipelines/telemetry/all/numeric",
            params=[
                ("metric_id", "motion.score"),
                ("pipeline_name", "pipe_a"),
                ("window_seconds", "3600"),
            ],
        )
        assert numeric_res.status_code == 200
        numeric_body = numeric_res.json()
        assert numeric_body["aggregation"] == "max"
        assert len(numeric_body["series"]) == 1
        assert numeric_body["series"][0]["pipeline_count"] == 1
        assert numeric_body["series"][0]["series_count"] == 1
        assert [point["avg"] for point in numeric_body["series"][0]["points"]] == [0.2]

        markers_res = client.get(
            "/api/pipelines/telemetry/all/image-markers",
            params=[
                ("metric_id", "store.image"),
                ("pipeline_name", "pipe_b"),
                ("window_seconds", "3600"),
            ],
        )
        assert markers_res.status_code == 200
        markers_body = markers_res.json()
        assert markers_body["pipeline_count"] == 1
        assert [item["pipeline_name"] for item in markers_body["markers"]] == ["pipe_b"]
        assert [item["rel_path"] for item in markers_body["markers"]] == ["pipelines/b/frame.png"]


def test_pipeline_storage_summary_and_cleanup_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="storage_api",
            enabled=False,
            graph={
                "schema_version": 1,
                "limits": {"storage_max_bytes": 80},
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {"format": "png", "max_files_per_layer": 1},
                    },
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "store", "port": "in"},
                    }
                ],
            },
        ).model_dump(mode="json")
        created = client.post("/api/pipelines", json=payload)
        assert created.status_code == 201

        storage_manager = getattr(client.app.state, "pipeline_storage_manager", None)
        telemetry_store = getattr(client.app.state, "pipeline_telemetry_store", None)
        assert storage_manager is not None
        assert telemetry_store is not None

        first = storage_manager.store_blob(
            pipeline_name="storage_api",
            node_id="store",
            artifact_name="main",
            layer_label="Original",
            filename_hint="first",
            ext=".bin",
            mime_type="application/octet-stream",
            blob=b"a" * 50,
            frame_ts=time.time() - 10,
        )
        second = storage_manager.store_blob(
            pipeline_name="storage_api",
            node_id="store",
            artifact_name="main",
            layer_label="Original",
            filename_hint="second",
            ext=".bin",
            mime_type="application/octet-stream",
            blob=b"b" * 50,
            frame_ts=time.time(),
        )
        telemetry_store.record_image_marker(
            "storage_api",
            node_id="store",
            rel_path=first.rel_path,
            metric_id="store.image",
            ts_s=time.time() - 10,
        )
        telemetry_store.record_image_marker(
            "storage_api",
            node_id="store",
            rel_path=second.rel_path,
            metric_id="store.image",
            ts_s=time.time(),
        )

        summary_res = client.get("/api/pipelines/storage_api/storage")
        assert summary_res.status_code == 200
        summary = summary_res.json()
        assert summary["used_bytes"] == 100
        assert summary["limit_bytes"] == 80
        assert summary["file_count"] == 2
        assert summary["over_limit"] is True
        assert summary["layers"][0]["layer_label"] == "Original"

        cleanup_res = client.post("/api/pipelines/storage_api/storage/cleanup")
        assert cleanup_res.status_code == 200
        cleaned = cleanup_res.json()
        assert cleaned["file_count"] == 1
        assert cleaned["used_bytes"] == 50
        assert cleaned["layers"][0]["file_count"] == 1

        markers = telemetry_store.list_image_markers("storage_api")
        assert [item["rel_path"] for item in markers] == [second.rel_path]

        purge_res = client.post(
            "/api/pipelines/storage_api/storage/cleanup",
            params={"purge": "true"},
        )
        assert purge_res.status_code == 200
        purged = purge_res.json()
        assert purged["file_count"] == 0
        assert purged["used_bytes"] == 0
        assert purged["layers"] == []
        assert not (tmp_path / "files" / second.rel_path).exists()
        assert telemetry_store.list_image_markers("storage_api") == []


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
            url="http://192.168.1.50:49321",
            username="test-user",
            password="secret",
        ).model_dump()
        res = client.put("/api/processing-servers/remote_gpu", json=server)
        assert res.status_code == 200
        assert res.json()["id"] == "remote_gpu"
        assert res.json()["url"] == "http://192.168.1.50:49321"
        assert res.json()["username"] == "test-user"
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


def test_pipeline_compile_returns_recommendations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        pipeline = Pipeline(
            name="demo_pipeline",
            graph={
                "schema_version": 1,
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {"id": "notify", "operator": "core.notify", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "notify", "port": "in"},
                    },
                ],
            },
        ).model_dump(mode="json")
        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200
        body = res.json()
        assert isinstance(body.get("alerts"), list)
        assert any(item.get("code") == "notify_missing_store_images" for item in body["alerts"])


def test_pipeline_telemetry_endpoints_return_numeric_and_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="telemetry_pipeline",
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
        telemetry_store.observe_numeric(
            "telemetry_pipeline", "gate", "motion.score", 0.021, now_s=now
        )
        telemetry_store.observe_numeric(
            "telemetry_pipeline", "gate", "motion.score", 0.037, now_s=now + 1.0
        )
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


def test_pipelines_telemetry_overview_endpoints_aggregate_all_pipelines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        for pipeline_name in ("alpha_pipeline", "beta_pipeline"):
            payload = Pipeline(
                name=pipeline_name,
                graph={"schema_version": 1, "nodes": [], "edges": []},
            ).model_dump(mode="json")
            created = client.post("/api/pipelines", json=payload)
            assert created.status_code == 201

        telemetry_store = getattr(client.app.state, "pipeline_telemetry_store", None)
        assert telemetry_store is not None
        now = time.time()

        telemetry_store.observe_numeric(
            "alpha_pipeline", "motion_a", "motion.score", 0.22, now_s=now - 120.0
        )
        telemetry_store.observe_numeric(
            "beta_pipeline", "motion_b", "motion.score", 0.81, now_s=now - 120.0
        )
        telemetry_store.observe_numeric(
            "alpha_pipeline", "yolo_a", "vision.confidence", 0.33, now_s=now - 30.0
        )
        telemetry_store.observe_numeric(
            "beta_pipeline", "yolo_b", "vision.confidence", 0.67, now_s=now - 30.0
        )

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
            params=[
                ("metric_id", "motion.score"),
                ("metric_id", "vision.confidence"),
                ("point_limit", "200"),
            ],
        )
        assert numeric_res.status_code == 200
        numeric_body = numeric_res.json()
        assert numeric_body["aggregation"] == "max"
        assert [item["metric_id"] for item in numeric_body["series"]] == [
            "motion.score",
            "vision.confidence",
        ]
        motion_series = next(
            item for item in numeric_body["series"] if item["metric_id"] == "motion.score"
        )
        yolo_series = next(
            item for item in numeric_body["series"] if item["metric_id"] == "vision.confidence"
        )
        assert motion_series["pipeline_count"] == 2
        assert motion_series["series_count"] == 2
        assert motion_series["points"][-1]["avg"] == pytest.approx(0.81)
        assert yolo_series["points"][-1]["avg"] == pytest.approx(0.67)

        markers_res = client.get("/api/pipelines/telemetry/all/image-markers", params={"limit": 20})
        assert markers_res.status_code == 200
        markers_body = markers_res.json()
        assert markers_body["aggregation"] == "max"
        assert markers_body["pipeline_count"] == 2
        assert [item["pipeline_name"] for item in markers_body["markers"]] == [
            "alpha_pipeline",
            "beta_pipeline",
        ]
        assert [item["rel_path"] for item in markers_body["markers"]] == [
            "pipelines/alpha_pipeline/frame_1.png",
            "pipelines/beta_pipeline/frame_2.png",
        ]
