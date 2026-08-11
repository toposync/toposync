from __future__ import annotations

from toposync.runtime.config_store import AppConfig, Composition, CompositionElement
from toposync_ext_cameras.view_resolver import resolve_ptz_target_view


def _view(
    view_id: str,
    preset_token: str,
    *,
    left: float = 0.0,
    right: float = 10.0,
    compatible_roles: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": view_id,
        "label": view_id,
        "pose_reference": {"preset_token": preset_token, "preset_name": view_id},
        "stream_scope": {
            "compatible_roles": compatible_roles
            if compatible_roles is not None
            else ["main", "sub"],
            "compatible_source_ids": [],
        },
        "projection_model": {
            "type": "image_quad_on_world",
            "image_region": {
                "top_left": {"x": 0.0, "y": 0.0},
                "bottom_right": {"x": 1.0, "y": 1.0},
            },
            "world_quad": {
                "top_left": {"x": left, "z": 0.0},
                "top_right": {"x": right, "z": 0.0},
                "bottom_right": {"x": right, "z": 10.0},
                "bottom_left": {"x": left, "z": 10.0},
            },
            "refinement": None,
        },
        "projection_quality": {"status": "ready", "estimated": False},
    }


def _config(views: list[dict[str, object]]) -> AppConfig:
    return AppConfig(
        compositions=[
            Composition(
                id="front",
                name="Front",
                elements=[
                    CompositionElement(
                        id="camera-element",
                        type="com.toposync.cameras.camera",
                        props={"camera_id": "cam1", "calibrated_views": views},
                    )
                ],
            )
        ],
        active_composition_id="front",
    )


def _settings() -> dict[str, object]:
    return {
        "schema_version": 4,
        "devices": [
            {
                "id": "cam1",
                "name": "Front",
                "enabled": True,
                "control": {"type": "onvif"},
                "onvif": {"xaddr": "http://camera/onvif/device_service"},
                "sources": [
                    {
                        "id": "main",
                        "name": "Main",
                        "enabled": True,
                        "is_default": True,
                        "kind": "video",
                        "role": "main",
                        "view_id": "main",
                        "origin": {"type": "onvif_profile", "has_ptz": True},
                    },
                    {
                        "id": "zoom",
                        "name": "Zoom",
                        "enabled": True,
                        "kind": "video",
                        "role": "zoom",
                        "view_id": "zoom",
                        "origin": {"type": "onvif_profile", "has_ptz": True},
                    },
                ],
            }
        ],
    }


def test_view_resolver_selects_world_covering_preset_and_honors_allowlist() -> None:
    config = _config([_view("home", "1"), _view("street", "2", left=10.0, right=20.0)])
    selected = resolve_ptz_target_view(
        config=config,
        cameras_settings=_settings(),
        camera_id="cam1",
        source_id="main",
        ptz_device_id="cam1",
        composition_id="front",
        target={
            "world_envelope": {
                "center": {"x": 15.0, "z": 5.0},
                "radius_meters": 1.0,
            }
        },
        eligible_view_ids=["street"],
    )
    assert selected["view_id"] == "street"
    assert selected["preset_token"] == "2"
    assert selected["reason"] == "best_world_coverage"
    assert selected["confidence"] > 0.5
    assert set(selected) == {"view_id", "preset_token", "confidence", "reason"}

    denied = resolve_ptz_target_view(
        config=config,
        cameras_settings=_settings(),
        camera_id="cam1",
        source_id="main",
        ptz_device_id="cam1",
        composition_id="front",
        target={"world": {"x": 15.0, "z": 5.0}},
        eligible_view_ids=["home"],
    )
    assert denied["view_id"] is None
    assert denied["reason"] == "world_target_outside_eligible_views"


def test_view_resolver_fails_closed_for_zoom_scope_and_implicit_bbox_view() -> None:
    config = _config([_view("home", "1"), _view("zoom-safe", "3", compatible_roles=["zoom"])])
    scoped = resolve_ptz_target_view(
        config=config,
        cameras_settings=_settings(),
        camera_id="cam1",
        source_id="zoom",
        ptz_device_id="cam1",
        composition_id="front",
        target={"world": {"x": 5.0, "z": 5.0}},
        eligible_view_ids=["zoom-safe"],
    )
    assert scoped["view_id"] == "zoom-safe"

    implicit_bbox = resolve_ptz_target_view(
        config=config,
        cameras_settings=_settings(),
        camera_id="cam1",
        source_id="zoom",
        ptz_device_id="cam1",
        composition_id="front",
        target={"bbox01": [0.2, 0.2, 0.3, 0.3]},
        eligible_view_ids=["zoom-safe"],
    )
    assert implicit_bbox["view_id"] is None
    assert implicit_bbox["reason"] == "bbox_target_requires_explicit_source_and_view"

    explicit_bbox = resolve_ptz_target_view(
        config=config,
        cameras_settings=_settings(),
        camera_id="cam1",
        source_id="zoom",
        ptz_device_id="cam1",
        composition_id="front",
        target={"bbox01": [0.2, 0.2, 0.3, 0.3]},
        preferred_view_id="zoom-safe",
        eligible_view_ids=["zoom-safe"],
    )
    assert explicit_bbox["view_id"] == "zoom-safe"
    assert explicit_bbox["reason"] == "explicit_view_bbox_target"


def test_view_resolver_rejects_unready_or_estimated_projection() -> None:
    for projection_quality in (
        {"status": "estimated", "estimated": False},
        {"status": "incomplete", "estimated": False},
        {"status": "ready", "estimated": True},
    ):
        view = _view("unsafe", "4")
        view["projection_quality"] = projection_quality
        selected = resolve_ptz_target_view(
            config=_config([view]),
            cameras_settings=_settings(),
            camera_id="cam1",
            source_id="main",
            ptz_device_id="cam1",
            composition_id="front",
            target={"world": {"x": 5.0, "z": 5.0}},
            eligible_view_ids=["unsafe"],
        )
        assert selected["view_id"] is None
        assert selected["reason"] == "no_eligible_calibrated_view"


def test_view_resolver_rejects_degenerate_or_low_quality_mapper() -> None:
    low_coverage = _view("low-coverage", "5")
    projection_model = low_coverage["projection_model"]
    assert isinstance(projection_model, dict)
    projection_model["image_region"] = {
        "top_left": {"x": 0.0, "y": 0.0},
        "bottom_right": {"x": 0.1, "y": 0.1},
    }

    degenerate = _view("degenerate", "6")
    projection_model = degenerate["projection_model"]
    assert isinstance(projection_model, dict)
    projection_model["world_quad"] = {
        "top_left": {"x": 0.0, "z": 0.0},
        "top_right": {"x": 3.0, "z": 0.0},
        "bottom_right": {"x": 2.0, "z": 0.0},
        "bottom_left": {"x": 1.0, "z": 0.0},
    }

    for view in (low_coverage, degenerate):
        selected = resolve_ptz_target_view(
            config=_config([view]),
            cameras_settings=_settings(),
            camera_id="cam1",
            source_id="main",
            ptz_device_id="cam1",
            composition_id="front",
            target={"world": {"x": 5.0, "z": 5.0}},
            eligible_view_ids=[str(view["id"])],
        )
        assert selected["view_id"] is None
        assert selected["reason"] == "no_eligible_calibrated_view"


def test_view_resolver_rejects_disabled_fixed_or_non_ptz_camera() -> None:
    view = _view("street", "7")

    disabled_settings = _settings()
    disabled_devices = disabled_settings["devices"]
    assert isinstance(disabled_devices, list)
    disabled_camera = disabled_devices[0]
    assert isinstance(disabled_camera, dict)
    disabled_camera["enabled"] = False
    disabled = resolve_ptz_target_view(
        config=_config([view]),
        cameras_settings=disabled_settings,
        camera_id="cam1",
        source_id="main",
        ptz_device_id="cam1",
        composition_id="front",
        target={"world": {"x": 5.0, "z": 5.0}},
    )
    assert disabled["reason"] == "camera_disabled"

    fixed_settings = _settings()
    fixed_devices = fixed_settings["devices"]
    assert isinstance(fixed_devices, list)
    fixed_camera = fixed_devices[0]
    assert isinstance(fixed_camera, dict)
    fixed_camera["control"] = {"type": "none"}
    fixed = resolve_ptz_target_view(
        config=_config([view]),
        cameras_settings=fixed_settings,
        camera_id="cam1",
        source_id="main",
        ptz_device_id="cam1",
        composition_id="front",
        target={"world": {"x": 5.0, "z": 5.0}},
    )
    assert fixed["reason"] == "camera_control_not_onvif"

    non_ptz_settings = _settings()
    non_ptz_devices = non_ptz_settings["devices"]
    assert isinstance(non_ptz_devices, list)
    non_ptz_camera = non_ptz_devices[0]
    assert isinstance(non_ptz_camera, dict)
    non_ptz_sources = non_ptz_camera["sources"]
    assert isinstance(non_ptz_sources, list)
    non_ptz_source = non_ptz_sources[0]
    assert isinstance(non_ptz_source, dict)
    non_ptz_source["origin"] = {"type": "onvif_profile", "has_ptz": False}
    non_ptz = resolve_ptz_target_view(
        config=_config([view]),
        cameras_settings=non_ptz_settings,
        camera_id="cam1",
        source_id="main",
        ptz_device_id="cam1",
        composition_id="front",
        target={"world": {"x": 5.0, "z": 5.0}},
    )
    assert non_ptz["reason"] == "camera_source_not_ptz_capable"


def test_view_resolver_uses_canonical_camera_id_as_ptz_identity() -> None:
    selected = resolve_ptz_target_view(
        config=_config([_view("street", "8")]),
        cameras_settings=_settings(),
        camera_id="cam1",
        source_id="main",
        ptz_device_id="free-form-head",
        composition_id="front",
        target={"world": {"x": 5.0, "z": 5.0}},
    )
    assert selected["view_id"] is None
    assert selected["reason"] == "ptz_device_mismatch"
