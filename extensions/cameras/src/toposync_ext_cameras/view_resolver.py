from __future__ import annotations

import math
from typing import Any

from .pipelines.postprocess import _parse_calibrated_views_as_control_point_sets  # noqa: PLC2701
from .processing.mapping import ControlPointMapper
from .settings import (
    camera_source_has_ptz,
    get_camera_device,
    get_camera_source,
    normalize_cameras_settings,
)


_MINIMUM_INLIER_RATIO = 0.75
_MINIMUM_IMAGE_HULL_AREA_RATIO = 0.05
_MAXIMUM_MEDIAN_REPROJECTION_ERROR_UV = 0.025
_MAXIMUM_P95_REPROJECTION_ERROR_UV = 0.05


def resolve_ptz_target_view(
    *,
    config: Any,
    cameras_settings: Any,
    camera_id: str,
    source_id: str,
    ptz_device_id: str,
    composition_id: str,
    target: dict[str, Any],
    preferred_view_id: str | None = None,
    eligible_view_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    cid = str(camera_id or "").strip()
    composition_key = str(composition_id or "").strip()
    requested_source_id = str(source_id or "").strip()
    if not cid:
        return _failure("camera_id_required")
    if not composition_key:
        return _failure("composition_id_required")
    if not isinstance(target, dict):
        return _failure("target_required")

    extension_settings = normalize_cameras_settings(cameras_settings)
    camera = get_camera_device(extension_settings, camera_id=cid)
    if not isinstance(camera, dict):
        return _failure("camera_not_found")
    if camera.get("enabled") is not True:
        return _failure("camera_disabled")
    control = camera.get("control") if isinstance(camera.get("control"), dict) else {}
    if str(control.get("type") or "").strip().lower() != "onvif":
        return _failure("camera_control_not_onvif")
    requested_ptz_device_id = str(ptz_device_id or "").strip()
    if requested_ptz_device_id and requested_ptz_device_id != cid:
        return _failure("ptz_device_mismatch")

    source = get_camera_source(
        camera,
        source_id=requested_source_id,
        kind="video",
        enabled_only=True,
    )
    if not isinstance(source, dict):
        return _failure("camera_source_not_found")
    if source.get("enabled") is not True or str(source.get("kind") or "").lower() != "video":
        return _failure("camera_source_not_eligible")
    if not camera_source_has_ptz(source):
        return _failure("camera_source_not_ptz_capable")
    resolved_source_id = str(source.get("id") or "").strip()
    source_role = str(source.get("role") or "custom").strip().lower() or "custom"

    compositions = list(getattr(config, "compositions", []) or [])
    composition = next(
        (
            item
            for item in compositions
            if str(getattr(item, "id", "") or "").strip() == composition_key
        ),
        None,
    )
    if composition is None:
        return _failure("composition_not_found")

    eligible = None
    if eligible_view_ids is not None:
        eligible = {
            str(item or "").strip() for item in eligible_view_ids if str(item or "").strip()
        }
        if not eligible:
            return _failure("eligible_view_allowlist_empty")

    candidates: list[dict[str, Any]] = []
    for element in list(getattr(composition, "elements", []) or []):
        if str(getattr(element, "type", "") or "").strip() != "com.toposync.cameras.camera":
            continue
        props = getattr(element, "props", None)
        props = props if isinstance(props, dict) else {}
        if str(props.get("camera_id") or "").strip() != cid:
            continue
        raw_views = props.get("calibrated_views")
        if not isinstance(raw_views, list):
            continue
        for raw_view in raw_views:
            view = raw_view if isinstance(raw_view, dict) else {}
            view_id = str(view.get("id") or "").strip()
            if not view_id or (eligible is not None and view_id not in eligible):
                continue
            pose = (
                view.get("pose_reference") if isinstance(view.get("pose_reference"), dict) else {}
            )
            preset_token = str(pose.get("preset_token") or "").strip()
            if not preset_token:
                continue
            if not _source_matches_view(
                view,
                source_id=resolved_source_id,
                source_role=source_role,
            ):
                continue
            if not _projection_is_ready(view):
                continue
            parsed = _parse_calibrated_views_as_control_point_sets([view])
            if len(parsed) != 1:
                continue
            mapper = _safe_mapper(parsed[0])
            if mapper is None:
                continue
            candidates.append(
                {
                    "view_id": view_id,
                    "preset_token": preset_token,
                    "element_id": str(getattr(element, "id", "") or "").strip(),
                    "mapper": mapper,
                }
            )

    if not candidates:
        return _failure("no_eligible_calibrated_view")
    candidate_view_ids = [str(item["view_id"]) for item in candidates]
    if len(set(candidate_view_ids)) != len(candidate_view_ids):
        return _failure("duplicate_eligible_view_id")

    preferred = str(preferred_view_id or "").strip()
    if target.get("home") is True:
        if preferred:
            matching = [item for item in candidates if item["view_id"] == preferred]
            if len(matching) != 1:
                return _failure("preferred_home_view_not_unique_or_unavailable")
            selected = matching[0]
        else:
            selected = candidates[0]
        return _success(
            selected,
            confidence=1.0,
            reason="first_eligible_calibrated_view",
            eligible_view_ids=candidate_view_ids,
        )
    bbox = _bbox01(target)
    if bbox is not None:
        if not requested_source_id or not preferred:
            return _failure("bbox_target_requires_explicit_source_and_view")
        matching = [item for item in candidates if item["view_id"] == preferred]
        if len(matching) != 1:
            return _failure("preferred_view_not_unique_or_unavailable")
        return _success(
            matching[0],
            confidence=0.5,
            reason="explicit_view_bbox_target",
        )

    world = _world_target(target)
    if world is None:
        return _failure("world_target_required")
    world_x, world_z, radius = world

    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        score = _coverage_score(
            candidate["mapper"],
            world_x=world_x,
            world_z=world_z,
            radius=radius,
        )
        if score is None:
            continue
        if preferred and candidate["view_id"] == preferred:
            return _success(
                candidate,
                confidence=score,
                reason="preferred_view_covers_world_target",
            )
        scored.append((score, candidate))

    if not scored:
        return _failure("world_target_outside_eligible_views")
    scored.sort(key=lambda item: (-item[0], str(item[1]["view_id"])))
    score, selected = scored[0]
    return _success(
        selected,
        confidence=score,
        reason="best_world_coverage",
    )


def _source_matches_view(view: dict[str, Any], *, source_id: str, source_role: str) -> bool:
    scope = view.get("stream_scope") if isinstance(view.get("stream_scope"), dict) else {}
    compatible_source_ids = {
        str(item or "").strip()
        for item in (scope.get("compatible_source_ids") or [])
        if str(item or "").strip()
    }
    compatible_roles = {
        str(item or "").strip().lower()
        for item in (scope.get("compatible_roles") or [])
        if str(item or "").strip()
    }
    if compatible_source_ids and source_id not in compatible_source_ids:
        return False
    if compatible_roles and source_role not in compatible_roles:
        return False
    if not compatible_source_ids and not compatible_roles and source_role not in {"main", "sub"}:
        return False
    return True


def _projection_is_ready(view: dict[str, Any]) -> bool:
    quality = view.get("projection_quality")
    if not isinstance(quality, dict):
        return False
    status = str(quality.get("status") or "").strip().lower()
    if status != "ready":
        return False
    return not bool(quality.get("estimated", False))


def _safe_mapper(control_point_set: Any) -> ControlPointMapper | None:
    try:
        mapper = ControlPointMapper(
            list(control_point_set.control_points),
            refinement_points=control_point_set.refinement_points,
            boundary_refinement_points=control_point_set.boundary_refinement_points,
        )
    except Exception:
        return None

    quality = mapper.quality
    numeric_values = (
        float(quality.inlier_ratio),
        float(quality.convex_hull_area_ratio_uv),
    )
    if not all(math.isfinite(value) for value in numeric_values):
        return None
    if quality.number_of_points < 4 or quality.number_of_inliers < 4:
        return None
    if quality.inlier_ratio < _MINIMUM_INLIER_RATIO:
        return None
    if quality.convex_hull_area_ratio_uv < _MINIMUM_IMAGE_HULL_AREA_RATIO:
        return None
    if quality.is_near_collinear or quality.is_numerically_unstable:
        return None

    median_error = quality.median_reprojection_error_uv
    p95_error = quality.p95_reprojection_error_uv
    if median_error is None or p95_error is None:
        return None
    if not math.isfinite(float(median_error)) or not math.isfinite(float(p95_error)):
        return None
    if float(median_error) > _MAXIMUM_MEDIAN_REPROJECTION_ERROR_UV:
        return None
    if float(p95_error) > _MAXIMUM_P95_REPROJECTION_ERROR_UV:
        return None
    return mapper


def _world_target(target: dict[str, Any]) -> tuple[float, float, float] | None:
    envelope = target.get("world_envelope")
    envelope = envelope if isinstance(envelope, dict) else target.get("envelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    center = envelope.get("center") if isinstance(envelope.get("center"), dict) else None
    anchor = center
    if anchor is None:
        for key in ("world_anchor", "world", "anchor"):
            candidate = target.get(key)
            if isinstance(candidate, dict):
                anchor = candidate
                break
    if anchor is None and "x" in target and "z" in target:
        anchor = target
    if not isinstance(anchor, dict):
        return None
    try:
        x = float(anchor.get("x"))
        z = float(anchor.get("z"))
        radius = float(
            envelope.get("radius_meters")
            or envelope.get("radius")
            or target.get("radius_meters")
            or target.get("radius")
            or 0.0
        )
    except Exception:
        return None
    if not all(math.isfinite(value) for value in (x, z, radius)) or radius < 0.0:
        return None
    return x, z, radius


def _bbox01(target: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw = target.get("bbox01")
    if isinstance(raw, dict):
        values = (raw.get("x1"), raw.get("y1"), raw.get("x2"), raw.get("y2"))
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        values = tuple(raw)
    else:
        return None
    try:
        x1, y1, x2, y2 = (float(value) for value in values)
    except Exception:
        return None
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        return None
    if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
        return None
    return x1, y1, x2, y2


def _coverage_score(
    mapper: ControlPointMapper,
    *,
    world_x: float,
    world_z: float,
    radius: float,
) -> float | None:
    samples = [(world_x, world_z)]
    if radius > 0.0:
        samples.extend(
            [
                (world_x - radius, world_z),
                (world_x + radius, world_z),
                (world_x, world_z - radius),
                (world_x, world_z + radius),
            ]
        )
    mapped: list[tuple[float, float]] = []
    for x, z in samples:
        point = mapper.map_world_to_image(x, z)
        if point is None:
            return None
        u, v = float(point[0]), float(point[1])
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            return None
        mapped.append((u, v))
    center_u, center_v = mapped[0]
    distance = math.sqrt((center_u - 0.5) ** 2 + (center_v - 0.5) ** 2)
    confidence = max(0.0, min(1.0, 1.0 - (distance / math.sqrt(0.5))))
    if radius > 0.0:
        edge_margin = min(min(u, 1.0 - u, v, 1.0 - v) for u, v in mapped)
        confidence *= max(0.25, min(1.0, edge_margin / 0.2))
    return round(confidence, 6)


def _success(
    candidate: dict[str, Any],
    *,
    confidence: float,
    reason: str,
    eligible_view_ids: list[str] | None = None,
) -> dict[str, Any]:
    result = {
        "view_id": candidate["view_id"],
        "preset_token": candidate["preset_token"],
        "confidence": float(confidence),
        "reason": reason,
    }
    if eligible_view_ids is not None:
        result["eligible_view_ids"] = list(eligible_view_ids)
    return result


def _failure(reason: str) -> dict[str, Any]:
    return {
        "view_id": None,
        "preset_token": None,
        "confidence": 0.0,
        "reason": reason,
    }
