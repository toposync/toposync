from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import AppConfig, AppSettings, Composition, CompositionElement, Pipeline, Vector3
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.recommendations import PipelineAlert, analyze_compiled_pipeline
import toposync.extensions.manager as ext_manager_mod
from toposync_ext_cameras.pipelines.operators import register_camera_pipeline_operators


def _create_client_with_cameras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

    def _eps(_group: str):
        return [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            ),
        ]

    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", _eps)
    return TestClient(create_app())


def _valid_control_points() -> list[dict[str, object]]:
    return [
        {"id": "A", "image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
        {"id": "B", "image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
        {"id": "C", "image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
        {"id": "D", "image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
    ]


def _valid_calibrated_views() -> list[dict[str, object]]:
    return [
        {
            "id": "main",
            "label": "Vista principal",
            "pose_reference": None,
            "stream_scope": {"compatible_roles": ["main", "sub"], "compatible_source_ids": []},
            "projection_model": {
                "type": "image_quad_on_world",
                "image_region": {"top_left": {"x": 0.0, "y": 0.0}, "bottom_right": {"x": 1.0, "y": 1.0}},
                "world_quad": {
                    "top_left": {"x": 0.0, "z": 0.0},
                    "top_right": {"x": 10.0, "z": 0.0},
                    "bottom_right": {"x": 10.0, "z": 10.0},
                    "bottom_left": {"x": 0.0, "z": 10.0},
                },
                "refinement": None,
            },
            "projection_quality": {"status": "ready", "estimated": False, "note": None},
        }
    ]


def _camera_composition(
    *,
    camera_id: str = "cam1",
    composition_id: str = "yard",
    control_points: list[dict[str, object]] | None = None,
    calibrated_views: list[dict[str, object]] | None = None,
) -> Composition:
    return Composition(
        id=composition_id,
        name=composition_id.title(),
        elements=[
            CompositionElement(
                id=f"{camera_id}-element",
                type="com.toposync.cameras.camera",
                name=f"Camera {camera_id}",
                position=Vector3(),
                rotation=Vector3(),
                props={
                    "camera_id": camera_id,
                    **(
                        {"calibrated_views": calibrated_views}
                        if calibrated_views is not None
                        else {
                            "control_point_sets": [
                                {
                                    "id": "main",
                                    "label": "Main",
                                    "control_points": control_points if control_points is not None else _valid_control_points(),
                                }
                            ]
                        }
                    ),
                },
            )
        ],
    )


def _camera_mapping_alerts(
    *,
    mapping_config: dict[str, object] | None = None,
    compositions: list[Composition] | None = None,
) -> list[PipelineAlert]:
    registry = OperatorRegistry()
    register_camera_pipeline_operators(registry)
    pipeline = Pipeline(
        name="mapping_diagnostics",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "camera.source", "config": {"camera_id": "cam1"}},
                {"id": "map", "operator": "camera.camera_mapping", "config": dict(mapping_config or {})},
            ],
            "edges": [{"from": {"node": "source"}, "to": {"node": "map", "port": "in"}}],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    return analyze_compiled_pipeline(
        pipeline=compiled,
        registry=registry,
        context={"compositions": [item.model_dump(mode="json") for item in (compositions or [])]},
    )


def _camera_mapping_alert_codes(alerts: list[PipelineAlert]) -> set[str]:
    return {alert.code for alert in alerts if alert.code.startswith("camera_mapping_")}


def test_control_points_map_accepts_control_point_set_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        res = client.post(
            "/api/cameras/control_points/map",
            json={
                "control_point_set": {
                    "id": "main",
                    "label": "Vista principal",
                    "pose_reference": None,
                    "control_points": [
                        {"id": "A", "image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                        {"id": "B", "image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                        {"id": "C", "image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                        {"id": "D", "image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                    ],
                },
                "query": {"kind": "image", "x": 0.5, "y": 0.5},
            },
        )

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["world"]["x"] == pytest.approx(5.0, abs=1e-6)
        assert body["world"]["z"] == pytest.approx(5.0, abs=1e-6)
        assert body["quality"]["number_of_points"] == 4
        assert body["quality"]["number_of_inliers"] == 4


def test_projection_map_accepts_calibrated_view_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        res = client.post(
            "/api/cameras/projection/map",
            json={
                "calibrated_view": _valid_calibrated_views()[0],
                "query": {"kind": "image", "x": 0.5, "y": 0.5},
            },
        )

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["world"]["x"] == pytest.approx(5.0, abs=1e-6)
        assert body["world"]["z"] == pytest.approx(5.0, abs=1e-6)
        assert body["quality"]["number_of_points"] == 4


def test_projection_map_applies_calibrated_view_refinement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _valid_calibrated_views()[0]
    projection_model = view["projection_model"]
    assert isinstance(projection_model, dict)
    projection_model["refinement"] = {
        "model": "local_rbf_v1",
        "points": [
            {
                "id": "center",
                "image": {"x": 0.5, "y": 0.5},
                "world": {"x": 7.0, "z": 3.0},
            }
        ],
    }

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        center_res = client.post(
            "/api/cameras/projection/map",
            json={"calibrated_view": view, "query": {"kind": "image", "x": 0.5, "y": 0.5}},
        )
        corner_res = client.post(
            "/api/cameras/projection/map",
            json={"calibrated_view": view, "query": {"kind": "image", "x": 0.0, "y": 0.0}},
        )

    assert center_res.status_code == 200, center_res.text
    assert corner_res.status_code == 200, corner_res.text
    assert center_res.json()["world"]["x"] == pytest.approx(7.0, abs=1e-6)
    assert center_res.json()["world"]["z"] == pytest.approx(3.0, abs=1e-6)
    assert corner_res.json()["world"]["x"] == pytest.approx(0.0, abs=1e-6)
    assert corner_res.json()["world"]["z"] == pytest.approx(0.0, abs=1e-6)


def test_camera_mapping_diagnostics_reports_camera_without_composition() -> None:
    alerts = _camera_mapping_alerts(compositions=[])

    assert "camera_mapping_camera_not_in_composition" in _camera_mapping_alert_codes(alerts)
    assert any(
        alert.severity == "error" and alert.node_id == "map"
        for alert in alerts
        if alert.code == "camera_mapping_camera_not_in_composition"
    )


def test_camera_mapping_diagnostics_reports_missing_control_points() -> None:
    alerts = _camera_mapping_alerts(
        compositions=[_camera_composition(control_points=_valid_control_points()[:3])]
    )

    assert _camera_mapping_alert_codes(alerts) == {"camera_mapping_control_points_missing"}


def test_camera_mapping_diagnostics_accepts_calibrated_composition() -> None:
    alerts = _camera_mapping_alerts(
        compositions=[_camera_composition(calibrated_views=_valid_calibrated_views())]
    )

    assert _camera_mapping_alert_codes(alerts) == set()


def test_camera_mapping_diagnostics_inline_calibrated_views_skip_composition_check() -> None:
    alerts = _camera_mapping_alerts(mapping_config={"calibrated_views": _valid_calibrated_views()}, compositions=[])

    assert _camera_mapping_alert_codes(alerts) == set()


def test_camera_mapping_diagnostics_reports_unknown_composition_id() -> None:
    alerts = _camera_mapping_alerts(
        mapping_config={"composition_id": "missing"},
        compositions=[_camera_composition(composition_id="yard")],
    )

    assert _camera_mapping_alert_codes(alerts) == {"camera_mapping_composition_missing"}


def test_camera_mapping_diagnostics_reports_camera_missing_from_selected_composition() -> None:
    alerts = _camera_mapping_alerts(
        mapping_config={"composition_id": "front"},
        compositions=[
            _camera_composition(camera_id="cam2", composition_id="front"),
            _camera_composition(camera_id="cam1", composition_id="yard"),
        ],
    )

    assert _camera_mapping_alert_codes(alerts) == {"camera_mapping_camera_not_in_composition"}


def test_camera_mapping_diagnostics_inline_control_points_skip_composition_check() -> None:
    alerts = _camera_mapping_alerts(
        mapping_config={
            "control_point_sets": [
                {
                    "id": "inline",
                    "label": "Inline",
                    "control_points": _valid_control_points(),
                }
            ]
        },
        compositions=[],
    )

    assert _camera_mapping_alert_codes(alerts) == set()


def test_pipeline_compile_returns_camera_mapping_diagnostic_from_compositions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.save_config,
            AppConfig(
                compositions=[Composition(id="ground", name="Ground", elements=[])],
                active_composition_id="ground",
            ),
        )
        pipeline = Pipeline(
            name="mapping_diagnostics",
            graph={
                "schema_version": 1,
                "nodes": [
                    {"id": "source", "operator": "camera.source", "config": {"camera_id": "cam1"}},
                    {"id": "map", "operator": "camera.camera_mapping", "config": {}},
                ],
                "edges": [{"from": {"node": "source"}, "to": {"node": "map", "port": "in"}}],
            },
        )

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline.model_dump(mode="json")})

        assert res.status_code == 200, res.text
        alerts = res.json()["alerts"]
        assert any(
            item["severity"] == "error"
            and item["code"] == "camera_mapping_camera_not_in_composition"
            and item["node_id"] == "map"
            for item in alerts
        )


def test_camera_contexts_reports_control_point_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.save_config,
            AppConfig(
                settings=AppSettings(
                    extensions={
                        "com.toposync.cameras": {
                            "cameras": [
                                {
                                    "id": "cam1",
                                    "name": "Camera 1",
                                    "connection_type": "rtsp",
                                    "rtsp_url": "rtsp://example.local/stream",
                                    "username": "",
                                    "password": "",
                                    "fps": 5,
                                }
                            ]
                        }
                    }
                ),
                compositions=[
                    Composition(
                        id="yard",
                        name="Yard",
                        elements=[
                            CompositionElement(
                                id="cam-element",
                                type="com.toposync.cameras.camera",
                                name="Camera 1",
                                position=Vector3(),
                                rotation=Vector3(),
                                props={
                                    "camera_id": "cam1",
                                    "control_point_sets": [
                                        {
                                            "id": "main",
                                            "label": "Vista principal",
                                            "control_points": [
                                                {"id": "A", "image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                                {"id": "B", "image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                                {"id": "C", "image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                                                {"id": "D", "image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                            ],
                                        },
                                        {
                                            "id": "door",
                                            "label": "Porta",
                                            "pose_reference": {"pan": 0.1, "tilt": -0.2, "zoom": 0.3},
                                            "control_points": [
                                                {"id": "A", "image": {"x": 0.1, "y": 0.1}, "world": {"x": 1.0, "z": 1.0}},
                                                {"id": "B", "image": {"x": 0.2, "y": 0.2}, "world": {"x": 2.0, "z": 2.0}},
                                                {"id": "C", "image": {"x": 0.3, "y": 0.3}, "world": {"x": 3.0, "z": 3.0}},
                                            ],
                                        },
                                    ],
                                },
                            ),
                            CompositionElement(
                                id="area-1",
                                type="com.toposync.structural.area",
                                name="Gate",
                                position=Vector3(),
                                rotation=Vector3(),
                                props={
                                    "vertices": [
                                        {"x": 0.0, "z": 0.0},
                                        {"x": 2.0, "z": 0.0},
                                        {"x": 1.0, "z": 2.0},
                                    ]
                                },
                            ),
                        ],
                    )
                ],
                active_composition_id="yard",
            ),
        )

        res = client.get("/api/cameras/cameras/cam1/contexts")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["camera_id"] == "cam1"
        assert len(body["compositions"]) == 1
        composition = body["compositions"][0]
        assert composition["id"] == "yard"
        assert composition["camera_elements"][0]["control_points_pairs"] == 7
        assert composition["camera_elements"][0]["has_mapping"] is True
        assert composition["areas"][0]["id"] == "area-1"
        assert composition["areas"][0]["name"] == "Gate"
        assert composition["areas"][0]["vertices_count"] == 3
        assert composition["areas"][0]["vertices"] == [
            {"x": 0.0, "z": 0.0},
            {"x": 2.0, "z": 0.0},
            {"x": 1.0, "z": 2.0},
        ]


def test_camera_ptz_routes_forward_to_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        services = client.app.state.services

        async def list_presets(*, camera_id: str, camera_source_id: str | None = None):
            assert camera_id == "cam1"
            assert camera_source_id is None
            return [{"token": "home", "name": "Home", "pan": 0.1, "tilt": -0.2, "zoom": 0.3}]

        async def goto_preset(*, camera_id: str, preset_token: str, camera_source_id: str | None = None):
            assert camera_id == "cam1"
            assert camera_source_id is None
            assert preset_token == "home"
            return {"ok": True}

        async def get_status(*, camera_id: str, camera_source_id: str | None = None):
            assert camera_id == "cam1"
            assert camera_source_id is None
            return {"pan": 0.1, "tilt": -0.2, "zoom": 0.3, "move_status": "IDLE", "error": "", "utc_time": "2026-01-01T00:00:00Z"}

        async def absolute_move(
            *,
            camera_id: str,
            pan: float | None = None,
            tilt: float | None = None,
            zoom: float | None = None,
            camera_source_id: str | None = None,
        ):
            assert camera_id == "cam1"
            assert camera_source_id is None
            assert pan == pytest.approx(0.1)
            assert tilt == pytest.approx(-0.2)
            assert zoom == pytest.approx(0.3)
            return {"ok": True}

        async def move(
            *,
            camera_id: str,
            pan: float,
            tilt: float,
            zoom: float,
            timeout_s: float | None = None,
            camera_source_id: str | None = None,
        ):
            assert camera_id == "cam1"
            assert camera_source_id is None
            assert pan == pytest.approx(0.5)
            assert tilt == pytest.approx(-0.5)
            assert zoom == pytest.approx(0.25)
            assert timeout_s == pytest.approx(0.8)
            return {"ok": True}

        async def stop(
            *, camera_id: str, pan_tilt: bool = True, zoom: bool = True, camera_source_id: str | None = None
        ):
            assert camera_id == "cam1"
            assert camera_source_id is None
            assert pan_tilt is True
            assert zoom is False
            return {"ok": True}

        services.register("cameras.ptz.list_presets", list_presets)
        services.register("cameras.ptz.goto_preset", goto_preset)
        services.register("cameras.ptz.get_status", get_status)
        services.register("cameras.ptz.absolute_move", absolute_move)
        services.register("cameras.ptz.continuous_move", move)
        services.register("cameras.ptz.stop", stop)

        presets = client.get("/api/cameras/cameras/cam1/ptz/presets")
        assert presets.status_code == 200, presets.text
        assert presets.json()["presets"][0]["token"] == "home"

        goto = client.post("/api/cameras/cameras/cam1/ptz/goto-preset", json={"preset_token": "home"})
        assert goto.status_code == 200, goto.text
        assert goto.json()["ok"] is True

        status = client.get("/api/cameras/cameras/cam1/ptz/status")
        assert status.status_code == 200, status.text
        assert status.json()["status"]["move_status"] == "IDLE"

        absolute_move_res = client.post(
            "/api/cameras/cameras/cam1/ptz/absolute-move",
            json={"pan": 0.1, "tilt": -0.2, "zoom": 0.3},
        )
        assert absolute_move_res.status_code == 200, absolute_move_res.text
        assert absolute_move_res.json()["ok"] is True

        move_res = client.post(
            "/api/cameras/cameras/cam1/ptz/move",
            json={"pan": 0.5, "tilt": -0.5, "zoom": 0.25, "timeout_s": 0.8},
        )
        assert move_res.status_code == 200, move_res.text
        assert move_res.json()["ok"] is True

        stop_res = client.post("/api/cameras/cameras/cam1/ptz/stop", json={"pan_tilt": True, "zoom": False})
        assert stop_res.status_code == 200, stop_res.text
        assert stop_res.json()["ok"] is True
