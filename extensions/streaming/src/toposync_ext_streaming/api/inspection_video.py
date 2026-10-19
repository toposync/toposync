"""Demand-free, bounded presentation of already published frames for the player."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, Response

from toposync.runtime.pipelines.image_geometry import inspection_geometry, transformed_geometry
from toposync.runtime.pipelines.inspection_api import checked_inspection, pipeline_image_encoders
from toposync.runtime.pipelines.operators_sinks import _encode_image_bytes
from toposync.runtime.pipelines.runtime import Artifact

from ..streaming.resize import contain_content_rect, resize_frame_contain
from ..streaming.runtime_state import TransmissionRuntimeState


MAXIMUM_PACKET_BYTES = 400_000
PREVIEW_INTERVAL_SECONDS = 0.2


def _encode_preview(frame, presentation):
    height, width = frame.shape[:2]
    scale = min(1.0, 640 / width, 360 / height)
    target_width, target_height = max(1, round(width * scale)), max(1, round(height * scale))
    rect = contain_content_rect(width, height, target_width, target_height)
    sx, sy = rect["width"] * target_width / width, rect["height"] * target_height / height
    # OpenCV resize maps pixel centres, including the half-pixel translation.
    forward = [[sx, 0, rect["x"] * target_width + (sx - 1) / 2],
               [0, sy, rect["y"] * target_height + (sy - 1) / 2], [0, 0, 1]]
    geometry = None
    if presentation["geometry"] is not None:
        artifact = Artifact("preview", frame, metadata={"image_geometry": {
            **presentation["geometry"], "capture_evidence": presentation["frame"]}})
        transformed = transformed_geometry(artifact, forward, target_width, target_height)
        geometry = inspection_geometry(transformed.get("image_geometry"), target_width, target_height, presentation["frame"])
    resized = resize_frame_contain(frame, target_width, target_height)
    body, _, _ = _encode_image_bytes(resized, fmt="jpg", jpeg_quality=80)
    return body, {**presentation, "image_size": [target_width, target_height], "geometry": geometry}


def install_inspection_video_routes(router: APIRouter) -> None:
    @router.get("/inspection/{token}/{node_id}/frame")
    async def inspection_video_frame(request: Request, token: str, node_id: str) -> Response:
        store, pipeline, _, _ = await checked_inspection(request, token)
        definition = next((item for item in pipeline.graph.get("nodes", []) if item["id"] == node_id), None)
        if definition is None or definition.get("operator") != "stream.publish_video":
            raise HTTPException(404, "inspection_publication_unavailable")
        transmission = definition.get("config", {}).get("transmission_id")
        runtime = getattr(request.app.state, "streaming_runtime_state", None)
        if not transmission or not isinstance(runtime, TransmissionRuntimeState):
            raise HTTPException(409, "inspection_publication_unavailable")
        try:
            snapshot = store.snapshot(token)
        except KeyError as error:
            raise HTTPException(410, "inspection_session_expired") from error
        session = store.sessions[token]
        now = store.clock()
        if session.preview_active or now < session.preview_not_before:
            raise HTTPException(429, "inspection_preview_limited", headers={"Retry-After": "1"})
        session.preview_active = True
        session.preview_not_before = now + PREVIEW_INTERVAL_SECONDS
        try:
            selected = await runtime.get_selected_writer_frame(transmission)
            published = next(item for item in snapshot.nodes if item.node_id == node_id)
            presentation = selected.presentation
            if (selected.frame is None or selected.stale or selected.fallback_active
                or presentation is None or published.frame != presentation["frame"]
                or published.expires_at is None or published.expires_at <= now
                or selected.selected_writer_id is None
                or not selected.selected_writer_id.endswith(":" + published.physical_node_id)):
                raise HTTPException(409, "inspection_frame_not_paired")
            frame = selected.frame
            if (frame.nbytes > store.maximum_image_bytes
                or presentation["image_size"] != [frame.shape[1], frame.shape[0]]):
                raise HTTPException(413, "inspection_frame_capacity")
            encoders = pipeline_image_encoders(request.app)
            if encoders.locked():
                raise HTTPException(429, "inspection_encoder_busy")
            async with encoders:
                encoding = asyncio.create_task(asyncio.to_thread(_encode_preview, frame, presentation))
                try:
                    body, presentation = await asyncio.shield(encoding)
                except asyncio.CancelledError:
                    await encoding
                    raise
            await checked_inspection(request, token)
            store.expire()
            if store.sessions.get(token) is not session or store.clock() >= published.expires_at:
                raise HTTPException(410, "inspection_frame_expired")
            current = session.nodes.get(node_id)
            reference = presentation["frame"]
            if current is None or current.frame is None or any(
                current.frame[key] != reference[key]
                for key in ("source_id", "device_id", "capture_instance", "generation")
            ):
                raise HTTPException(409, "inspection_capture_changed")
            # The next frame may already exist. This response retains its own sequence and expiry.
            metadata = json.dumps({
                "schema_version": 1, "transport": "paired_jpeg", "presentation": presentation,
                "snapshot": snapshot.model_dump(mode="json"), "expires_at": published.expires_at,
            }, separators=(",", ":"), allow_nan=False).encode()
            if len(metadata) > store.maximum_bytes:
                raise HTTPException(413, "inspection_frame_capacity")
            payload = len(metadata).to_bytes(4, "big") + metadata + body
            if len(payload) > MAXIMUM_PACKET_BYTES:
                raise HTTPException(413, "inspection_frame_capacity")
            return Response(payload, media_type="application/vnd.toposync.paired-frame",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        finally:
            session.preview_active = False
