import cv2
import numpy as np
import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync_ext_cameras.processing.planar_localization import fit_frame_homography, PlanarLocalizer
from toposync_ext_cameras.processing.visual_region import rasterize_region, project_region, evaluate_detection
from toposync_ext_cameras.visual_region_models import RegionStroke, VisualRegionConfig

EVIDENCE = {"capture_instance": "camera", "generation": 1, "sequence": 1}


def geometry():
    return image_geometry(200, 100, EVIDENCE)


def location():
    return {"homography": np.eye(3).tolist(), "reference_size": [200, 100],
            "validation_p95_pixels": .1, "analysis_width": 200,
            "support_pixels": [[x, y] for x in np.linspace(0, 199, 10) for y in np.linspace(0, 99, 6)]}


def test_draw_erase_holes_and_reject_empty_or_nonfinite():
    paint = RegionStroke(diameter01=.4, points01=[(.5, .5)])
    erase = RegionStroke(op="erase", diameter01=.1, points01=[(.5, .5)])
    mask = rasterize_region([paint, erase], 200, 100)
    assert mask[50, 100] == 0 and mask[50, 115] == 255
    with pytest.raises(ValueError, match="empty"):
        rasterize_region([paint, paint.model_copy(update={"op": "erase"})], 200, 100)
    with pytest.raises(ValidationError):
        RegionStroke(diameter01=.1, points01=[(float("nan"), .5)])


def test_seam_stroke_does_not_paint_middle():
    mask = rasterize_region([RegionStroke(diameter01=.1, points01=[(.98,.5),(.02,.5)])], 200, 100, wrap=True)
    assert mask[50, 0] and mask[50, 199] and not mask[50, 100]


def test_reference_resize_and_partial_visibility():
    mask = np.zeros((50, 100), np.uint8)
    mask[10:40, 70:90] = 255
    located = location()
    located["homography"][0][2] = 40
    projected, valid, coverage = project_region(mask, np.full_like(mask, 255), located, geometry())
    assert projected[40, 120] and not projected[40, 180]
    assert coverage["state"] == "full"
    located["homography"][0][2] = -40
    _, _, coverage = project_region(mask, np.full_like(mask, 255), located, geometry())
    assert coverage["state"] == "partial"


def test_chained_crop_cannot_restore_removed_pixels():
    original = SimpleNamespace(data=np.zeros((100,200,3),np.uint8), metadata={"image_geometry": geometry()})
    crop = transformed_geometry(original, [[1,0,-100],[0,1,0],[0,0,1]],100,100)
    cropped = SimpleNamespace(data=np.zeros((100,100,3),np.uint8), metadata=crop)
    padded = transformed_geometry(cropped, [[1,0,100],[0,1,0],[0,0,1]],200,100)["image_geometry"]
    mask = np.full((100,200),255,np.uint8)
    projected, valid, coverage = project_region(mask, mask, location(), padded)
    assert not valid[50,50] and valid[50,150]
    assert not projected[50,50] and coverage["state"] == "partial"


@pytest.mark.parametrize("criterion", ["base", "center", "overlap", "whole"])
def test_decisions_and_uncertainty(criterion):
    config = VisualRegionConfig(criterion=criterion)
    mask = np.zeros((100,200),np.uint8)
    mask[10:90,50:150] = 255
    valid = np.full_like(mask,255)
    def evaluate(bbox):
        return evaluate_detection(config, {"bbox01":bbox}, mask, valid, location(), geometry(), EVIDENCE)["classification"]
    assert evaluate([.4,.3,.6,.6]) == "inside"
    assert evaluate([.05,.3,.15,.6]) == "outside"
    if criterion in {"base", "center"}:
        assert evaluate([.2,.3,.3,.6]) == "indeterminate"
    missing = location(); missing["support_pixels"] = []
    assert evaluate_detection(config,{"bbox01":[.4,.3,.6,.6]},mask,valid,missing,geometry(),EVIDENCE)["classification"] == "indeterminate"


def test_homography_holdout_recovers_and_rejects_parallax():
    current = np.random.default_rng(31).uniform([20,20],[940,520],(500,2)).astype(np.float32)
    transform = np.array([[1.01,.02,8],[-.01,1.01,-4],[.00001,.00002,1.]])
    reference = cv2.perspectiveTransform(current[None],transform)[0]
    result = fit_frame_homography(current,reference,(960,540))
    assert result and np.allclose(result["homography"], transform, atol=.001)
    reference[::2] += [30,0]
    assert fit_frame_homography(current,reference,(960,540)) is None


def test_textureless_image_is_not_localized():
    image = np.zeros((100,200,3),np.uint8)
    localizer = PlanarLocalizer(image,[200,100])
    assert localizer.locate(image,EVIDENCE,geometry())["status"] == "unlocalized"


def test_spherical_reference_projection_and_rotation_keep_frame_coordinates():
    from toposync_ext_cameras.processing.visual_region import frame_to_reference, reference_to_frame
    located = {**location(), 'rotation_matrix': np.eye(3).tolist(),
        'lens': {'width':200,'height':100,'fx':150,'fy':150,'cx':99.5,'cy':49.5,'distortion':[-.1,.02,0,0,0]}}
    located.pop('homography')
    pixels = np.array([[99.5,49.5],[80,40],[120,60]], dtype=float)
    reference = frame_to_reference(pixels, located, geometry(), [1000,500])
    assert np.allclose(reference[0], [499.5,249.5])
    recovered, visible = reference_to_frame(reference, located, geometry(), [1000,500])
    assert visible.all() and np.allclose(recovered, pixels, atol=.01)
    mask = rasterize_region([RegionStroke(diameter01=.12,points01=[(.5,.5)])],1000,500,wrap=True)
    projected, valid, visibility = project_region(mask,np.full_like(mask,255),located,geometry())
    assert projected[50,100] and visibility['state']=='full'
    decision = evaluate_detection(VisualRegionConfig(),{'bbox01':[.45,.4,.55,.55]},projected,valid,located,geometry(),EVIDENCE)
    assert decision['classification']=='inside'
    located['rotation_matrix'] = cv2.Rodrigues(np.array([0.,1.5,0.]))[0].tolist()
    _, _, visibility = project_region(mask,np.full_like(mask,255),located,geometry())
    assert visibility['state']=='outside'
