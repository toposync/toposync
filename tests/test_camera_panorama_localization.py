from __future__ import annotations

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.processing.panorama_localization import (
    PanoramaLocalizer,
    fit_frame_rotation,
    frame_identity,
    correspondence_groups,
)


def _correspondences():
    lens = {
        "width": 960,
        "height": 540,
        "fx": 640,
        "fy": 640,
        "cx": 479.5,
        "cy": 269.5,
        "distortion": [-0.15, 0.03, 0, 0, 0],
    }
    matrix = np.array([[640.0, 0, 479.5], [0, 640, 269.5], [0, 0, 1]])
    rays = np.column_stack(
        (np.random.default_rng(775).uniform([-0.6, -0.33], [0.6, 0.33], (500, 2)), np.ones(500))
    )
    rotation, _ = cv2.Rodrigues(np.array([0.018, -0.055, 0.012]))
    current, _ = cv2.projectPoints(
        rays, np.zeros(3), np.zeros(3), matrix, np.array(lens["distortion"])
    )
    reference, _ = cv2.projectPoints(
        rays @ rotation.T, np.zeros(3), np.zeros(3), matrix, np.array(lens["distortion"])
    )
    return lens, current.reshape(-1, 2), reference.reshape(-1, 2), rotation


def test_rotation_has_independent_pixel_oracle_and_held_out_validation():
    lens, current, reference, expected = _correspondences()
    result = fit_frame_rotation(current, reference, lens, np.eye(3))
    assert result is not None
    assert np.allclose(result["rotation_matrix"], expected, atol=1e-6)
    assert result["validation_p95_pixels"] < 1e-5
    assert result["validation_matches"] >= 80


def test_support_and_partition_are_independent_of_order_and_duplicate_descriptors():
    lens, current, reference, _ = _correspondences()
    expected = fit_frame_rotation(current, reference, lens, np.eye(3))
    for indices in (np.arange(500)[::-1], np.random.default_rng(49).permutation(500),
                    np.tile(np.arange(500), 3)):
        result = fit_frame_rotation(current[indices], reference[indices], lens, np.eye(3))
        assert result == expected
    indices, partition = correspondence_groups(current, reference, 960)
    extended, extended_partition = correspondence_groups(
        np.vstack((current, [1., 1.])), np.vstack((reference, [1., 1.])), 960
    )
    original = {tuple(reference[index]): bool(value) for index, value in zip(indices, partition)}
    assert all(original[tuple(reference[index])] == value
               for index, value in zip(extended, extended_partition) if index < len(reference))


def test_duplicate_descriptors_cannot_manufacture_support():
    lens, current, reference, _ = _correspondences()
    assert fit_frame_rotation(np.tile(current[:30], (20, 1)),
                              np.tile(reference[:30], (20, 1)), lens, np.eye(3)) is None


def test_screen_overlay_requires_coherent_nearby_scene_motion():
    from toposync_ext_cameras.processing.panorama_correspondences import stationary_overlay_points

    x, y = np.meshgrid(np.linspace(0, 960, 24), np.linspace(0, 540, 14))
    points = np.column_stack((x.ravel(), y.ravel()))
    assert not stationary_overlay_points(points, points).any()
    assert not stationary_overlay_points(points, points + [3, 2]).any()
    moved = points + [110, -40]
    moved[:4] = points[:4]
    excluded = stationary_overlay_points(points, moved)
    assert excluded[:4].all() and not excluded[4:].any()
    # Pure roll has a genuinely stationary point at its axis.
    points = np.vstack((points, [480, 270]))
    angle = .2
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    moved = (points - [480, 270]) @ rotation.T + [480, 270]
    assert not stationary_overlay_points(points, moved)[-1]


@pytest.mark.parametrize("failure", ["holdout", "local", "zoom"])
def test_rotation_refuses_bad_validation_local_support_and_changed_optics(failure):
    lens, current, reference, _ = _correspondences()
    if failure == "holdout":
        reference[::5] = reference[np.random.default_rng(433).permutation(500)[:100]]
    elif failure == "local":
        current = current * 0.1 + [420, 230]
        reference = reference * 0.1 + [420, 230]
    else:
        current = (current - [479.5, 269.5]) * 1.12 + [479.5, 269.5]
    assert fit_frame_rotation(current, reference, lens, np.eye(3)) is None


def test_presentation_rotation_is_applied_once():
    lens, current, reference, rotation = _correspondences()
    presentation, _ = cv2.Rodrigues(np.array([0.2, -0.3, 0.1]))
    result = fit_frame_rotation(current, reference, lens, presentation)
    assert result is not None
    assert np.allclose(result["rotation_matrix"], presentation @ rotation, atol=1e-6)


def test_localizer_binds_cache_to_frame_bytes_and_never_reuses_last_pose(tmp_path):
    image = np.random.default_rng(500).integers(0, 256, (540, 960), np.uint8)
    image = cv2.GaussianBlur(image, (5, 5), 1)
    path = tmp_path / "reference.png"
    assert cv2.imwrite(str(path), image)
    lens, _, _, _ = _correspondences()
    localizer = PanoramaLocalizer(
        {"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    evidence = {"capture_instance": "first", "generation": 1, "sequence": 1}
    located = localizer.locate(image, evidence)
    assert located["status"] == "localized"
    assert located["capture_evidence"] == evidence
    assert localizer.locate(np.zeros_like(image), evidence)["status"] == "unlocalized"
    for sequence in range(2, 9):
        localizer.locate(np.zeros_like(image), {**evidence, "sequence": sequence})
    assert len(localizer.results) == 4
    assert localizer.locate(image, {})["reason"] == "panorama_frame_identity_missing"


def test_localizer_preserves_geometric_support_under_reduced_contrast(tmp_path):
    image = cv2.GaussianBlur(
        np.random.default_rng(500).integers(0, 256, (540, 960), np.uint8), (5, 5), 1
    )
    path = tmp_path / "reference.png"
    assert cv2.imwrite(str(path), image)
    lens, _, _, _ = _correspondences()
    localizer = PanoramaLocalizer(
        {"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    # Change only the photometry: the camera and optical geometry stay fixed.
    dimmed = np.rint(image.astype(float) * 0.12 + 20).astype(np.uint8)
    result = localizer.locate(dimmed, {"capture_instance": "contrast", "generation": 0, "sequence": 1})
    assert result["status"] == "localized"
    assert np.allclose(result["rotation_matrix"], np.eye(3), atol=1e-3)
    assert result["validation_matches"] >= 12
    assert result["validation_p95_pixels"] < 1


@pytest.mark.parametrize("decision", [
    {"status": "localized", "rotation_matrix": np.eye(3).tolist()},
    {"status": "unlocalized", "reason": "panorama_visual_localization_ambiguous"},
])
def test_photometric_retry_never_overrides_a_native_decision(monkeypatch, decision):
    from collections import OrderedDict

    localizer = object.__new__(PanoramaLocalizer)
    localizer.lens = _correspondences()[0]
    localizer.results = OrderedDict()

    def locate(image, matrix, diagnostics, *, normalize=False):
        assert not normalize, "A qualified or ambiguous native result must stand"
        return decision

    monkeypatch.setattr(localizer, "_locate", locate)
    result = localizer._locate_frame(
        np.zeros((540, 960), np.uint8),
        {"capture_instance": "native", "generation": 0, "sequence": 1}, None,
    )
    assert all(result[key] == value for key, value in decision.items())


def test_frame_identity_requires_capture_instance_and_integer_sequence():
    assert frame_identity({"capture_instance": "one", "generation": 0, "sequence": 1}) == (
        "one",
        0,
        1,
    )
    for invalid in (
        {},
        {"generation": 0, "sequence": 1},
        {"capture_instance": "one", "generation": 0, "sequence": True},
    ):
        assert frame_identity(invalid) is None


def test_crop_resize_provenance_localizes_optical_pixels_and_rejects_changed_binding(tmp_path):
    from types import SimpleNamespace
    from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry

    image = cv2.GaussianBlur(
        np.random.default_rng(716).integers(0, 256, (540, 960), np.uint8), (5, 5), 1
    )
    path = tmp_path / "optical.png"
    cv2.imwrite(str(path), image)
    lens, _, _, _ = _correspondences()
    localizer = PanoramaLocalizer(
        {"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    evidence = {"capture_instance": "one", "generation": 1, "sequence": 3}
    artifact = SimpleNamespace(
        data=image, metadata={"image_geometry": image_geometry(960, 540, evidence)}
    )
    cropped = image[30:510, 40:920]
    cropped_geometry = transformed_geometry(
        artifact, [[1, 0, -40], [0, 1, -30], [0, 0, 1]], 880, 480
    )
    artifact = SimpleNamespace(data=cropped, metadata=cropped_geometry)
    resized = cv2.resize(cropped, (660, 360), interpolation=cv2.INTER_AREA)
    geometry = transformed_geometry(
        artifact, [[0.75, 0, -0.125], [0, 0.75, -0.125], [0, 0, 1]], 660, 360
    )["image_geometry"]
    located = localizer.locate(resized, evidence, geometry)
    assert located["status"] == "localized"
    assert np.allclose(located["rotation_matrix"], np.eye(3), atol=0.002)
    assert localizer.locate(resized, evidence)["status"] == "unlocalized"
    assert (
        localizer.locate(resized, {**evidence, "sequence": 4}, geometry)["status"] == "unlocalized"
    )


def test_navigation_replay_keeps_exact_inputs_and_bounded_history(tmp_path):
    import json
    import zipfile
    from toposync_ext_cameras.processing.panorama_localization_replay import LocalizationReplay

    replay = LocalizationReplay()
    for sequence in range(5):
        image = np.full((32, 48, 3), sequence, dtype=np.uint8)
        replay.observe({'image': image, 'capture_evidence': {'sequence': sequence}},
                       {'status': 'unlocalized', 'reason': 'insufficient'})
        image[:] = 100  # A decoder recycling its array cannot change the evidence.
    for operation in range(3):
        replay.preserve(tmp_path, {'sequence': operation})
    files = list(tmp_path.glob('*.zip'))
    assert len(files) == 2
    for path in files:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            assert manifest['operation']['sequence'] in [1, 2]
            assert [frame['capture_evidence']['sequence'] for frame in manifest['frames']] == [2, 3, 4]
            with archive.open('frame-0.npy') as stream:
                assert (np.lib.format.read_array(stream, allow_pickle=False) == 2).all()
            assert manifest['truncated'] is False


def test_pruned_reverse_search_is_identical_to_exhaustive_reciprocity():
    from toposync_ext_cameras.processing.panorama_localization import _mutual_matches
    generator = np.random.default_rng(400)
    first = generator.normal(size=(700, 128)).astype(np.float32)
    second = generator.normal(size=(600, 128)).astype(np.float32)
    second[:100] = first[200:300] + generator.normal(0, .01, (100, 128))
    matcher = cv2.BFMatcher()
    forward = matcher.knnMatch(first, second, k=2)
    backward = matcher.knnMatch(second, first, k=2)
    reverse = {pair[0].queryIdx: pair[0].trainIdx for pair in backward if pair[0].distance < pair[1].distance * .7}
    exhaustive = {(pair[0].queryIdx, pair[0].trainIdx) for pair in forward
                  if pair[0].distance < pair[1].distance * .7 and reverse.get(pair[0].trainIdx) == pair[0].queryIdx}
    assert len(exhaustive) >= 100
    assert {(pair.queryIdx, pair.trainIdx) for pair in _mutual_matches(matcher, first, second)} == exhaustive


def test_navigation_replay_serializes_shared_storage_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from toposync_ext_cameras.processing.panorama_localization_replay import LocalizationReplay

    def preserve(sequence):
        replay = LocalizationReplay()
        replay.observe({"image": np.zeros((32, 48, 3), dtype=np.uint8)}, {"status": "unlocalized"})
        replay.preserve(tmp_path, {"sequence": sequence})

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(preserve, range(8)))
    assert len(list(tmp_path.glob("*.zip"))) == 2
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("change", ["movement", "transient", "epoch", "blank", "zoom", "ambiguous", "expired"])
def test_tracked_registration_measures_new_pixels_and_retains_original_gates(tmp_path, monkeypatch, change):
    from collections import OrderedDict
    import time

    lens, points, references, _ = _correspondences()
    image = np.random.default_rng(61).integers(0, 256, (540, 960), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (3, 3), 0.7)
    reference = {"id": "original", "rotation_matrix": np.eye(3).tolist()}
    pairs = [(reference, points.astype(np.float32), references)]
    if change == "ambiguous":
        other_rotation, _ = cv2.Rodrigues(np.array([0.0, 0.2, 0.0]))
        pairs.append(({"id": "alternative", "rotation_matrix": other_rotation.tolist()},
                      points.astype(np.float32), references))
    localizer = object.__new__(PanoramaLocalizer)
    localizer.lens = lens
    localizer.model_directory = tmp_path
    localizer.references = [reference for reference, _, _ in pairs]
    localizer.results = OrderedDict()
    localizer._matching_retry_after = float("inf")  # No recognition or downloads in this unit test.
    key = ("capture", 1, np.eye(3).tobytes(), image.shape)
    anchor = {"gray": image.copy(), "pairs": pairs, "seen": time.monotonic(), "sequence": 1}
    if change == "expired":
        anchor["seen"] -= 6
    localizer._anchors = OrderedDict([(key, anchor)])
    def recognize(*args, **kwargs):
        assert change not in {"movement", "transient", "ambiguous"}, "Measure eligible anchors before costly recognition"
        return {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
    monkeypatch.setattr(localizer, "_locate", recognize)
    if change in {"blank", "transient"}:
        current = np.zeros_like(image)
    elif change == "zoom":
        current = cv2.warpAffine(image, cv2.getRotationMatrix2D((480, 270), 0, 1.12), (960, 540))
    else:
        current = cv2.warpAffine(image, np.float32([[1, 0, 2], [0, 1, 3]]), (960, 540))
    evidence = {"capture_instance": "new" if change == "epoch" else "capture",
                "generation": 1, "sequence": 2}
    diagnostics = {}
    last_qualified = anchor["seen"]
    result = localizer._locate_frame(current, evidence, None, diagnostics)
    if change == "transient":
        assert result["status"] == "unlocalized"
        assert "rotation_matrix" not in result
        assert anchor["seen"] == last_qualified
        assert localizer._anchors[key] is anchor
        evidence = {**evidence, "sequence": 3}
        current = cv2.warpAffine(image, np.float32([[1, 0, 2], [0, 1, 3]]), (960, 540))
        result = localizer._locate_frame(current, evidence, None, diagnostics)
    if change in {"movement", "transient"}:
        assert result["status"] == "localized"
        assert diagnostics["photometry"] == "tracked_correspondences"
        assert result["capture_evidence"] == evidence
        measured = np.array(diagnostics["candidates"][0]["current"])
        expected = {tuple(reference): point + [2, 3] for point, reference in zip(points, references)}
        observed_references = diagnostics["candidates"][0]["reference"]
        positions = np.array([expected[tuple(reference)] for reference in observed_references])
        assert np.median(np.linalg.norm(measured - positions, axis=1)) < 0.1
        assert np.array_equal(anchor["gray"], image)
        assert np.array_equal(anchor["pairs"][0][1], points.astype(np.float32))
    else:
        assert result["status"] == "unlocalized"
        if change == "ambiguous":
            assert result["reason"] == "panorama_visual_localization_ambiguous"
