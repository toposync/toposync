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


@pytest.mark.parametrize("case", ["rotation", "static", "foreground", "zoom", "missing_matcher", "dimensions"])
def test_calibrated_transition_uses_observed_rotation_not_scene_change(case):
    from types import SimpleNamespace

    lens, current, reference, _ = _correspondences()
    if case == "static":
        reference = current.copy()
    elif case == "foreground":
        reference[100:] = current[100:]
    elif case == "zoom":
        reference = (current - [lens["cx"], lens["cy"]]) * 1.2 + [lens["cx"], lens["cy"]]
    localizer = PanoramaLocalizer.__new__(PanoramaLocalizer)
    localizer.lens = lens
    localizer._contextual_matcher = None if case == "missing_matcher" else SimpleNamespace(
        width=960, height=540, features=lambda image: image,
        correspondences=lambda before, after: (reference, current),
    )
    previous = np.zeros((540, 960), np.uint8)
    image = previous[:, :800] if case == "dimensions" else previous.copy()
    result = localizer.calibrated_transition(previous, image)
    assert (result is not None) == (case == "rotation")
    if result is not None:
        assert result["motion_pixels"] > 20
        assert result["validation_p95_pixels"] < 1e-5


@pytest.mark.parametrize("ordinary_decision", ["insufficient", "qualified", "ambiguous", "both_insufficient"])
@pytest.mark.parametrize("start_normalized", [False, True])
def test_contextual_contrast_retry_preserves_decisions_and_separates_photometry(
    tmp_path, monkeypatch, ordinary_decision, start_normalized,
):
    from types import SimpleNamespace

    lens, current, reference, rotation = _correspondences()
    photograph = np.full((540, 960, 3), 80, np.uint8)
    image = np.full((540, 960, 3), 40, np.uint8)
    path = tmp_path / "reference.png"
    cv2.imwrite(str(path), photograph)
    localizer = PanoramaLocalizer(
        {"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}], model_directory=tmp_path,
    )
    calls = []

    def features(pixels, *, normalize=False):
        calls.append((int(pixels[0, 0, 0]), normalize))
        return normalize

    def correspondences(first, second):
        assert first == second, "Photometric descriptor forms must never mix"
        if ordinary_decision == "both_insufficient" or (ordinary_decision == "insufficient" and first == start_normalized):
            return np.empty((0, 2)), np.empty((0, 2))
        return current, reference

    localizer._contextual_matcher = SimpleNamespace(
        width=960, height=540, features=features, correspondences=correspondences,
    )
    if ordinary_decision == "ambiguous":
        monkeypatch.setattr(localizer, "_fit_contextual", lambda *args: {
            "status": "unlocalized", "reason": "panorama_visual_localization_ambiguous",
        })
    result = localizer._contextual_locate(image, np.eye(3), ("test", 1, 1), {}, normalize=start_normalized)
    if ordinary_decision == "both_insufficient":
        assert result["status"] == "unlocalized"
        assert calls.count((40, False)) == calls.count((40, True)) == 1
        assert not localizer._anchors
    elif ordinary_decision == "ambiguous":
        assert result["reason"] == "panorama_visual_localization_ambiguous"
        assert all(normalize == start_normalized for _, normalize in calls)
        assert not localizer._anchors
    else:
        assert result["status"] == "localized"
        assert np.allclose(result["rotation_matrix"], rotation, atol=1e-6)
        normalized = start_normalized ^ (ordinary_decision == "insufficient")
        anchor = next(iter(localizer._anchors.values()))
        assert anchor["normalize"] is normalized
        assert np.array_equal(anchor["gray"], image[:, :, 0])
        assert calls.count((80, start_normalized)) == 1
        assert calls.count((80, not start_normalized)) == int(ordinary_decision == "insufficient")
        # A fresh observation reuses only descriptors of its own photometric form.
        localizer._contextual_locate(image, np.eye(3), ("test", 1, 2), {}, normalize=normalized)
        assert calls.count((80, normalized)) == 1


@pytest.mark.parametrize('multiplicity', [1, 3])
def test_complementary_photometry_qualifies_only_with_distributed_independent_support(tmp_path, multiplicity):
    from types import SimpleNamespace

    lens, current, reference, rotation = _correspondences()
    image = np.full((540, 960, 3), 40, np.uint8)
    path = tmp_path / 'reference.png'
    assert cv2.imwrite(str(path), image)
    localizer = PanoramaLocalizer(
        {'lens': lens, 'captures': [{'id': 'one', 'rotation_matrix': np.eye(3).tolist()}]},
        [{'id': 'one', 'path': str(path)}], model_directory=tmp_path,
    )

    def correspondences(first, second):
        assert first == second
        mask = current[:, 0] >= 480 if first else current[:, 0] < 480
        a, b = current[mask], reference[mask]
        assert fit_frame_rotation(a, b, lens, np.eye(3)) is None
        return np.tile(a, (multiplicity, 1)), np.tile(b, (multiplicity, 1))

    localizer._contextual_matcher = SimpleNamespace(
        width=960, height=540, features=lambda _, normalize=False: normalize,
        correspondences=correspondences,
    )
    diagnostics = {}
    result = localizer._contextual_locate(image, np.eye(3), ('test', 1, 1), diagnostics)
    oracle = fit_frame_rotation(current, reference, lens, np.eye(3))
    assert result['status'] == 'localized'
    assert diagnostics['photometry'] == 'combined_photometry_contextual_correspondences'
    assert result['validation_matches'] == oracle['validation_matches']
    assert np.allclose(result['rotation_matrix'], rotation, atol=1e-6)
    assert result['validation_p95_pixels'] < 1e-5


@pytest.mark.parametrize('age,extra', [(0, True), (6, False)])
def test_recent_geometry_only_adds_one_reference_hint_to_full_candidate_search(age, extra):
    import time
    from collections import OrderedDict
    from toposync_ext_cameras.processing.panorama_localization import MAXIMUM_CANDIDATES

    localizer = object.__new__(PanoramaLocalizer)
    localizer.references = [{'id': str(i), 'coarse': None} for i in range(MAXIMUM_CANDIDATES + 3)]
    localizer.last_reference = str(MAXIMUM_CANDIDATES)
    localizer._anchors = OrderedDict([('other-decoder', {
        'seen': time.monotonic() - age, 'neighbors': localizer.references[-2:],
    })])
    candidates = localizer._rank_references(None)
    assert [item['id'] for item in candidates[:MAXIMUM_CANDIDATES]] == [str(i) for i in range(MAXIMUM_CANDIDATES)]
    assert len(candidates) == MAXIMUM_CANDIDATES + 1 + int(extra)
    assert (localizer.references[-2] in candidates) is extra


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


@pytest.mark.parametrize("diagnostic_first", [False, True])
@pytest.mark.parametrize("blank", [False, True])
def test_diagnostics_reuse_the_original_decision_and_evidence(tmp_path, monkeypatch, diagnostic_first, blank):
    import copy

    image = cv2.GaussianBlur(
        np.random.default_rng(500).integers(0, 256, (540, 960), np.uint8), (5, 5), 1
    )
    path = tmp_path / "reference.png"
    assert cv2.imwrite(str(path), image)
    localizer = PanoramaLocalizer(
        {"lens": _correspondences()[0],
         "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    if blank:
        image = np.zeros_like(image)
    evidence = {"capture_instance": "first", "generation": 1, "sequence": 1}
    first = (localizer.locate_diagnostic if diagnostic_first else localizer.locate)(image, evidence)
    expected = {key: value for key, value in first.items() if key != "diagnostics"}

    def unexpected_recognition(*args, **kwargs):
        pytest.fail("Inspecting the same frame must not run recognition again")

    for method in ("_locate", "_tracked_locate", "_contextual_locate"):
        monkeypatch.setattr(localizer, method, unexpected_recognition)
    diagnostic = localizer.locate_diagnostic(image, evidence)
    assert {key: value for key, value in diagnostic.items() if key != "diagnostics"} == expected
    assert diagnostic["diagnostics"]["model_sha256"] == localizer.model_digest
    assert diagnostic["diagnostics"]["photometry"] == ("local_contrast" if blank else "native")
    original = copy.deepcopy(diagnostic)
    if not blank:
        assert diagnostic["diagnostics"]["candidates"][0]["stage"] == "accepted"
        diagnostic["diagnostics"]["candidates"][0]["current"][0][0] = -999
        diagnostic["rotation_matrix"][0][0] = -999
    diagnostic["capture_evidence"]["sequence"] = -999
    assert localizer.locate_diagnostic(image, evidence) == original
    assert localizer.locate(image, evidence) == expected


@pytest.mark.parametrize("change", ["sequence", "generation", "capture_instance", "bytes", "geometry", "shape"])
def test_cached_diagnostics_cannot_cross_frame_or_geometry_changes(monkeypatch, change):
    from collections import OrderedDict
    from toposync.runtime.pipelines.image_geometry import image_geometry

    localizer = object.__new__(PanoramaLocalizer)
    localizer.lens = _correspondences()[0]
    localizer.results = OrderedDict()
    calls = []

    def recognize(*args, **kwargs):
        calls.append(True)
        return {"status": "unlocalized", "reason": "panorama_visual_localization_ambiguous"}

    monkeypatch.setattr(localizer, "_locate", recognize)
    image = np.zeros((540, 960), np.uint8)
    evidence = {"capture_instance": "first", "generation": 1, "sequence": 1}
    geometry = image_geometry(960, 540, evidence)
    localizer._locate_frame(image, evidence, geometry, {})
    if change in evidence:
        evidence = {**evidence, change: "second" if change == "capture_instance" else 2}
        geometry["capture_evidence"] = evidence
    elif change == "bytes":
        image[0, 0] = 1
    elif change == "geometry":
        geometry["to_source"][0][2] = 1
    else:
        image = image.reshape(270, 1920)
        geometry["image_size"] = [1920, 270]
    localizer._locate_frame(image, evidence, geometry, {})
    assert len(calls) == 2
    localizer._locate_frame(image, evidence, geometry, {})
    assert len(calls) == 2
    geometry["source_size"] = [320, 240]
    assert localizer._locate_frame(image, evidence, geometry, {})["reason"] == "panorama_source_geometry_changed"
    assert len(calls) == 2


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


@pytest.mark.parametrize("limited", [False, True])
def test_navigation_replay_retains_slow_inputs_without_expanding_byte_budget(tmp_path, monkeypatch, limited):
    import json
    import zipfile
    from toposync_ext_cameras.processing import panorama_localization_replay as module

    if limited:
        monkeypatch.setattr(module, "MAXIMUM_OPERATION_BYTES", 1024**2 + 4000)
    replay = module.LocalizationReplay()
    for sequence in range(8):
        image = np.full((20, 50), sequence, dtype=np.uint8)
        replay.observe({"image": image, "capture_evidence": {"sequence": sequence}},
                       {"status": "localized"}, elapsed_seconds=.8 if sequence < 2 else .01)
        image[:] = 99
    replay.preserve(tmp_path, {"sequence": 1})
    path = next(tmp_path.glob("*.zip"))
    assert path.stat().st_size <= module.MAXIMUM_OPERATION_BYTES
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        sequences = [f["capture_evidence"]["sequence"] for f in manifest["frames"]]
        assert sequences == ([0, 5, 6, 7] if limited else [0, 1, 5, 6, 7])
        assert manifest["truncated"] is limited
        assert manifest["frames"][0]["elapsed_seconds"] == .8
        for index, sequence in enumerate(sequences):
            with archive.open(f"frame-{index}.npy") as stream:
                assert (np.lib.format.read_array(stream, allow_pickle=False) == sequence).all()


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


def test_fixed_anchor_tracking_can_measure_large_displacement():
    from toposync_ext_cameras.processing.panorama_correspondences import track_image_points

    generator = np.random.default_rng(41)
    image = np.zeros((540, 960), np.uint8)
    for _ in range(500):
        x, y = generator.integers([20, 20], [940, 520])
        cv2.circle(image, (int(x), int(y)), int(generator.integers(3, 12)),
                   int(generator.integers(40, 255)), -1)
    image = cv2.GaussianBlur(image, (5, 5), 1)
    points = cv2.goodFeaturesToTrack(image, 500, .01, 10).reshape(-1, 2)
    current = cv2.warpAffine(image, np.float32([[1, 0, 60], [0, 1, 0]]), (960, 540))
    measured, valid = track_image_points(image, current, points, .75, large_displacement=True)
    correct = np.linalg.norm(measured[valid] - points[valid] - [60, 0], axis=1) < .5
    assert correct.sum() >= 100
    assert correct.mean() >= .8
    # These are proposals, not a pose. Independent fitting remains mandatory.
    _, blank_valid = track_image_points(image, np.zeros_like(image), points, .75,
                                       large_displacement=True)
    assert blank_valid.sum() == 0


def test_batched_tracking_preserves_uneven_reference_pairs_and_ambiguity(monkeypatch):
    from collections import OrderedDict
    import time
    from toposync_ext_cameras.processing import panorama_localization as module

    image = np.random.default_rng(93).integers(0, 256, (540, 960), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (3, 3), .7)
    current = cv2.warpAffine(image, np.float32([[1, 0, 3], [0, 1, 2]]), (960, 540))
    points = cv2.goodFeaturesToTrack(image, 120, .01, 10).reshape(-1, 2)
    groups = [points[:45], points[45:], points[:0]]
    pairs = [({"id": str(index)}, group, group + [index * 100, 17])
             for index, group in enumerate(groups)]
    expected = []
    for reference, group, original_reference in pairs:
        measured, valid = module.track_image_points(image, current, group, .75)
        expected.append((reference, measured[valid], original_reference[valid]))
    localizer = object.__new__(PanoramaLocalizer)
    key = ("capture", 1, np.eye(3).tobytes(), image.shape)
    localizer._anchors = OrderedDict([(key, {"gray": image, "pairs": pairs,
                                            "seen": time.monotonic(), "sequence": 1})])
    track = module.track_image_points
    calls = []
    def tracked(*args, **kwargs):
        calls.append(len(args[2]))
        return track(*args, **kwargs)
    def fit(measured_pairs, *args):
        for measured, original in zip(measured_pairs, expected, strict=True):
            assert measured[0] == original[0]
            np.testing.assert_array_equal(measured[1], original[1])
            np.testing.assert_array_equal(measured[2], original[2])
        return {"status": "unlocalized", "reason": "panorama_visual_localization_ambiguous"}
    monkeypatch.setattr(module, "track_image_points", tracked)
    monkeypatch.setattr(localizer, "_fit_contextual", fit)
    result = localizer._tracked_locate(current, np.eye(3), ("capture", 1, 2), {})
    assert result["reason"] == "panorama_visual_localization_ambiguous"
    assert calls == [len(points)]
    assert not localizer._anchors


@pytest.mark.parametrize("cross_capture", [False, True])
@pytest.mark.parametrize("change", ["movement", "transient", "epoch", "generation", "blank", "zoom", "ambiguous", "expired", "crop", "source_geometry"])
def test_tracked_registration_measures_new_pixels_and_retains_original_gates(tmp_path, monkeypatch, change, cross_capture):
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
        assert change not in {"movement", "ambiguous"}, "Measure eligible anchors before costly recognition"
        if change == "transient":
            assert cross_capture and not np.any(args[0]), "Only lost cross-capture support needs global recovery"
        return {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
    monkeypatch.setattr(localizer, "_locate", recognize)
    if change in {"blank", "transient"}:
        current = np.zeros_like(image)
    elif change == "zoom":
        current = cv2.warpAffine(image, cv2.getRotationMatrix2D((480, 270), 0, 1.12), (960, 540))
    else:
        current = cv2.warpAffine(image, np.float32([[1, 0, 2], [0, 1, 3]]), (960, 540))
    evidence = {"capture_instance": "new" if change == "epoch" or cross_capture else "capture",
                "generation": 1, "sequence": 2}
    if change == "generation":
        evidence = {**evidence, "capture_instance": "capture", "generation": 2}
    geometry = None
    if change == "crop":
        geometry = {"source_size": [960, 540], "image_size": [960, 540],
                    "to_source": [[1, 0, 12], [0, 1, 0], [0, 0, 1]], "capture_evidence": evidence}
    if change == "source_geometry":
        geometry = {"source_size": [961, 540], "image_size": [960, 540],
                    "to_source": np.eye(3).tolist(), "capture_evidence": evidence}
    # Epoch changes alone never opt in. Only the source-qualified browser path
    # can propose original-reference pairs from an independent capture.
    options = {"reference_candidates": cross_capture and change != "epoch"}
    diagnostics = {}
    last_qualified = anchor["seen"]
    result = localizer._locate_frame(current, evidence, geometry, diagnostics, **options)
    if change == "transient":
        assert result["status"] == "unlocalized"
        assert "rotation_matrix" not in result
        assert anchor["seen"] == last_qualified
        assert localizer._anchors[key] is anchor
        evidence = {**evidence, "sequence": 3}
        current = cv2.warpAffine(image, np.float32([[1, 0, 2], [0, 1, 3]]), (960, 540))
        result = localizer._locate_frame(current, evidence, None, diagnostics, **options)
    if change in {"movement", "transient"}:
        assert result["status"] == "localized"
        assert diagnostics["photometry"] == ("cross_capture_correspondences" if cross_capture else "tracked_correspondences")
        assert result["capture_evidence"] == evidence
        measured = np.array(diagnostics["candidates"][0]["current"])
        expected = {tuple(reference): point + [2, 3] for point, reference in zip(points, references)}
        observed_references = diagnostics["candidates"][0]["reference"]
        positions = np.array([expected[tuple(reference)] for reference in observed_references])
        assert np.median(np.linalg.norm(measured - positions, axis=1)) < 0.1
        assert np.array_equal(anchor["gray"], image)
        assert np.array_equal(anchor["pairs"][0][1], points.astype(np.float32))
        if cross_capture:
            assert anchor["sequence"] == 1
            assert list(localizer._anchors) == [key]  # No identity relabel or anchor chain.
    else:
        assert result["status"] == "unlocalized"
        if change == "ambiguous":
            assert result["reason"] == "panorama_visual_localization_ambiguous"


@pytest.mark.parametrize("scale", [1, 2])
def test_native_recognition_seeds_original_pixels_and_recovers_after_tracking_loss(monkeypatch, scale):
    from collections import OrderedDict

    lens, points, reference_points, _ = _correspondences()
    image = np.random.default_rng(61).integers(0, 256, (540, 960), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (3, 3), 0.7)
    localizer = object.__new__(PanoramaLocalizer)
    localizer.lens = {**lens, **{key: lens[key] * scale for key in ("width", "height", "fx", "fy")},
                      "cx": (lens["cx"] + .5) * scale - .5, "cy": (lens["cy"] + .5) * scale - .5}
    reference = {"id": "original", "rotation_matrix": np.eye(3).tolist()}
    localizer.references = [reference]
    localizer.results, localizer._anchors = OrderedDict(), OrderedDict()
    localizer.model_directory = None
    source_points = (points + .5) * scale - .5
    source_references = (reference_points + .5) * scale - .5
    calls = []

    def recognize(current, matrix, diagnostics, **kwargs):
        calls.append(current.copy())
        diagnostics.update(photometry="native", candidates=[])
        if not current.any():
            return {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
        candidate = {"reference_id": "original"}
        fit = fit_frame_rotation(source_points, source_references, localizer.lens, np.eye(3), candidate)
        diagnostics["candidates"].append(candidate)
        return {"status": "localized", **fit, "reference_id": "original"}

    monkeypatch.setattr(localizer, "_locate", recognize)
    monkeypatch.setattr(localizer, "_recognition_neighbors", lambda result: [reference])
    for sequence, current in enumerate([image, cv2.warpAffine(image, np.float32([[1, 0, 2], [0, 1, 3]]),
                                                               (960, 540)), np.zeros_like(image)], 1):
        evidence = {"capture_instance": "capture", "generation": 1, "sequence": sequence}
        geometry = {"source_size": [960 * scale, 540 * scale], "image_size": [960, 540],
                    "to_source": [[scale, 0, (scale - 1) / 2], [0, scale, (scale - 1) / 2], [0, 0, 1]],
                    "capture_evidence": evidence}
        diagnostics = {}
        result = localizer._locate_frame(current, evidence, geometry, diagnostics)
        if sequence == 2:
            assert len(calls) == 1
            assert result["status"] == "localized"
            assert diagnostics["photometry"] == "tracked_correspondences"
            assert np.array_equal(next(iter(localizer._anchors.values()))["gray"], image)
        elif sequence == 3:
            assert len(calls) == 3  # Native and normalized recovery were attempted.
            assert result["status"] == "unlocalized"
            assert "rotation_matrix" not in result
