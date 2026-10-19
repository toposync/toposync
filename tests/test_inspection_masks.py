"""Sampled masks preserve holes, capture binding and the shared pixel budget."""

from types import SimpleNamespace
import asyncio

import numpy as np
import pytest

from toposync.runtime.pipelines.inspection import PipelineInspectionStore
from toposync_ext_vision.processing.inspection import mask_visuals
from test_vision_inspection_visuals import packet, observe
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync_ext_vision.processing.contracts import SegmentationInstance
from toposync_ext_vision.processing.tasks.segmentation import VisionSegmentInstancesRuntime
from toposync_ext_vision.registry.manifests import ModelManifest, ModelRegistry


def selected(mask):
    return [(SimpleNamespace(label="synthetic", score=0.9,
        metadata={"_mask": mask, "embedding": "never-export"}), {"tracking_id": "track:1"})]


def test_mask_sampling_preserves_holes_and_copies_only_a_bounded_grid():
    original = np.zeros((360, 640), np.float32)
    original[40:320, 80:560] = 1
    original[120:240, 240:400] = 0
    masks, visuals = mask_visuals(original.shape, selected(original) * 6)
    assert len(masks) == len(visuals) == 4
    assert masks[0].shape == (90, 160) and masks[0].nbytes == 14400
    assert masks[0][20, 30] == 255 and masks[0][45, 80] == 0
    assert not np.shares_memory(masks[0], original)
    assert all("embedding" not in visual and "_mask" not in visual for visual in visuals)
    assert "never-export" not in str(visuals)
    original[:] = 0
    assert masks[0][20, 30] == 255
    for invalid in [np.zeros((90, 160)), np.full((360, 640), np.nan), None]:
        assert mask_visuals((360, 640), selected(invalid)) == ([], [])


def test_mask_pixels_stay_outside_snapshots_and_expire_with_their_frame():
    now = [100.0]
    store = PipelineInspectionStore(clock=lambda: now[0])
    token = store.open("test", "revision", {"pose": "vision.segment_instances"})
    masks, visuals = mask_visuals((100, 100), selected(np.ones((100, 100))))
    observe(store, packet(), masks=masks, visuals=visuals)
    node = store.snapshot(token).nodes[0]
    assert node.visuals[0].raster.width == node.visuals[0].raster.height == 90
    assert len(store.snapshot(token).model_dump_json()) < 2500
    assert store.sessions[token].image_bytes == 30000 + 8100
    assert store.mask(token, "pose", node.image_sequence, 0) is masks[0]
    with pytest.raises(KeyError):
        store.mask(token, "pose", node.image_sequence + 1, 0)
    now[0] += 1.1
    assert not store.snapshot(token).nodes[0].visuals
    assert store.sessions[token].image_bytes == 0 and not store.sessions[token].masks
    store.close(token)


def test_mask_budget_invalid_arrays_new_capture_and_close_do_not_leave_pixels():
    store = PipelineInspectionStore(clock=lambda: 100.0)
    token = store.open("test", "revision", {"pose": "vision.segment_instances"})
    masks, visuals = mask_visuals((100, 100), selected(np.ones((100, 100))))
    store.maximum_image_bytes = 30001
    observe(store, packet(), masks=masks, visuals=visuals)
    assert not store.snapshot(token).nodes[0].visuals
    assert store.sessions[token].image_bytes == 30000
    store.maximum_image_bytes = 8388608
    for invalid in [[], [np.zeros((1, 1), np.uint8)], [np.ones((90, 90), np.float32)]]:
        observe(store, packet(), masks=invalid, visuals=visuals)
        assert not store.snapshot(token).nodes[0].visuals
    observe(store, packet(), masks=masks, visuals=visuals)
    session = store.sessions[token]
    assert session.masks
    observe(store, packet(sequence=1, generation=2), masks=[], visuals=[])
    assert not session.masks
    observe(store, packet(sequence=2, generation=2), masks=masks, visuals=visuals)
    assert session.masks
    store.close(token)
    assert not session.masks and session.image_bytes == 0


def test_segmentation_runtime_inspects_the_selected_mask_without_changing_results():
    async def scenario():
        calls = []
        class Backend:
            def segment(self, frame, **kwargs):
                calls.append("segment")
                return [SegmentationInstance(label="square", label_id=0, score=1.0,
                    bbox01=(0.1, 0.1, 0.8, 0.8), mask_artifact_name="synthetic-mask",
                    metadata={"_mask": np.ones(frame.shape[:2], np.uint8)})]
        manifest = ModelManifest(model_id="fake.mask", display_name="Synthetic mask", task="segmentation",
            runtime="fake", artifact_format="fake", artifact_path="fake://mask")
        dependencies = PipelineRuntimeDependencies(vision_model_registry=ModelRegistry([manifest]),
            segmenter_backend_factory=lambda _: Backend())
        store = PipelineInspectionStore(clock=lambda: 100.0)
        class Context:
            inspection_store = store
            def inspection_active(self):
                return bool(store.sessions)
            async def run_blocking(self, function, *args, **kwargs):
                kwargs.pop("mode", None)
                kwargs.pop("concurrency_key", None)
                return function(*args, **kwargs)
            def inspect(self, state, value, **kwargs):
                store.observe(pipeline="test", revision="revision", node="pose", physical="physical-mask",
                    operator="vision.segment_instances", producer_epoch="runtime", state=state, packet=value, **kwargs)
        runtime = VisionSegmentInstancesRuntime({"model_id": "fake.mask"}, dependencies)
        source = packet()
        baseline = await runtime.process_packet(source, Context())
        token = store.open("test", "revision", {"pose": "vision.segment_instances"})
        result = await runtime.process_packet(source, Context())
        assert result[0].payload == baseline[0].payload
        assert calls == ["segment", "segment"]
        node = store.snapshot(token).nodes[0]
        assert len(node.visuals) == 1
        assert store.mask(token, "pose", node.image_sequence, 0).shape == (90, 90)
        # A subject without geometry must not borrow a different object's segmentation.
        focused = packet(sequence=8)
        focused.payload["subject"] = {"id": "other-subject", "category": "person"}
        await runtime.process_packet(focused, Context())
        assert store.snapshot(token).nodes[0].visuals == []
        assert not store.sessions[token].masks
        store.close(token)
    asyncio.run(scenario())
