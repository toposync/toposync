"""HTTP pairing over the actual video publisher with synthetic pixels only."""

import json
from types import SimpleNamespace

import cv2
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
import numpy as np
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.inspection import PipelineInspectionStore
from toposync.runtime.pipelines.inspection_api import install_inspection_routes
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync_ext_streaming.api import inspection_video
from toposync_ext_streaming.pipelines import operators
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState
from test_streaming_frame_presentation import sample


@pytest.fixture
def preview(monkeypatch):
    app = FastAPI()
    now, allowed = [100.0], [True]
    store = PipelineInspectionStore(clock=lambda: now[0])
    runtime = TransmissionRuntimeState(monotonic=lambda: now[0], wall_time=lambda: now[0])
    pipeline = Pipeline(name="demo", graph={"schema_version": 2, "uid": "demo", "nodes": [
        {"id": "publish", "uid": "publish", "operator": "stream.publish_video", "config": {"transmission_id": "test"}},
        {"id": "detect", "uid": "detect", "operator": "vision.detect"}], "edges": []})

    async def get_pipeline(name):
        return pipeline

    def authorize(request):
        if not allowed[0]:
            raise HTTPException(403, "denied")
        return "owner"

    app.state.pipeline_inspection = store
    app.state.streaming_runtime_state = runtime
    app.state.config_store = SimpleNamespace(get_pipeline=get_pipeline)
    install_inspection_routes(app, authorize)
    router = APIRouter(prefix="/api/streams")
    inspection_video.install_inspection_video_routes(router)
    app.include_router(router)
    monkeypatch.setattr(operators, "_GLOBAL_RUNTIME_BINDINGS", operators.StreamingRuntimeBindings(runtime))
    with TestClient(app) as client:
        token = client.post("/api/pipelines/demo/inspection").json()["session"]
        publisher = operators.PublishVideoRuntime({"transmission_id": "test"})

        async def publish(sequence=1, generation=0, source="main"):
            packet = sample(sequence, source, generation)
            packet.payload["capture_evidence"]["published_at"] = now[0]
            for node in ("publish", "detect"):
                store.observe(pipeline="demo", node=node, physical=node,
                    operator="stream.publish_video" if node == "publish" else "vision.detect",
                    producer_epoch="runtime", revision=store.sessions[token].revision,
                    state="emitted" if node == "publish" else "analyzed", packet=packet,
                    frame=packet.artifacts["main"].data if node == "detect" else None,
                    geometry=packet.artifacts["main"].metadata["image_geometry"],
                    detections=[{"bbox01": [0.1, 0.2, 0.8, 0.9], "score": 0.8, "label": "square"}])
            await publisher.process_packet(packet, SimpleNamespace(pipeline_name="demo", node_id="publish"))

        yield SimpleNamespace(client=client, now=now, allowed=allowed, store=store, runtime=runtime,
            token=token, publish=lambda **kwargs: client.portal.call(publish, **kwargs),
            url=f"/api/streams/inspection/{token}/publish/frame", publish_async=publish)


def decode(response):
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    length = int.from_bytes(response.content[:4], "big")
    metadata = json.loads(response.content[4:4 + length])
    pixels = cv2.imdecode(np.frombuffer(response.content[4 + length:], dtype=np.uint8), cv2.IMREAD_COLOR)
    return metadata, pixels


def test_transport_atomically_pairs_published_pixels_and_metadata_without_starting_a_producer(preview):
    assert preview.client.get(preview.url).status_code == 409
    preview.now[0] += 0.21
    preview.publish()
    metadata, pixels = decode(preview.client.get(preview.url))
    reference = metadata["presentation"]["frame"]
    assert reference["sequence"] == round(float(pixels.mean())) == 1
    assert reference["source_id"] == "main" and reference["capture_instance"] == "synthetic"
    detection = next(node for node in metadata["snapshot"]["nodes"] if node["node_id"] == "detect")
    assert detection["frame"] == reference
    assert detection["geometry"]["source_projection"] == "declared"
    assert detection["detections"][0]["label"] == "square"
    assert "never-export" not in json.dumps(metadata)
    assert metadata["transport"] == "paired_jpeg"
    assert len(preview.client.get(preview.url).content) < inspection_video.MAXIMUM_PACKET_BYTES
    assert preview.client.get(preview.url).status_code == 429


@pytest.mark.parametrize("change", ["permission", "close", "generation", "expiry"])
def test_pending_encoding_is_rechecked_before_private_pixels_are_delivered(preview, monkeypatch, change):
    preview.publish()
    original = inspection_video._encode_image_bytes

    def encode(frame, **kwargs):
        if change == "permission":
            preview.allowed[0] = False
        elif change == "close":
            preview.client.portal.call(preview.store.close, preview.token)
        elif change == "generation":
            preview.client.portal.call(lambda: preview.publish_async(generation=1))
        else:
            preview.now[0] += 2
        return original(frame, **kwargs)

    monkeypatch.setattr(inspection_video, "_encode_image_bytes", encode)
    result = preview.client.get(preview.url)
    assert result.status_code in {403, 404, 409, 410}
    assert result.headers["content-type"] == "application/json"


def test_newer_frame_during_encoding_does_not_relabel_the_encoded_pixels(preview, monkeypatch):
    preview.publish()
    original = inspection_video._encode_image_bytes

    def encode(frame, **kwargs):
        preview.client.portal.call(lambda: preview.publish_async(sequence=2))
        return original(frame, **kwargs)

    monkeypatch.setattr(inspection_video, "_encode_image_bytes", encode)
    metadata, pixels = decode(preview.client.get(preview.url))
    assert metadata["presentation"]["frame"]["sequence"] == round(float(pixels.mean())) == 1
    assert preview.store.snapshot(preview.token).nodes[0].frame["sequence"] == 2


def test_expired_or_foreign_publication_has_no_transport_fallback(preview):
    preview.publish()
    assert preview.client.get(preview.url.replace("publish/frame", "detect/frame")).status_code == 404
    preview.now[0] += 1.1
    assert preview.client.get(preview.url).status_code == 409
    assert preview.client.delete(f"/api/pipeline-inspection/{preview.token}").status_code == 204
    assert preview.client.get(preview.url).status_code == 404


def test_size_limit_fails_before_response_and_releases_encoder(preview, monkeypatch):
    preview.publish()
    monkeypatch.setattr(inspection_video, "_encode_image_bytes", lambda *args, **kwargs:
        (b"x" * inspection_video.MAXIMUM_PACKET_BYTES, ".jpg", "image/jpeg"))
    assert preview.client.get(preview.url).status_code == 413
    assert preview.store.sessions[preview.token].preview_active is False
    assert not preview.client.app.state.pipeline_image_encoders.locked()


@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080), (640, 360), (1000, 333)])
def test_preview_resizing_preserves_pixel_centre_geometry_and_bounded_dimensions(width, height):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    capture = {"capture_instance": "synthetic", "generation": 0, "sequence": 1}
    geometry = image_geometry(width, height, capture)
    geometry["valid_domains"] = []
    body, result = inspection_video._encode_preview(frame, {"frame": capture,
        "image_size": [width, height], "geometry": geometry})
    decoded = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    out_height, out_width = decoded.shape[:2]
    assert out_width <= 640 and out_height <= 360
    assert result["image_size"] == [out_width, out_height]
    from toposync_ext_streaming.streaming.resize import contain_content_rect
    rect = contain_content_rect(width, height, out_width, out_height)
    sx, sy = rect["width"] * out_width / width, rect["height"] * out_height / height
    point = np.asarray(result["geometry"]["to_source"]) @ np.array([
        rect["x"] * out_width, rect["y"] * out_height, 1])
    np.testing.assert_allclose(point, [(1 / sx - 1) / 2, (1 / sy - 1) / 2, 1], atol=1e-9)
