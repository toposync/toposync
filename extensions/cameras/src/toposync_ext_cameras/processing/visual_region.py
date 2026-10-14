"""Canonical mask projection and conservative, frame-bound region decisions."""

from __future__ import annotations

import cv2
import numpy as np

from toposync.runtime.pipelines.image_geometry import geometry_matrix, source_pixels

from .panorama_mapping import _lens_arrays, _rotation_basis


def observed_pixels(pixels, geometry):
    valid = np.ones(len(pixels), dtype=bool)
    domains = [{"size": geometry["image_size"], "matrix": np.eye(3)},
               {"size": geometry["source_size"], "matrix": geometry["to_source"]},
               *geometry.get("valid_domains", [])]
    for domain in domains:
        points = source_pixels(pixels, np.asarray(domain["matrix"]))
        width, height = domain["size"]
        valid &= ((points[:, 0] >= 0) & (points[:, 0] <= width - 1)
                  & (points[:, 1] >= 0) & (points[:, 1] <= height - 1))
    return valid


def rasterize_region(strokes, width, height, *, wrap=False):
    if not (2 <= width <= 8192 and 2 <= height <= 4096):
        raise ValueError("visual_region_dimensions_invalid")
    mask = np.zeros((height, width), dtype=np.uint8)
    for stroke in strokes:
        points = np.asarray(stroke.points01, dtype=float) * [width - 1, height - 1]
        thickness = max(1, round(stroke.diameter01 * min(width, height)))
        color = 255 if stroke.op == "paint" else 0
        previous = points[0]
        segments = [(previous, previous)]
        for point in points[1:]:
            adjusted = point.copy()
            if wrap:
                adjusted[0] = previous[0] + (point[0] - previous[0] + width / 2) % width - width / 2
            segments.append((previous, adjusted))
            previous = adjusted
        for first, second in segments:
            # Repeat only the local unwrapped segment, not a line across the seam.
            offsets = [0] if not wrap else range(
                int(np.floor(-max(first[0], second[0]) / width)),
                int(np.ceil((width - min(first[0], second[0])) / width)) + 1,
            )
            for offset in offsets:
                a, b = np.rint(first + [offset * width, 0]).astype(int), np.rint(second + [offset * width, 0]).astype(int)
                cv2.line(mask, tuple(a), tuple(b), color, thickness, cv2.LINE_8)
                cv2.circle(mask, tuple(a), max(1, thickness // 2), color, -1)
                cv2.circle(mask, tuple(b), max(1, thickness // 2), color, -1)
    if not mask.any():
        raise ValueError("visual_region_empty")
    return mask


def frame_to_reference(pixels, located, geometry, reference_size):
    original = source_pixels(pixels, np.asarray(geometry["to_source"], dtype=float))
    if "homography" in located:
        return (source_pixels(original, np.asarray(located["homography"]))
                * (np.asarray(reference_size) - 1) / (np.asarray(located["reference_size"]) - 1))
    matrix, distortion, _, _ = _lens_arrays(located["lens"])
    ideal = cv2.undistortPoints(original.reshape(-1, 1, 2), matrix, distortion).reshape(-1, 2)
    rays = np.column_stack((ideal, np.ones(len(ideal)))) @ _rotation_basis(located["rotation_matrix"]).T
    u = (np.arctan2(rays[:, 1], rays[:, 0]) / (2 * np.pi) + .5) % 1
    v = .5 - np.arctan2(rays[:, 2], np.hypot(rays[:, 0], rays[:, 1])) / np.pi
    return np.column_stack((u, v)) * (np.asarray(reference_size) - 1)


def reference_to_frame(pixels, located, geometry, reference_size):
    if "homography" in located:
        reference = pixels * (np.asarray(located["reference_size"]) - 1) / (np.asarray(reference_size) - 1)
        original = source_pixels(reference, np.linalg.inv(located["homography"]))
        valid = np.ones(len(pixels), dtype=bool)
    else:
        uv = pixels / (np.asarray(reference_size) - 1)
        pan, tilt = (uv[:, 0] * 2 - 1) * np.pi, (.5 - uv[:, 1]) * np.pi
        rays = np.column_stack((np.cos(tilt) * np.cos(pan), np.cos(tilt) * np.sin(pan), np.sin(tilt)))
        local = rays @ _rotation_basis(located["rotation_matrix"])
        matrix, distortion, _, _ = _lens_arrays(located["lens"])
        original = cv2.projectPoints(local, np.zeros(3), np.zeros(3), matrix, distortion)[0].reshape(-1, 2)
        valid = local[:, 2] > 1e-6
    current = source_pixels(original, np.linalg.inv(geometry["to_source"]))
    width, height = geometry["image_size"]
    source_width, source_height = geometry["source_size"]
    valid &= ((current[:, 0] >= 0) & (current[:, 0] <= width - 1)
              & (current[:, 1] >= 0) & (current[:, 1] <= height - 1)
              & (original[:, 0] >= 0) & (original[:, 0] <= source_width - 1)
              & (original[:, 1] >= 0) & (original[:, 1] <= source_height - 1))
    return current, valid & observed_pixels(current, geometry)


def project_region(mask, coverage, located, geometry):
    width, height = geometry["image_size"]
    geometry_matrix(geometry, width, height)
    reference_size = [mask.shape[1], mask.shape[0]]
    if not isinstance(coverage, np.ndarray) or coverage.shape != mask.shape or np.any((mask > 0) & (coverage == 0)):
        raise ValueError("visual_region_outside_reference_coverage")
    projected = np.zeros((height, width), dtype=np.uint8)
    valid = np.zeros_like(projected)
    for start in range(0, height, 128):
        ys, xs = np.mgrid[start:min(height, start + 128), :width]
        pixels = np.column_stack((xs.ravel(), ys.ravel())).astype(float)
        mapped = frame_to_reference(pixels, located, geometry, reference_size)
        optical = observed_pixels(pixels, geometry).reshape(xs.shape)
        mx, my = mapped[:, 0].reshape(xs.shape).astype(np.float32), mapped[:, 1].reshape(xs.shape).astype(np.float32)
        projected[start:start + len(xs)] = cv2.remap(mask, mx, my, cv2.INTER_NEAREST)
        valid[start:start + len(xs)] = cv2.remap(coverage, mx, my, cv2.INTER_NEAREST) * optical
    projected[valid == 0] = 0
    visible, total = 0, int(np.count_nonzero(mask))
    for start in range(0, mask.shape[0], 128):
        y, x = np.nonzero(mask[start:start + 128])
        if len(x):
            _, observed = reference_to_frame(np.column_stack((x, y + start)), located, geometry, reference_size)
            visible += int(observed.sum())
    state = "outside" if visible == 0 else "full" if visible == total else "partial"
    return projected, valid, {"state": state, "visible_fraction": visible / total}


def evaluate_detection(config, detection, mask, valid, located, geometry, evidence):
    unknown = {"classification": "indeterminate", "reason": "visual_region_detection_geometry_missing"}
    if not isinstance(detection, dict):
        return unknown
    metadata = detection.get("metadata") or {}
    if not isinstance(metadata, dict):
        return unknown
    source_shape = metadata.get("source_shape")
    bbox = detection.get("bbox01")
    try:
        box = np.asarray(bbox, dtype=float)
        if box.shape != (4,) or not np.isfinite(box).all() or np.any(box < 0) or np.any(box > 1):
            return unknown
        left, top, right, bottom = box
        if left >= right or top >= bottom:
            return unknown
        if source_shape:
            if source_shape.get("capture_evidence") != evidence:
                return unknown
            polygon = np.asarray(source_shape["polygon01"], dtype=float)
            base = np.asarray(source_shape["base01"], dtype=float)
            center = np.asarray(source_shape["center01"], dtype=float)
            if polygon.shape != (4, 2) or base.shape != (2,) or center.shape != (2,):
                return unknown
            coordinates = np.concatenate((polygon.ravel(), base, center))
            if not np.isfinite(coordinates).all() or np.any(coordinates < 0) or np.any(coordinates > 1):
                return unknown
        else:
            # Unknown transforms must not turn their bounding envelope into a precise contour.
            if geometry["to_source"] != [[1, 0, 0], [0, 1, 0], [0, 0, 1]]:
                return unknown
            polygon = np.array([[left, top], [right, top], [right, bottom], [left, bottom]])
            base, center = [(left+right)/2, bottom], [(left+right)/2, (top+bottom)/2]
        scale = np.asarray(geometry["source_size"]) - 1
        polygon = source_pixels(polygon * scale, np.linalg.inv(geometry["to_source"]))
        anchor = source_pixels(np.asarray([base if config.criterion == "base" else center]) * scale,
                               np.linalg.inv(geometry["to_source"]))[0]
        support = np.asarray(located.get("support_pixels", []), dtype=float)
        if len(support) < 3:
            return {**unknown, "reason": "visual_region_local_support_missing"}
        support = source_pixels(support, np.linalg.inv(geometry["to_source"]))
        hull = cv2.convexHull(support.astype(np.float32))
        tested = [anchor] if config.criterion in {"base", "center"} else polygon
        if any(cv2.pointPolygonTest(hull, tuple(map(float, point)), False) < 0 for point in tested):
            return {**unknown, "reason": "visual_region_local_support_missing"}
        original_tested = source_pixels(tested, np.asarray(geometry["to_source"]))
        original_support = np.asarray(located["support_pixels"])
        nearest = np.linalg.norm(original_tested[:, None] - original_support[None, :], axis=2).min(axis=1)
        if np.any(nearest > .2 * min(geometry["source_size"])):
            return {**unknown, "reason": "visual_region_local_support_missing"}
        width, height = geometry["image_size"]
        source_error = located["validation_p95_pixels"] * geometry["source_size"][0] / located["analysis_width"]
        source_tested = source_pixels(tested, np.asarray(geometry["to_source"]))
        displaced = np.concatenate([source_tested + offset for offset in
                                    ([source_error, 0], [-source_error, 0], [0, source_error], [0, -source_error])])
        mapped = source_pixels(displaced, np.linalg.inv(geometry["to_source"]))
        error = max(1, int(np.ceil(np.linalg.norm(mapped - np.tile(tested, (4, 1)), axis=1).max())))
        # Conservative boundary band; empirical residuals are not a physical accuracy certificate.
        distance_inside = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 5)
        distance_outside = cv2.distanceTransform((mask == 0).astype(np.uint8), cv2.DIST_L2, 5)
        if config.criterion in {"base", "center"}:
            x, y = np.rint(anchor).astype(int)
            if not (0 <= x < width and 0 <= y < height) or valid[y, x] == 0:
                return {**unknown, "reason": "visual_region_detection_unobserved"}
            inside = mask[y, x] > 0
            if (distance_inside if inside else distance_outside)[y, x] <= error:
                return {**unknown, "reason": "visual_region_boundary_uncertain"}
            return {"classification": "inside" if inside else "outside", "reason": "confirmed"}
        if np.any(polygon < 0) or np.any(polygon[:, 0] >= width) or np.any(polygon[:, 1] >= height):
            return {**unknown, "reason": "visual_region_detection_truncated"}
        object_mask = np.zeros_like(mask)
        cv2.fillPoly(object_mask, [np.rint(polygon).astype(np.int32)], 255)
        selected = object_mask > 0
        total = int(selected.sum())
        if not total or np.any(valid[selected] == 0):
            return {**unknown, "reason": "visual_region_detection_unobserved"}
        lower = float(np.count_nonzero(selected & (distance_inside > error)) / total)
        upper = float(np.count_nonzero(selected & (distance_outside <= error)) / total)
        threshold = 1 if config.criterion == "whole" else config.minimum_overlap
        result = "inside" if lower >= threshold else "outside" if upper < threshold else "indeterminate"
        return {"classification": result, "reason": "visual_region_boundary_uncertain" if result == "indeterminate" else "confirmed",
                "overlap_bounds": [lower, upper]}
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, cv2.error, np.linalg.LinAlgError):
        return unknown
