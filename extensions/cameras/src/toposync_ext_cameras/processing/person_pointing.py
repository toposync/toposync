"""Metric alignment and conservative selection; outputs never authorize actions."""

from __future__ import annotations

import math
import numpy as np

from .metric_camera import MetricCamera, image_ray_camera


def vector(value, size=3):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError("invalid_pointing_coordinates")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise ValueError("invalid_pointing_coordinates")
    result = np.asarray(value, dtype=float)
    if not np.isfinite(result).all():
        raise ValueError("invalid_pointing_coordinates")
    return result


def align_pose(
    camera: MetricCamera, pose: dict, ground: dict, threshold: float, error_limit: float
):
    """Solve camera translation and positive uniform scale, not a rigid-pose PnP.

    For each undistorted ray r: (I-rr') (translation + scale*q) = 0.
    Support midpoints fix scale only conditional on the stationary-floor hypothesis.
    Monocular reprojection cannot prove physical contact or rule out global elevation.
    Rotation comes from the calibrated camera, never from an arbitrary fit.
    """
    metadata = pose.get("metadata")
    relative = metadata.get("relative_landmarks_3d") if isinstance(metadata, dict) else None
    if not isinstance(relative, dict):
        raise ValueError("supported_image_3d_pose_required")
    if (
        pose.get("skeleton_id") != "mediapipe_pose_33"
        or relative.get("reference") != "hip_center_camera_axes"
        or relative.get("axes") != "x_right_y_down_z_away"
        or relative.get("units") != "model_meters"
        or relative.get("provenance") != "image_estimate"
        or pose.get("landmark_reference") != "stream_image"
        or pose.get("landmark_units") != "image_fraction"
    ):
        raise ValueError("supported_image_3d_pose_required")
    positions = relative.get("positions")
    if not isinstance(positions, list) or len(positions) != 33:
        raise ValueError("supported_image_3d_pose_required")
    points, images = {}, {}
    names_by_index = {
        11: "left_shoulder",
        12: "right_shoulder",
        13: "left_elbow",
        14: "right_elbow",
        15: "left_wrist",
        16: "right_wrist",
        23: "left_hip",
        24: "right_hip",
        29: "left_heel",
        30: "right_heel",
        31: "left_foot_index",
        32: "right_foot_index",
    }
    for landmark in pose.get("landmarks", []):
        if not isinstance(landmark, dict):
            continue
        index, score = landmark.get("index"), landmark.get("model_score")
        if (
            type(index) is not int
            or not 0 <= index < 33
            or landmark.get("invalid_reason")
            or landmark.get("visibility") in ("occluded", "outside_image")
            or landmark.get("provenance") != "image_estimate"
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or score < threshold
        ):
            continue
        if index not in names_by_index:
            continue
        if landmark.get("name") != names_by_index[index]:
            raise ValueError("pose_landmark_identity_mismatch")
        try:
            image = vector(landmark.get("position"), 2)
            point = vector(positions[index])
        except ValueError:
            continue
        if np.any(image < 0) or np.any(image > 1):
            continue
        name = landmark.get("name")
        if name in points:
            raise ValueError("duplicate_pose_landmark")
        points[name], images[name] = point, image
    torso = ("left_hip", "right_hip", "left_shoulder", "right_shoulder")
    if not all(name in points for name in torso):
        raise ValueError("torso_image_evidence_required")
    rotation = np.asarray(camera.world_to_camera)
    blocks, values, supports = [], [], []
    for name, point in points.items():
        ray = image_ray_camera(camera.lens, tuple(images[name]))
        perpendicular = np.eye(3) - np.outer(ray, ray)
        blocks.append(np.column_stack((perpendicular, perpendicular @ point)))
        values.append(np.zeros(3))
    for side, foot in (ground.get("feet") or {}).items():
        if not isinstance(foot, dict) or foot.get("provenance") != "image_estimate":
            continue
        if foot.get("contact") != "stationary_support_hypothesis":
            continue
        names = (f"{side}_heel", f"{side}_foot_index")
        if (
            not all(name in points for name in names)
            or foot.get("anchor_type") != "heel_toe_support_midpoint"
            or foot.get("image_evidence_timestamp") != ground.get("timestamp")
        ):
            continue
        support = vector(foot.get("position"))
        radius = float(foot.get("uncertainty_radius_meters", math.inf))
        if not 0 < radius <= 0.30 or not camera.contains_ground(tuple(support)):
            continue
        midpoint = np.mean([points[name] for name in names], axis=0)
        blocks.append(np.column_stack((np.eye(3), midpoint)))
        values.append(rotation @ (support - camera.camera_center))
        supports.append((midpoint, support, radius))
    if not supports:
        raise ValueError("current_stationary_metric_support_required")
    # API returns rank and singular values, which are checked explicitly:
    # https://numpy.org/doc/1.26/reference/generated/numpy.linalg.lstsq.html
    matrix = np.vstack(blocks)
    solution, _, rank, singular = np.linalg.lstsq(matrix, np.hstack(values), rcond=None)
    if rank != 4 or singular[-1] / singular[0] < 1e-5 or not np.isfinite(solution).all():
        raise ValueError("metric_pose_alignment_degenerate")
    translation, scale = solution[:3], float(solution[3])
    if not 0.25 <= scale <= 4:
        raise ValueError("metric_pose_scale_inconsistent")
    world = {
        name: np.asarray(camera.camera_center) + rotation.T @ (translation + scale * point)
        for name, point in points.items()
    }
    errors = []
    for name, point in world.items():
        projected = camera.project(tuple(point))
        if projected is None:
            raise ValueError("aligned_pose_behind_camera")
        errors.append(math.dist(projected, images[name]))
    if max(errors) > error_limit:
        raise ValueError("metric_pose_reprojection_inconsistent")
    for midpoint, support, radius in supports:
        mapped = np.asarray(camera.camera_center) + rotation.T @ (translation + scale * midpoint)
        if np.linalg.norm(mapped - support) > radius:
            raise ValueError("metric_pose_support_inconsistent")
    hip = np.mean([world[name] for name in torso[:2]], axis=0)
    shoulder = np.mean([world[name] for name in torso[2:]], axis=0)
    hypotheses = (ground.get("body") or {}).get("hypotheses") or []
    compatible = [
        h
        for h in hypotheses
        if np.linalg.norm((hip - vector(h.get("position")))[[0, 2]]) <= 0.35
        and 0.40 * h["height_meters"] <= hip[1] <= 0.65 * h["height_meters"]
        and 0.68 * h["height_meters"] <= shoulder[1] <= 0.92 * h["height_meters"]
    ]
    if not compatible or not camera.contains_ground(tuple(hip)):
        raise ValueError("metric_pose_body_inconsistent")
    # Propagate bounded metric-anchor perturbations through the actual fit.
    # This does not calibrate model error; the angular cone remains heuristic.
    inverse = np.linalg.pinv(matrix)
    origin_radius = max(v[2] for v in supports)
    for name in ("left_wrist", "right_wrist"):
        if name not in points:
            continue
        sensitivity = rotation.T @ np.column_stack((np.eye(3), points[name])) @ inverse
        bound = sum(
            np.linalg.norm(sensitivity[:, 3 * (len(points) + i) : 3 * (len(points) + i + 1)], ord=2)
            * support[2]
            for i, support in enumerate(supports)
        )
        origin_radius = max(origin_radius, float(bound))
    if origin_radius > 0.75:
        raise ValueError("metric_pose_origin_ambiguous")
    return world, {
        "method": "image_rays_and_current_metric_support",
        "scale": scale,
        "root_camera_meters": translation.tolist(),
        "maximum_reprojection_error": max(errors),
        "support_count": len(supports),
        "origin_uncertainty_meters": origin_radius,
        "assumptions": [
            "model_camera_axes_correct",
            "uniform_body_scale",
            "upright_planar_support",
            "stationary_floor_contact_unverified",
        ],
        "metric_accuracy_qualified": False,
        "global_elevation_ambiguity_resolved": False,
    }


def pointing_ray(world: dict, side: str):
    candidates = []
    for current in ("left", "right") if side == "auto" else (side,):
        names = [f"{current}_{joint}" for joint in ("shoulder", "elbow", "wrist")]
        if not all(name in world for name in names):
            continue
        shoulder, elbow, wrist = (world[name] for name in names)
        upper, lower = elbow - shoulder, wrist - elbow
        lengths = np.linalg.norm(upper), np.linalg.norm(lower)
        if (
            min(lengths) < 0.12
            or max(lengths) > 0.65
            or np.dot(upper, lower) / np.prod(lengths) < 0.8
        ):
            continue
        candidates.append((current, wrist, lower / lengths[1]))
    if len(candidates) != 1:
        raise ValueError("pointing_arm_ambiguous" if candidates else "extended_arm_required")
    return candidates[0]


def _rotation_y(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def composition_geometry(composition):
    """Read existing composition primitives. Missing dimensions never get defaults."""
    entities, unknown_walls, diagnostics = [], [], []
    for element in composition.elements:
        props = element.props
        try:
            position = vector([element.position.x, element.position.y, element.position.z])
            rotation = vector([element.rotation.x, element.rotation.y, element.rotation.z])
            if element.type == "com.toposync.structural.wall":
                a, b = props["a"], props["b"]
                start, end = vector([a["x"], 0.0, a["z"]]), vector([b["x"], 0.0, b["z"]])
                length = np.linalg.norm(end - start)
                if length < 0.01 or np.any(rotation != 0) or position[1] != 0:
                    raise ValueError("unsupported_wall_transform")
                height = props.get("physical_height_meters")
                if (
                    not isinstance(height, (int, float))
                    or not math.isfinite(height)
                    or height <= 0
                    or props.get("openings")
                ):
                    unknown_walls.append((start, end))
                    raise ValueError("wall_physical_height_or_opening_geometry_required")
                size = vector([length, height, props["width"]])
                center = (start + end) / 2 + [0, height / 2, 0]
                basis = _rotation_y(math.atan2(-(end - start)[2], (end - start)[0]))
                entity = {"kind": "box", "center": center, "basis": basis, "half": size / 2}
            elif element.type == "com.toposync.models.gltf":
                if rotation[0] != 0 or rotation[2] != 0 or props.get("animation_enabled"):
                    raise ValueError("unsupported_volume_transform_or_animation")
                size = vector([props["size"][axis] for axis in "xyz"]) * float(props["scale"])
                center = position + [0, size[1] / 2, 0]
                entity = {
                    "kind": "box",
                    "center": center,
                    "basis": _rotation_y(rotation[1]),
                    "half": size / 2,
                }
            elif element.type == "com.toposync.structural.area":
                if np.any(rotation != 0) or position[1] != 0:
                    raise ValueError("unsupported_ground_area_transform")
                vertices = np.array([vector([p["x"], 0.0, p["z"]]) for p in props["vertices"]])
                if not 3 <= len(vertices) <= 256:
                    raise ValueError("invalid_ground_area")
                center = vertices.mean(axis=0)
                entity = {"kind": "area", "center": center, "vertices": vertices}
            else:
                continue
            radius = (
                np.linalg.norm(size) / 2
                if entity["kind"] == "box"
                else max(np.linalg.norm(p - center) for p in vertices)
            )
            if (
                not math.isfinite(radius)
                or radius <= 0
                or (entity["kind"] == "box" and np.any(size <= 0))
            ):
                raise ValueError("invalid_entity_dimensions")
            entities.append({**entity, "id": element.id, "radius": radius, "type": element.type})
        except (ValueError, TypeError, KeyError, OverflowError):
            diagnostics.append(
                {"entity_id": element.id, "reason": "unsupported_or_incomplete_entity_geometry"}
            )
    return entities, unknown_walls, diagnostics


def _hit(entity, origin, direction):
    if entity["kind"] == "box":
        local = entity["basis"].T @ (origin - entity["center"])
        ray = entity["basis"].T @ direction
        near, far = 0.0, math.inf
        for value, component, half in zip(local, ray, entity["half"]):
            if abs(component) < 1e-9:
                if abs(value) > half:
                    return None
                continue
            bounds = sorted(((-half - value) / component, (half - value) / component))
            near, far = max(near, bounds[0]), min(far, bounds[1])
        return near if 0 < near <= far else None
    if abs(direction[1]) < 1e-9:
        return None
    distance = -origin[1] / direction[1]
    if distance <= 0:
        return None
    point, vertices = origin + distance * direction, entity["vertices"]
    inside = False
    for a, b in zip(vertices, np.roll(vertices, -1, axis=0)):
        if (a[2] > point[2]) != (b[2] > point[2]) and point[0] < (b[0] - a[0]) * (
            point[2] - a[2]
        ) / (b[2] - a[2]) + a[0]:
            inside = not inside
    return distance if inside else None


def select_target(
    composition, origin, direction, cone_degrees, origin_radius, maximum_distance, camera=None
):
    entities, unknown_walls, diagnostics = composition_geometry(composition)
    candidates = []
    for entity in entities:
        delta = entity["center"] - origin
        distance = np.linalg.norm(delta)
        radius = entity["radius"] + origin_radius
        if distance - radius > maximum_distance or np.dot(delta, direction) + radius <= 0:
            continue
        angle = math.acos(float(np.clip(np.dot(delta, direction) / max(distance, 1e-9), -1, 1)))
        if angle > math.radians(cone_degrees) + math.asin(min(1.0, radius / max(distance, 1e-9))):
            continue
        hit = _hit(entity, origin, direction)
        if hit is not None and hit > maximum_distance:
            hit = None
        if camera is not None and camera.project(tuple(entity["center"])) is None:
            diagnostics.append({"entity_id": entity["id"], "reason": "target_behind_camera"})
            continue
        candidates.append(
            {
                "entity_id": entity["id"],
                "entity_type": entity["type"],
                "central_ray_distance_meters": float(hit) if hit is not None else None,
                "center_angle_degrees": math.degrees(angle),
                "occlusion": "unknown" if unknown_walls else "not_tested",
                "score_kind": "geometric_candidate",
            }
        )
    for candidate in candidates:
        distance = candidate["central_ray_distance_meters"]
        if distance is None:
            continue
        blocked = any(
            other["entity_id"] != candidate["entity_id"]
            and other["central_ray_distance_meters"] is not None
            and other["central_ray_distance_meters"] < distance - 0.01
            for other in candidates
        )
        if blocked:
            candidate["occlusion"] = "blocked_on_central_ray"
        elif not unknown_walls:
            candidate["occlusion"] = "clear_in_supported_map_geometry"
    # Bounding spheres deliberately over-cover the cone. Never force nearest.
    # A central-ray blocker does not prove the entire uncertain cone is blocked.
    # Keep deeper candidates, instead of implicitly selecting the nearest one.
    eligible = candidates
    selected = None
    if unknown_walls or diagnostics:
        status, reason = "unavailable", "map_geometry_incomplete"
    elif len(eligible) > 1:
        status, reason = "ambiguous", "multiple_entities_in_uncertainty_cone"
    elif len(eligible) == 1 and eligible[0]["central_ray_distance_meters"] is not None:
        selected = eligible[0]["entity_id"]
        status, reason = "candidate", "intent_and_accuracy_not_confirmed"
    else:
        status, reason = "none", "no_supported_target"
    return {
        "status": status,
        "reason": reason,
        "selected_entity_id": selected,
        "candidates": candidates,
        "map_diagnostics": diagnostics,
    }
