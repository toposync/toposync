from __future__ import annotations

import asyncio
import time
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync_ext_cameras.processing.planar_localization import PlanarLocalizer
from toposync_ext_cameras.processing.panorama_localization import PanoramaLocalizer
from toposync_ext_cameras.processing.visual_region import canonical_region, frame_to_reference, reference_mask_points
from toposync_ext_cameras.visual_region_models import CanonicalRegionConfig

from test_camera_visual_region_service import setup
from test_camera_visual_region_geometry import EVIDENCE, location


def region():
    image = np.random.default_rng(291).integers(0, 256, (100, 200, 3), dtype=np.uint8)
    mask = np.zeros((100, 200), dtype=np.uint8)
    mask[30:70, 70:130] = 255
    located = {**location(), "status": "localized", "capture_evidence": EVIDENCE}
    return image, mask, np.full_like(mask, 255), located, image_geometry(200, 100, EVIDENCE)


def test_canonical_pixels_are_equal_after_known_camera_translation_and_do_not_modify_inputs():
    image, mask, coverage, located, geometry = region()
    expected = image[30:70, 70:130].copy()
    reference = canonical_region(image, mask, coverage, located, geometry)
    np.testing.assert_array_equal(reference, expected)
    transformed = cv2.warpAffine(image, np.float32([[1, 0, 12], [0, 1, 5]]), (200, 100))
    located = {**located, "homography": [[1, 0, -12], [0, 1, -5], [0, 0, 1]]}
    recovered = canonical_region(transformed, mask, coverage, located, geometry)
    np.testing.assert_array_equal(recovered, reference)
    assert not recovered.flags.writeable
    np.testing.assert_array_equal(image[30:70, 70:130], expected)


@pytest.mark.parametrize("translation,reason", [(90, "partial"), (180, "outside")])
def test_partial_or_missing_region_has_no_classifiable_pixels(translation, reason):
    image, mask, coverage, located, geometry = region()
    located["homography"][0][2] = translation
    with pytest.raises(ValueError, match=f"visual_region_{reason}$"):
        canonical_region(image, mask, coverage, located, geometry)


@pytest.mark.parametrize("fault", ["coverage", "support", "identity", "unlocalized", "geometry"])
def test_canonical_region_rejects_holes_unverified_pose_and_missing_support(fault):
    image, mask, coverage, located, geometry = region()
    if fault == "coverage":
        coverage[50, 100] = 0
    elif fault == "support":
        located["support_pixels"] = [[0, 0], [0, 10], [10, 10]]
    elif fault == "identity":
        located["capture_evidence"] = {**EVIDENCE, "sequence": 2}
    elif fault == "unlocalized":
        located["status"] = "unlocalized"
    else:
        geometry["image_size"] = [201, 100]
    with pytest.raises(ValueError):
        canonical_region(image, mask, coverage, located, geometry)


def test_alignment_mask_rejects_nonfinite_and_outside_points_without_clipping_into_support():
    mask = np.full((10, 20), 255, np.uint8)
    result = reference_mask_points(mask, np.array([[5, 5], [-1, 5], [20, 5], [np.nan, 2], [1e300, 2]]))
    assert result.tolist() == [True, False, False, False, False]


def test_padding_does_not_recover_pixels_removed_by_an_upstream_crop():
    image, mask, coverage, located, geometry = region()
    original = SimpleNamespace(data=image, metadata={"image_geometry": geometry})
    cropped = SimpleNamespace(data=image[:, 100:], metadata=transformed_geometry(original,
        [[1, 0, -100], [0, 1, 0], [0, 0, 1]], 100, 100))
    padded = transformed_geometry(cropped, [[1, 0, 100], [0, 1, 0], [0, 0, 1]], 200, 100)["image_geometry"]
    with pytest.raises(ValueError, match="visual_region_partial"):
        canonical_region(image, mask, coverage, located, padded)


@pytest.mark.parametrize("seam", [False, True])
def test_spherical_canonical_pixels_follow_an_independent_pinhole_oracle(seam):
    y, x = np.mgrid[:100, :200]
    image = np.stack((x, y, x * 0), axis=-1).astype(np.uint8)
    mask = np.zeros((500, 1000), np.uint8)
    columns = np.r_[np.arange(993, 1000), np.arange(8)] if seam else np.arange(495, 506)
    mask[245:256, columns] = 255
    lens = {"width": 200, "height": 100, "fx": 150, "fy": 150, "cx": 99.5, "cy": 49.5, "distortion": [0, 0, 0, 0, 0]}
    rotation = cv2.Rodrigues(np.array([0., np.pi if seam else 0., 0.]))[0]
    located = {**location(), "status": "localized", "capture_evidence": EVIDENCE,
               "lens": lens, "rotation_matrix": rotation.tolist()}
    located.pop("homography")
    output = canonical_region(image, mask, np.full_like(mask, 255), located,
                              image_geometry(200, 100, EVIDENCE), wrap=True)
    pan = (columns / 999 * 2 - 1) * np.pi
    if seam:
        pan = (pan - np.pi + np.pi) % (2 * np.pi) - np.pi
    tilt = (.5 - np.arange(245, 256) / 499) * np.pi
    expected_x = 150 * np.tan(pan) + 99.5
    expected_y = -150 * np.tan(tilt[:, None]) / np.cos(pan) + 49.5
    assert output.shape == (11, len(columns), 3)
    assert np.max(np.abs(output[:, :, 0] - expected_x)) <= 1
    assert np.max(np.abs(output[:, :, 1] - expected_y)) <= 1


def test_panorama_context_is_applied_to_native_and_tracked_matches_without_mutating_navigation(tmp_path):
    from test_camera_panorama_localization import _correspondences

    image = cv2.GaussianBlur(np.random.default_rng(500).integers(0, 256, (540, 960), np.uint8), (5, 5), 1)
    path = tmp_path / "reference.png"
    assert cv2.imwrite(str(path), image)
    lens, _, _, _ = _correspondences()
    base = PanoramaLocalizer({"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
                             [{"id": "one", "path": str(path)}])
    context = np.full((500, 1000), 255, np.uint8)
    context[220:280, 470:530] = 0
    localizer = base.with_reference_mask(context)
    context[:] = 0  # Caller mutation cannot change the bound region.
    for sequence in (1, 2):
        evidence = {**EVIDENCE, "sequence": sequence}
        located = localizer.locate(image, evidence, image_geometry(960, 540, evidence))
        assert located["status"] == "localized"
        mapped = frame_to_reference(np.asarray(located["support_pixels"]), located,
                                    {"to_source": np.eye(3)}, [1000, 500])
        assert reference_mask_points(localizer.reference_mask, mapped).all()
        np.testing.assert_allclose(located["rotation_matrix"], np.eye(3), atol=1e-3)
    assert not base.results and not base._anchors and not hasattr(base, "reference_mask")
    assert localizer._anchors
    assert list(localizer.results.values())[-1][1]["photometry"] == "tracked_correspondences"
    empty = base.with_reference_mask(np.zeros((500, 1000), np.uint8))
    assert empty.locate(image, EVIDENCE)["status"] == "unlocalized"
    ambiguous = base.with_reference_mask(np.full((500, 1000), 255, np.uint8))
    ambiguous.references.append({**ambiguous.references[0], "id": "competing",
        "rotation_matrix": cv2.Rodrigues(np.array([0., .05, 0.]))[0].tolist()})
    assert ambiguous.locate(image, EVIDENCE)["reason"] == "panorama_visual_localization_ambiguous"


def test_repeated_cancellation_keeps_native_work_in_flight_until_return():
    from toposync_ext_cameras.visual_regions import _compute

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        slots = asyncio.Semaphore(1)

        def native():
            entered.set()
            release.wait(2)

        async def work():
            async with slots:
                await _compute(native)

        task = asyncio.create_task(work())
        try:
            while not entered.is_set():
                await asyncio.sleep(.005)
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and slots.locked()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not slots.locked()

    asyncio.run(scenario())


def test_planar_alignment_uses_surrounding_context_even_when_target_moves():
    image = np.random.default_rng(712).integers(0, 256, (540, 960, 3), dtype=np.uint8)
    mask = np.full((540, 960), 255, np.uint8)
    mask[150:390, 300:660] = 0
    base = PlanarLocalizer(image, [960, 540])
    original_points = base.reference_points.copy()
    localizer = base.with_reference_mask(mask)
    assert len(localizer.reference_points) < len(original_points)
    assert reference_mask_points(mask, localizer.reference_points).all()
    current = image.copy()
    current[170:370, 320:640] = np.roll(current[170:370, 320:640], 70, axis=1)
    geometry = image_geometry(960, 540, EVIDENCE)
    located = localizer.locate(current, EVIDENCE, geometry)
    assert located["status"] == "localized"
    np.testing.assert_allclose(located["homography"], np.eye(3), atol=.5)
    target = np.zeros_like(mask)
    target[180:360, 330:630] = 255
    output = canonical_region(current, target, np.full_like(mask, 255), located, geometry, maximum_side=512)
    np.testing.assert_array_equal(output, current[180:360, 330:630])
    assert not np.array_equal(output, image[180:360, 330:630])
    np.testing.assert_array_equal(base.reference_points, original_points)
    assert not base.results and localizer.results


def test_target_texture_alone_cannot_establish_alignment():
    image = np.zeros((540, 960, 3), np.uint8)
    image[150:390, 300:660] = np.random.default_rng(9).integers(0, 256, (240, 360, 3), dtype=np.uint8)
    context = np.full((540, 960), 255, np.uint8)
    context[140:400, 290:670] = 0
    localizer = PlanarLocalizer(image, [960, 540]).with_reference_mask(context)
    assert localizer.locate(image, EVIDENCE, image_geometry(960, 540, EVIDENCE))["status"] == "unlocalized"


def test_shared_service_prepares_capture_and_inference_without_camera_or_motion_calls(tmp_path):
    async def scenario():
        service, config, image, evidence, counts = setup(tmp_path)
        configuration = CanonicalRegionConfig.model_validate({**config.model_dump(), "maximum_side": 128})
        evidence["published_at"] = time.time()
        geometry = image_geometry(960, 540, evidence)
        first = await service.prepare(config=configuration.model_dump(), image=image, evidence=evidence, geometry=geometry)
        second = await service.prepare(config=configuration.model_dump(), image=image, evidence=evidence, geometry=geometry)
        assert first["status"] == second["status"] == "ready", (first, second)
        assert first["preparation_revision"] == second["preparation_revision"]
        np.testing.assert_array_equal(first["_image"], second["_image"])
        assert max(first["_image"].shape[:2]) == 128
        assert counts == {"capture": 0, "motion": 0}
        assert len(service.canonical) == 1
        missing = await service.prepare(config=configuration.model_dump(), image=np.zeros_like(image),
                                        evidence=evidence, geometry=geometry)
        assert missing["status"] == "unavailable" and "_image" not in missing
        assert missing["preparation_revision"] == first["preparation_revision"]
        changed = configuration.model_copy(update={"maximum_side": 64})
        assert changed.geometry_key() != configuration.geometry_key()
        for maximum in (64, 96, 192):
            revised = configuration.model_copy(update={"maximum_side": maximum})
            next_result = await service.prepare(config=revised.model_dump(), image=image, evidence=evidence, geometry=geometry)
            assert next_result["status"] == "ready" and max(next_result["_image"].shape[:2]) == maximum
            assert len(service.canonical) <= 2
        with pytest.raises(ValidationError):
            CanonicalRegionConfig.model_validate({**configuration.model_dump(), "framing": "maintain"})
        evidence["published_at"] = time.time() - 100
        refused = await service.prepare(config=configuration.model_dump(), image=image, evidence=evidence,
                                        geometry=image_geometry(960, 540, evidence))
        assert refused["status"] == "unavailable" and "_image" not in refused
        assert refused["reason"] == "visual_region_frame_expired"

    asyncio.run(scenario())
