from __future__ import annotations

import hashlib
import time

import numpy as np

from toposync.runtime.pipelines.preview import reference_capture_evidence, reference_preparation_revision


class VisualRegionUnavailable(ValueError):
    pass


def visual_preparation_revision(configuration, pipeline):
    revision = reference_preparation_revision(pipeline)
    region = configuration.canonical_region
    if region is None:
        return revision
    source = next(node["config"] for node in pipeline.graph["nodes"] if node["operator"] == "camera.source")
    if source.get("camera_id") != region.camera_id or source.get("source_id") != region.source_id:
        raise ValueError("canonical region requires its bound camera source")
    return hashlib.sha256(f"{revision}:{region.geometry_key()}".encode()).hexdigest()


async def prepare_visual_frame(configuration, pipeline, packet, services, *, clock=time.monotonic, unix_clock=time.time):
    """Capture and inference consume these same pixels; no affine provenance is fabricated for a panorama."""
    revision = visual_preparation_revision(configuration, pipeline)
    if configuration.preparation_revision is not None and configuration.preparation_revision != revision:
        raise ValueError("visual sensor preparation changed; capture compatible references")

    def evidence():
        value = reference_capture_evidence(packet, pipeline,
            maximum_age_ms=configuration.temporal.maximum_frame_age_ms,
            input_artifact_name=configuration.input_artifact_name, now_monotonic=clock(), now_unix=unix_clock())
        if configuration.source_identity is not None and value["source_identity"] != configuration.source_identity:
            raise ValueError("visual sensor source changed")
        region = configuration.canonical_region
        if region is not None and value["source_identity"] != f"camera:{region.camera_id}:{region.source_id}":
            raise ValueError("canonical region source does not match the captured image")
        return value

    current = evidence()
    artifact = packet.artifacts[configuration.input_artifact_name]
    image = artifact.data
    region = configuration.canonical_region
    if region is None and not current["prepared_region"] and not configuration.use_full_image:
        raise ValueError("visual sensor requires a region or explicit full image")
    if region is not None:
        result = await services.call("cameras.visual_region.prepare", config=region.model_dump(mode="json"),
            image=image, evidence=packet.payload["capture_evidence"], geometry=artifact.metadata["image_geometry"])
        if (result.get("status") != "ready" or result.get("preparation_revision") != region.geometry_key()
                or result.get("capture_evidence") != packet.payload["capture_evidence"]
                or result.get("reference") != region.reference.model_dump()):
            raise VisualRegionUnavailable("region_unavailable")
        image = result.get("_image")
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                or image.shape[2] != 3 or min(image.shape[:2]) < 1 or max(image.shape[:2]) > region.maximum_side):
            raise VisualRegionUnavailable("region_unavailable")
        current = evidence()
        current.update(preparation_revision=revision, prepared_region=True)
    return image, current
