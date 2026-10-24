"""Shared, observational region evaluation for pipelines and editor previews."""
from __future__ import annotations

import asyncio
import base64
import uuid
import time
from collections import OrderedDict
from copy import deepcopy

import cv2
import numpy as np
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines.image_geometry import image_geometry, source_pixels
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness
from toposync.runtime.pipelines.runtime import Packet

from .panorama import _error
from .processing.planar_localization import PlanarLocalizer
from .processing.visual_region import canonical_region, evaluate_detection, project_region, rasterize_region
from .visual_region_models import CanonicalRegionConfig, VisualRegionConfig, RegionReference


class AreaPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    composition_id: str = Field(min_length=1, max_length=128)
    area_ids: list[str] = Field(min_length=1, max_length=64)


async def _compute(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    # Repeated cancellation must not release the slot while native computation is still running.
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class VisualRegionService:
    def __init__(self, panorama, sources):
        self.panorama, self.sources = panorama, sources
        self.planar = OrderedDict()
        self.masks = OrderedDict()
        self.canonical = OrderedDict()
        self.recent_decisions = OrderedDict()
        self.lock = asyncio.Lock()
        self.work = asyncio.Semaphore(2)
        self.framing_requests = {}

    async def framing_profile(self, camera_id, source_id, binding):
        store = getattr(self.panorama.app.state, "ptz_attention_store", None)
        if store is None:
            return None
        candidates = []
        for profile in store.list_profiles():
            if (profile.camera_id != camera_id or profile.source_id != source_id
                    or profile.mode != "live_panorama" or profile.operator_managed or profile.follow_view_updates
                    or profile.stale_timeout_seconds > 15
                    or not profile.same_head_observer_acknowledged
                    or not any(item.enabled and item.event_type == "visual_region" for item in profile.event_policies)):
                continue
            active = await self.panorama.get_active(camera_id=camera_id, source_id=source_id,
                composition_id=profile.composition_id, element_id=profile.panorama_element_id)
            job = self.panorama.jobs.get((active.get("job") or {}).get("id"))
            reference = (job or {}).get("source_panorama") or {}
            if (job and self.panorama._attention_qualified(job)
                    and reference.get("id") == binding["id"]
                    and reference.get("revision") == binding["revision"]
                    and reference.get("geometry", {}).get("model_digest") == binding["geometry"]["model_digest"]):
                candidates.append((profile, job))
        return candidates[0] if len(candidates) == 1 else None

    async def framing(self, *, config, owner, pipeline_name, node_id, close=False):
        previous = self.framing_requests.get(owner)
        if close:
            if previous:
                await self.panorama.services.call("ptz_attention.intent.submit", **{
                    **previous, "lifecycle": "close", "target": None, "event_at": time.time(), "received_at": time.time()})
                self.framing_requests.pop(owner, None)
            return {"state": "released"}
        parsed = VisualRegionConfig.model_validate(config)
        parsed.require_region()
        binding, paths = await self.reference(parsed.camera_id, parsed.source_id, parsed.reference)
        available = await self.framing_profile(parsed.camera_id, parsed.source_id, binding)
        if available is None or not parsed.native_tracking_disabled_confirmed:
            if previous:
                await self.framing(config=config, owner=owner, pipeline_name=pipeline_name, node_id=node_id, close=True)
            return {"state": "unavailable", "reason": "visual_region_framing_unqualified"}
        if len(self.framing_requests) >= 64 and previous is None:
            return {"state": "unavailable", "reason": "visual_region_framing_capacity"}
        from .processing.panorama_mapping import panorama_pixel_to_ray, map_ray_to_world
        profile, job = available
        mask = await _compute(rasterize_region, parsed.strokes, binding["width"], binding["height"], wrap=True)
        coverage = cv2.imread(str(paths["coverage"]), cv2.IMREAD_GRAYSCALE)
        if coverage is None or np.any((mask > 0) & (coverage == 0)):
            return {"state": "unavailable", "reason": "visual_region_outside_reference_coverage"}
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boundary = np.concatenate(contours).reshape(-1, 2)
        x, y = boundary[:, 0], boundary[:, 1]
        angles = (x / (binding["width"] - 1) - .5) * 2 * np.pi
        u = (np.arctan2(np.sin(angles).mean(), np.cos(angles).mean()) / (2*np.pi) + .5) % 1
        v = float((y.min() + y.max()) / 2 / (binding["height"] - 1))
        center = np.asarray(panorama_pixel_to_ray(u, v))
        from .processing.panorama_mapping import _lens_arrays
        lens = self.sources._read(paths["model"])["lens"]
        matrix, distortion, width, height = _lens_arrays(lens)
        right = np.cross([0, 0, 1], center)
        if np.linalg.norm(right) < .01:
            return {"state": "unavailable", "reason": "visual_region_framing_target_unavailable"}
        right /= np.linalg.norm(right)
        down = -np.cross(center, right)
        rays = np.array([panorama_pixel_to_ray(float(px / (binding["width"]-1)), float(py / (binding["height"]-1))) for px, py in boundary])
        local = rays @ np.column_stack((right, down, center))
        pixels = cv2.projectPoints(local, np.zeros(3), np.zeros(3), matrix, distortion)[0].reshape(-1, 2)
        if (np.any(local[:, 2] <= 0) or np.any(pixels < [.05*width, .05*height])
                or np.any(pixels > [.95*width, .95*height])):
            return {"state": "unavailable", "reason": "visual_region_cannot_fit_frame"}
        world = map_ray_to_world(job["solution"], panorama_pixel_to_ray(u, v))
        if world is None:
            return {"state": "unavailable", "reason": "visual_region_framing_target_unavailable"}
        intent = dict(key=owner, event_id=owner, profile_id=profile.id, ptz_device_id=profile.ptz_device_id,
            event_type="visual_region", pipeline_name=pipeline_name, node_id=node_id,
            lifecycle="update" if previous else "open", target={"world_anchor": {"x": world[0], "z": world[1]}},
            hold_after_close_seconds=0, priority=0, event_at=time.time(), received_at=time.time())
        await self.panorama.services.call("ptz_attention.intent.submit", **intent)
        self.framing_requests[owner] = intent
        status = await self.panorama.services.call("ptz_attention.status.snapshot")
        return {"state": "requested", "devices": [item for item in status.get("devices", []) if item.get("ptz_device_id") == profile.ptz_device_id]}

    async def reference(self, camera_id, source_id, reference=None, request=None):
        if request is not None:
            self.sources._authorize(request, camera_id)
        _, camera, source = await self.sources._context(camera_id, source_id, request)
        pointer = self.sources._pointer(source).get("active") or {}
        identifier = reference.artifact_id if reference else pointer.get("artifact_id")
        revision = reference.revision if reference else pointer.get("revision")
        binding, paths = self.panorama._source_artifact(camera, source, identifier, revision, require_active=reference is None)
        if reference and reference.geometry_digest != binding["geometry"]["model_digest"]:
            raise _error("source_panorama_changed", "A geometria da referência mudou.")
        return binding, paths

    async def localizer(self, camera_id, source_id, binding, paths):
        if binding["geometry"]["projection"] == "image_plane":
            key = (camera_id, source_id, binding["id"], binding["geometry"]["model_digest"])
            async with self.lock:
                localizer = self.planar.get(key)
                if localizer is None:
                    def load():
                        artifact = self.sources._artifact(binding["id"])
                        directory = self.sources._private_path(self.sources.root / "jobs", artifact["_job_id"])
                        job = self.sources._read(directory / "job.json") or {}
                        if job.get("camera_id") != camera_id or job.get("source_id") != source_id:
                            raise ValueError("visual_region_reference_source_changed")
                        model = self.sources._read(paths["model"])
                        photo = cv2.imread(str(directory / "source.png"))
                        return PlanarLocalizer(photo, model["source_size"])
                    localizer = await _compute(load)
                    self.planar[key] = localizer
                    while len(self.planar) > 2:
                        self.planar.popitem(last=False)
                self.planar.move_to_end(key)
        else:
            localizer = await self.panorama.reference_localizer(camera_id, source_id, binding)
        return localizer

    async def localize(self, camera_id, source_id, binding, paths, image, evidence, geometry):
        localizer = await self.localizer(camera_id, source_id, binding, paths)
        return await _compute(localizer.locate, image, evidence, geometry)

    async def prepare(self, *, config, image, evidence, geometry):
        parsed = CanonicalRegionConfig.model_validate(config)
        parsed.require_region()
        revision = parsed.geometry_key()
        unavailable = {"status": "unavailable", "preparation_revision": revision}
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                or image.shape[2] != 3 or not 1 <= image.shape[0] * image.shape[1] <= 4_000_000):
            return {**unavailable, "reason": "visual_region_image_invalid"}
        if not isinstance(geometry, dict) or geometry.get("capture_evidence") != evidence:
            return {**unavailable, "reason": "panorama_source_geometry_changed"}
        image, evidence, geometry = image.copy(), deepcopy(evidence), deepcopy(geometry)

        def fresh():
            age = resolve_frame_freshness(Packet.create(stream_id="visual-region", payload={"capture_evidence": evidence})).age_seconds
            return age is not None and age <= parsed.evidence_timeout_seconds

        if not fresh():
            return {**unavailable, "reason": "visual_region_frame_expired"}
        if self.work.locked():
            return {**unavailable, "reason": "visual_region_busy"}
        async with self.work:
            try:
                binding, paths = await self.reference(parsed.camera_id, parsed.source_id, parsed.reference)
                if revision not in self.canonical:
                    base = await self.localizer(parsed.camera_id, parsed.source_id, binding, paths)

                    def initialize():
                        wrap = binding["geometry"]["projection"] == "equirectangular"
                        mask = rasterize_region(parsed.strokes, binding["width"], binding["height"], wrap=wrap)
                        coverage = cv2.imread(str(paths["coverage"]), cv2.IMREAD_GRAYSCALE)
                        if coverage is None or coverage.shape != mask.shape or np.any((mask > 0) & (coverage == 0)):
                            raise ValueError("visual_region_outside_reference_coverage")
                        context = (rasterize_region(parsed.alignment_strokes, binding["width"], binding["height"], wrap=wrap)
                                   if parsed.alignment_strokes else coverage.copy())
                        radius = parsed.alignment_margin_pixels
                        padded = np.pad(mask, ((0, 0), (radius, radius)), mode="wrap") if wrap else mask
                        excluded = cv2.dilate(padded, np.ones((radius * 2 + 1, radius * 2 + 1), np.uint8))
                        if wrap:
                            excluded = excluded[:, radius:-radius]
                        context[(coverage == 0) | (excluded > 0)] = 0
                        if not context.any():
                            raise ValueError("visual_region_static_context_missing")
                        return mask, coverage, base.with_reference_mask(context)

                    self.canonical[revision] = await _compute(initialize)
                    while len(self.canonical) > 2:
                        self.canonical.popitem(last=False)
                self.canonical.move_to_end(revision)
                mask, coverage, localizer = self.canonical[revision]
                located = await _compute(localizer.locate, image, evidence, geometry)
                if located.get("status") != "localized":
                    return {**unavailable, "reason": located.get("reason", "panorama_visual_localization_failed")}
                output = await _compute(canonical_region, image, mask, coverage, located, geometry,
                                        maximum_side=parsed.maximum_side, wrap=binding["geometry"]["projection"] == "equirectangular")
                await self.reference(parsed.camera_id, parsed.source_id, parsed.reference)
                if not fresh():
                    return {**unavailable, "reason": "visual_region_frame_expired"}
                return {"status": "ready", "reason": "confirmed", "preparation_revision": revision,
                        "capture_evidence": evidence, "reference": parsed.reference.model_dump(), "_image": output}
            except ValueError as error:
                reason = str(error)
                allowed = {"visual_region_image_invalid", "visual_region_dimensions_invalid", "visual_region_pixel_limit",
                           "visual_region_outside_reference_coverage", "visual_region_outside", "visual_region_partial",
                           "visual_region_local_support_missing", "visual_region_localization_unverified", "visual_region_static_context_missing"}
                return {**unavailable, "reason": reason if reason in allowed else "visual_region_preparation_failed"}
            except (HTTPException, OSError, cv2.error, np.linalg.LinAlgError):
                return {**unavailable, "reason": "visual_region_preparation_failed"}

    async def evaluate(self, *, config, image, evidence, geometry, detections=()):
        parsed = VisualRegionConfig.model_validate(config)
        parsed.require_region()
        unknown = {"classification": "indeterminate", "reason": "visual_region_unavailable", "observations": []}
        freshness = resolve_frame_freshness(Packet.create(stream_id="visual-region", payload={"capture_evidence": evidence}))
        if freshness.age_seconds is None or freshness.age_seconds > parsed.evidence_timeout_seconds:
            return {**unknown, "reason": "visual_region_frame_expired"}
        if not isinstance(geometry, dict) or geometry.get("capture_evidence") != evidence:
            return {**unknown, "reason": "panorama_source_geometry_changed"}
        if not isinstance(image, np.ndarray) or image.size > 50_000_000 or image.size == 0:
            return {**unknown, "reason": "visual_region_image_invalid"}
        # Bound both work and waiting callers; a busy frame is unknown, never stale success.
        if self.work.locked():
            return {**unknown, "reason": "visual_region_busy"}
        async with self.work:
            binding, paths = await self.reference(parsed.camera_id, parsed.source_id, parsed.reference)
            located = await self.localize(parsed.camera_id, parsed.source_id, binding, paths, image, evidence, geometry)
            if located.get("status") != "localized":
                return {**unknown, "reason": located.get("reason", "panorama_visual_localization_failed")}
            key = parsed.geometry_key()
            if key not in self.masks:
                mask = await _compute(rasterize_region, parsed.strokes, binding["width"], binding["height"],
                                               wrap=binding["geometry"]["projection"] == "equirectangular")
                coverage = await _compute(cv2.imread, str(paths["coverage"]), cv2.IMREAD_GRAYSCALE)
                self.masks[key] = (mask, coverage)
                while len(self.masks) > 4:
                    self.masks.popitem(last=False)
            self.masks.move_to_end(key)
            mask, coverage = self.masks[key]
            def process():
                projected, valid, visibility = project_region(mask, coverage, located, geometry)
                observations = [evaluate_detection(parsed, item, projected, valid, located, geometry, evidence)
                                for item in detections]
                outlines = []
                for item, decision in zip(detections, observations):
                    try:
                        box = np.asarray(item.get("bbox01") if isinstance(item, dict) else None, dtype=float)
                    except (TypeError, ValueError):
                        box = np.empty(0)
                    if box.shape != (4,) or not np.isfinite(box).all():
                        outlines.append(None)
                        continue
                    left, top, right, bottom = box
                    points = source_pixels(np.array([[left, top], [right, top], [right, bottom], [left, bottom]])
                                           * (np.asarray(geometry["source_size"]) - 1), np.linalg.inv(geometry["to_source"]))
                    outlines.append({"points": points, "classification": decision["classification"]})
                return {"classification": "observed", "reason": "confirmed", "visibility": visibility,
                        "observations": observations, "capture_evidence": evidence,
                        "reference": parsed.reference.model_dump(), "_mask": projected, "_valid": valid, "_outlines": outlines}
            result = await _compute(process)
            current = resolve_frame_freshness(Packet.create(stream_id="visual-region", payload={"capture_evidence": evidence}))
            if current.age_seconds is None or current.age_seconds > parsed.evidence_timeout_seconds:
                return {**unknown, "reason": "visual_region_frame_expired"}
            await self.reference(parsed.camera_id, parsed.source_id, parsed.reference)
            if detections:
                self.recent_decisions[key] = (image.copy(), dict(evidence), result, [
                    dict(decision) for decision in result["observations"]])
                self.recent_decisions.move_to_end(key)
                while len(self.recent_decisions) > 2:
                    self.recent_decisions.popitem(last=False)
            return result

    async def preview(self, request, config):
        await self.reference(config.camera_id, config.source_id, config.reference, request)
        recent = self.recent_decisions.get(config.geometry_key())
        if recent is not None:
            image, evidence, result, detections = recent
            freshness = resolve_frame_freshness(Packet.create(stream_id="visual-region-preview", payload={"capture_evidence": evidence}))
            if freshness.age_seconds is not None and freshness.age_seconds <= config.evidence_timeout_seconds:
                return {**self.preview_result(image, evidence, dict(result)), "detection_available": True,
                        "observations": detections, "frame_age_seconds": freshness.age_seconds}
            self.recent_decisions.pop(config.geometry_key(), None)
        frame = await self.sources.capture_frame(config.camera_id, config.source_id, f"region-preview-{uuid.uuid4().hex}")
        image, evidence = frame["image"], frame["capture_evidence"]
        geometry = image_geometry(image.shape[1], image.shape[0], evidence)
        result = await self.evaluate(config=config.model_dump(), image=image, evidence=evidence, geometry=geometry)
        return self.preview_result(image, evidence, result)

    @staticmethod
    def preview_result(image, evidence, result):
        mask = result.pop("_mask", None)
        result.pop("_valid", None)
        outlines = result.pop("_outlines", [])
        display = image.copy()
        if mask is not None:
            selected = mask > 0
            display[selected] = (display[selected] * .55 + np.array([115, 155, 0]) * .45).astype(np.uint8)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(display, contours, -1, (145, 205, 0), 2)
        for index, outline in enumerate(outlines):
            if outline is None:
                continue
            points = np.rint(np.clip(outline["points"], [-display.shape[1], -display.shape[0]], [2*display.shape[1], 2*display.shape[0]])).astype(np.int32)
            color = {"inside": (130, 205, 0), "outside": (60, 60, 220), "indeterminate": (180, 180, 180)}[outline["classification"]]
            cv2.polylines(display, [points], True, color, 2)
            anchor = (max(0, int(points[0, 0])), max(20, int(points[0, 1])))
            cv2.putText(display, str(index + 1), anchor, cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 4)
            cv2.putText(display, str(index + 1), anchor, cv2.FONT_HERSHEY_SIMPLEX, .7, (255, 255, 255), 1)
        scale = min(1, 1280 / display.shape[1])
        if scale < 1:
            display = cv2.resize(display, None, fx=scale, fy=scale)
        ok, encoded = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise ValueError("visual_region_preview_encoding_failed")
        return {**result, "image": "data:image/jpeg;base64," + base64.b64encode(encoded).decode(),
                "capture_evidence": evidence, "detection_available": False}


    async def preview_areas(self, request, body):
        from .pipelines.postprocess import resolve_composition_area
        from .processing.panorama_mapping import map_image_plane_point, map_world_to_ray, mapping_extrapolated
        self.sources._authorize(request, body.camera_id)
        active = await self.panorama.get_active(camera_id=body.camera_id, source_id=body.source_id, composition_id=body.composition_id)
        if not active.get("job"):
            raise _error(active.get("reason", "active_mapping_required"), "O mapeamento desta área não está disponível.")
        job = self.panorama.jobs[active["job"]["id"]]
        await self.panorama._current(job, request)
        binding = job.get("source_panorama")
        if not binding:
            raise _error("source_panorama_unavailable", "Esta área precisa de um mapeamento com panorâmica.")
        reference = RegionReference(artifact_id=binding["id"], revision=binding["revision"], geometry_digest=binding["geometry"]["model_digest"])
        binding, paths = await self.reference(body.camera_id, body.source_id, reference, request)
        configuration = await self.sources.store.get_config()
        mask = np.zeros((binding["height"], binding["width"]), np.uint8)
        labels = []
        for identifier in body.area_ids:
            area = resolve_composition_area(configuration.compositions, composition_id=body.composition_id, area_id=identifier)
            if area is None:
                raise _error("camera_area_unavailable", "Uma área selecionada foi removida ou mudou.")
            labels.append(area.name)
            vertices = []
            for index, point in enumerate(area.points):
                following = area.points[(index + 1) % len(area.points)]
                for fraction in np.linspace(0, 1, 24, endpoint=False):
                    x, z = point.x + (following.x - point.x) * fraction, point.z + (following.z - point.z) * fraction
                    if mapping_extrapolated(job["solution"], x, z):
                        raise _error("outside_validated_region", "A área ultrapassa o trecho de mapeamento validado.")
                    if binding["geometry"]["projection"] == "image_plane":
                        uv = map_image_plane_point(job["solution"], x, z)
                        if uv is None:
                            raise ValueError("Area projection unavailable")
                    else:
                        ray = map_world_to_ray(job["solution"], x, z)
                        if ray is None:
                            raise ValueError("Area projection unavailable")
                        uv = (np.arctan2(ray[1], ray[0]) / (2 * np.pi) + .5,
                              .5 - np.arctan2(ray[2], np.hypot(ray[0], ray[1])) / np.pi)
                    vertices.append(uv)
            polygon = np.asarray(vertices) * [binding["width"]-1, binding["height"]-1]
            if binding["geometry"]["projection"] == "equirectangular":
                polygon[:, 0] = np.unwrap(polygon[:, 0] / binding["width"] * 2 * np.pi) / (2*np.pi) * binding["width"]
                for offset in (-binding["width"], 0, binding["width"]):
                    cv2.fillPoly(mask, [np.rint(polygon + [offset, 0]).astype(np.int32)], 255)
            else:
                cv2.fillPoly(mask, [np.rint(polygon).astype(np.int32)], 255)
        if not mask.any():
            raise ValueError("visual_region_empty")
        frame = await self.sources.capture_frame(body.camera_id, body.source_id, f"area-preview-{uuid.uuid4().hex}")
        image, evidence = frame["image"], frame["capture_evidence"]
        geometry = image_geometry(image.shape[1], image.shape[0], evidence)
        located = await self.localize(body.camera_id, body.source_id, binding, paths, image, evidence, geometry)
        result = {"classification": "indeterminate", "reason": located.get("reason", "panorama_visual_localization_failed")}
        if located.get("status") == "localized":
            coverage = cv2.imread(str(paths["coverage"]), cv2.IMREAD_GRAYSCALE)
            projected, valid, visibility = await _compute(project_region, mask, coverage, located, geometry)
            result = {"classification": "observed", "reason": "confirmed", "visibility": visibility, "_mask": projected}
        freshness = resolve_frame_freshness(Packet.create(stream_id="area-preview", payload={"capture_evidence": evidence}))
        if freshness.age_seconds is None or freshness.age_seconds > 5:
            result = {"classification": "indeterminate", "reason": "visual_region_frame_expired"}
        await self.panorama._current(job, request)
        return {**self.preview_result(image, evidence, result), "areas": labels, "mapping_revision": job["revision"]}


def register_visual_region_routes(app, panorama, sources, services):
    service = VisualRegionService(panorama, sources)
    services.register("cameras.visual_region.evaluate", service.evaluate)
    services.register("cameras.visual_region.prepare", service.prepare)
    services.register("cameras.visual_region.framing", service.framing)
    app.state.camera_visual_regions = service

    @app.get("/api/cameras/cameras/{camera_id}/sources/{source_id}/visual-region-reference")
    async def reference(request: Request, camera_id: str, source_id: str):
        binding, _ = await service.reference(camera_id, source_id, request=request)
        can_control = False
        try:
            sources._authorize(request, camera_id, control=True)
            can_control = True
        except HTTPException:
            pass
        return {**binding, "framing_available": can_control and await service.framing_profile(camera_id, source_id, binding) is not None}

    @app.post("/api/cameras/visual-regions/preview")
    async def preview(request: Request, body: VisualRegionConfig):
        try:
            async with asyncio.timeout(20):
                return await service.preview(request, body)
        except TimeoutError:
            raise _error("visual_region_preview_timeout", "A prévia demorou demais. Tente novamente.", 503)
        except ValueError as error:
            raise _error("visual_region_invalid", str(error), 422)
    @app.post("/api/cameras/areas/preview")
    async def preview_areas(request: Request, body: AreaPreview):
        try:
            async with asyncio.timeout(20):
                if service.work.locked():
                    raise _error("visual_region_busy", "Há outra conferência em andamento.", 503)
                async with service.work:
                    return await service.preview_areas(request, body)
        except TimeoutError:
            raise _error("visual_region_preview_timeout", "A prévia demorou demais. Tente novamente.", 503)
        except ValueError as error:
            raise _error("visual_region_invalid", str(error), 422)
    return service
