"""Synthetic publication provenance; no encoder, socket or camera is started."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.presentation import frame_presentation
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_streaming.pipelines import operators
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


def sample(sequence=1, source="main", generation=0):
    pixels = np.full((20, 40, 3), sequence, dtype=np.uint8)
    capture = {"capture_instance": "synthetic", "generation": generation,
               "sequence": sequence, "published_at": 100.0, "private": "never-export"}
    return Packet.create(stream_id=source, payload={"source": {"source_id": source, "device_id": "camera"},
        "capture_evidence": capture, "identity": "never-export"},
        artifacts={"main": Artifact("main", pixels,
            metadata={"image_geometry": image_geometry(40, 20, capture), "embedding": [0.9]})})


@pytest.mark.parametrize("transform,size", [
    ([[1, 0, -5], [0, 1, -3], [0, 0, 1]], (20, 10)),
    ([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1]], (20, 10)),
    ([[0, -1, 19], [1, 0, 0], [0, 0, 1]], (20, 40)),
])
def test_publication_preserves_declared_transform_without_private_metadata(transform, size):
    packet = sample()
    artifact = packet.artifacts["main"]
    metadata = transformed_geometry(artifact, transform, *size)
    packet.artifacts["main"] = Artifact("main", np.zeros((size[1], size[0], 3), dtype=np.uint8), metadata=metadata)
    result = frame_presentation(packet, "main")
    assert result["image_size"] == list(size)
    assert result["frame"]["sequence"] == 1
    assert "never-export" not in repr(result) and "embedding" not in repr(result)
    np.testing.assert_allclose(np.asarray(result["geometry"]["to_source"]) @ transform, np.eye(3))
    # Mutation of packet metadata cannot rewrite the published reference.
    packet.payload["capture_evidence"]["sequence"] = 99
    metadata["image_geometry"]["to_source"][0][0] = 99
    assert result["frame"]["sequence"] == 1
    assert result["geometry"]["to_source"][0][0] != 99


@pytest.mark.parametrize("corruption", ["missing", "old_capture", "singular", "nonfinite", "domains", "wrong_size"])
def test_unknown_geometry_keeps_capture_but_cannot_claim_spatial_alignment(corruption):
    packet = sample()
    geometry = packet.artifacts["main"].metadata["image_geometry"]
    if corruption == "missing":
        packet.artifacts["main"].metadata.clear()
    elif corruption == "old_capture":
        geometry["capture_evidence"]["sequence"] = 0
    elif corruption == "singular":
        geometry["to_source"] = [[0, 0, 0]] * 3
    elif corruption == "nonfinite":
        geometry["to_source"][0][0] = float("nan")
    elif corruption == "domains":
        geometry["valid_domains"] = [None]
    else:
        geometry["image_size"] = [20, 40]
    result = frame_presentation(packet, "main")
    assert result["frame"]["sequence"] == 1 and result["geometry"] is None


def test_main_and_substream_never_share_a_frame_identity():
    assert frame_presentation(sample(source="main"), "main") != frame_presentation(sample(source="sub"), "main")
    packet = sample()
    del packet.payload["capture_evidence"]
    assert frame_presentation(packet, "main") is None


def test_publication_without_new_pixels_cannot_relabel_the_previous_frame():
    async def scenario():
        state = TransmissionRuntimeState(monotonic=lambda: 100.0)
        original = sample()
        await state.update_writer_frame(transmission_id="test", writer_id="writer", lifecycle_state=Lifecycle.OPEN,
            writer_priority=0, frame=original.artifacts["main"].data, frame_ts=100,
            presentation=frame_presentation(original, "main"))
        await state.update_writer_frame(transmission_id="test", writer_id="writer", lifecycle_state=Lifecycle.UPDATE,
            writer_priority=0, frame=None, frame_ts=101, presentation=frame_presentation(sample(2), "main"))
        selected = await state.get_selected_writer_frame("test")
        assert selected.presentation["frame"]["sequence"] == int(selected.frame[0, 0, 0]) == 1
        assert selected.frame_ts == 100
    asyncio.run(scenario())


def test_real_publish_operator_keeps_pixels_and_reference_together_through_switch_and_freeze(monkeypatch):
    async def scenario():
        clock = [100.0]
        state = TransmissionRuntimeState(monotonic=lambda: clock[0], wall_time=lambda: clock[0])
        monkeypatch.setattr(operators, "_GLOBAL_RUNTIME_BINDINGS", operators.StreamingRuntimeBindings(state))
        publisher = operators.PublishVideoRuntime({"transmission_id": "synthetic"})
        context = SimpleNamespace(pipeline_name="test", node_id="publish")
        first = sample()
        await publisher.process_packet(first, context)
        selected = await state.get_selected_writer_frame("synthetic")
        assert selected.presentation["frame"]["sequence"] == int(selected.frame[0, 0, 0]) == 1
        selected.presentation["frame"]["sequence"] = 999
        assert (await state.get_selected_writer_frame("synthetic")).presentation["frame"]["sequence"] == 1
        clock[0] += 1
        await publisher.process_packet(sample(sequence=2, source="sub", generation=1), context)
        current = await state.get_selected_writer_frame("synthetic")
        expected = deepcopy(current.presentation)
        assert expected["frame"]["source_id"] == "sub"
        assert expected["frame"]["generation"] == 1
        assert expected["frame"]["sequence"] == int(current.frame[0, 0, 0]) == 2
        await state.close_writer(transmission_id="synthetic", writer_id="test:publish")
        frozen = await state.get_selected_writer_frame("synthetic")
        assert frozen.fallback_active and frozen.presentation == expected
        clock[0] += 10
        expired = await state.get_selected_writer_frame("synthetic")
        assert expired.stale and expired.placeholder_active
        assert expired.presentation == expected  # Historical provenance is never renewed.
        await state.update_writer_frame(transmission_id="synthetic", writer_id="other", lifecycle_state=Lifecycle.OPEN,
            writer_priority=0, frame=np.zeros((20, 40, 3), dtype=np.uint8), frame_ts=111)
        unknown = await state.get_selected_writer_frame("synthetic")
        assert unknown.presentation is None
    asyncio.run(scenario())
