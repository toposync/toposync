"""Persisted, fixed-zoom panoramic calibration owned by the cameras extension.

Geometry has no device effects. Only capture, check and aim acquire a fenced PTZ
lease; interrupted processes never resume physical movement after a restart.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import math
import re
import shutil
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from PIL import Image

from .processing.panorama_mapping import (
    _rotation_basis,
    estimate_panorama_mapping,
    estimate_image_plane_mapping,
    image_pixel_to_ray,
    map_world_to_ray,
    map_ray_to_world,
    mapping_extrapolated,
    panorama_pixel_to_ray,
    preview_panorama_mapping,
    ray_to_pan_tilt,
    render_panorama,
)
from .panorama_reference import PanoramaReferenceCoordinator
from .settings import camera_source_has_ptz, get_camera_device, get_camera_source, iter_camera_sources
from .source_panorama import (
    SourcePanoramaService, _identity as source_panorama_identity,
    _geometry_identity as source_panorama_geometry_identity, _artifact_identity_matches,
)

_PREFIX = "/api/cameras/cameras/{camera_id}/panorama"
_MAX_STORAGE = 512 * 1024 * 1024
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
_MAX_CAPTURES = 48
_MAX_DECODED_PIXELS = 64_000_000
_IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
_IMAGE_NAME = re.compile(r"^(?:capture-\d{3}|check-[a-f0-9]{32}|panorama|coverage)\.(?:jpg|png)$")


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Lens(_Input):
    width: int = Field(ge=2, le=4096)
    height: int = Field(ge=2, le=2160)
    fx: float = Field(gt=0)
    fy: float = Field(gt=0)
    cx: float = Field(ge=0)
    cy: float = Field(ge=0)
    distortion: list[float] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_lens(self) -> Lens:
        if (
            self.cx >= self.width
            or self.cy >= self.height
            or len(self.distortion) not in {0, 4, 5, 8}
        ):
            raise ValueError("Invalid lens dimensions, principal point or distortion")
        return self


class Axis(_Input):
    position_min: float = Field(ge=-1000000, le=1000000)
    position_max: float = Field(ge=-1000000, le=1000000)
    angle_min_radians: float = Field(ge=-2 * math.pi, le=2 * math.pi)
    angle_max_radians: float = Field(ge=-2 * math.pi, le=2 * math.pi)

    @model_validator(mode="after")
    def validate_axis(self) -> Axis:
        if (
            self.position_min >= self.position_max
            or self.angle_min_radians == self.angle_max_radians
        ):
            raise ValueError("Axis endpoints must describe a nonzero conversion")
        return self


class Profile(_Input):
    lens: Lens
    pan_axis: Axis
    tilt_axis: Axis
    zoom: float = Field(ge=-1000000, le=1000000)
    position_tolerance: float = Field(default=0.015, gt=0, le=0.1)
    settle_timeout_seconds: float = Field(default=20, ge=1, le=60)

    @model_validator(mode="after")
    def validate_profile(self) -> Profile:
        if (
            max(abs(self.tilt_axis.angle_min_radians), abs(self.tilt_axis.angle_max_radians))
            > math.pi / 2
        ):
            raise ValueError("Tilt elevation must stay between -pi/2 and pi/2")
        if (
            abs(self.pan_axis.angle_max_radians - self.pan_axis.angle_min_radians)
            > 2 * math.pi + 1e-9
        ):
            raise ValueError("Pan axis spans at most one revolution")
        return self


class Scan(_Input):
    pan_min: float
    pan_max: float
    tilt_min: float
    tilt_max: float
    overlap: float = Field(default=0.4, ge=0.2, le=0.7)


class Create(_Input):
    element_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    profile: Profile | None = None
    scan: Scan | None = None
    source_artifact_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    source_artifact_revision: int | None = Field(default=None, ge=1)
    reuse_draft: bool = False
    review_job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    review_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_origin(self) -> Create:
        if (self.review_job_id is None) != (self.review_revision is None) or (self.review_job_id and not self.source_artifact_id):
            raise ValueError("Review requires a saved panorama and the mapping revision")
        if self.source_artifact_id is not None:
            if (
                self.source_artifact_revision is None
                or self.profile is not None
                or self.scan is not None
            ):
                raise ValueError(
                    "A saved source panorama requires its revision and no manual profile"
                )
        elif self.source_artifact_revision is not None or self.profile is None or self.scan is None:
            raise ValueError("Choose a saved source panorama or provide a legacy capture profile")
        return self


class PrepareNativeReference(_Input):
    preparation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_id: str = Field(min_length=1, max_length=200)
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(ge=1)


class Revision(_Input):
    revision: int = Field(ge=1)


class Pixel(_Input):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class World(_Input):
    x: float = Field(ge=-1000000, le=1000000)
    z: float = Field(ge=-1000000, le=1000000)


class Point(_Input):
    id: str = Field(min_length=1, max_length=100)
    panorama: Pixel
    world: World


class Points(Revision):
    points: list[Point] = Field(max_length=64)


class Check(Revision):
    point_id: str = Field(min_length=1, max_length=100)


class CheckResult(Check):
    check_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    result: Literal["correct", "offset", "unverifiable"]
    observed_image: Pixel | None = None


class Aim(_Input):
    element_id: str = Field(min_length=1, max_length=200)
    world: World


class QualifyAttention(Revision):
    home: World
    target: World


class Restore(_Input):
    element_id: str = Field(min_length=1, max_length=200)
    expected_job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_revision: int = Field(ge=1)


def _error(code: str, message: str, status: int = 409) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"code": code, "message": message, "error": message}
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _angle(position: float, axis: dict[str, Any]) -> float:
    ratio = (position - axis["position_min"]) / (axis["position_max"] - axis["position_min"])
    return axis["angle_min_radians"] + ratio * (
        axis["angle_max_radians"] - axis["angle_min_radians"]
    )


def _position(angle: float, axis: dict[str, Any], *, wrap: bool = False) -> float:
    minimum, maximum = sorted((axis["angle_min_radians"], axis["angle_max_radians"]))
    candidates = [angle + offset * 2 * math.pi for offset in range(-2, 3)] if wrap else [angle]
    possible = [
        candidate for candidate in candidates if minimum - 1e-9 <= candidate <= maximum + 1e-9
    ]
    if not possible:
        raise _error(
            "outside_camera_limits", "This direction is outside the calibrated camera limits"
        )
    selected = min(possible, key=lambda candidate: abs(candidate - (minimum + maximum) / 2))
    ratio = (selected - axis["angle_min_radians"]) / (
        axis["angle_max_radians"] - axis["angle_min_radians"]
    )
    return axis["position_min"] + ratio * (axis["position_max"] - axis["position_min"])


def _grid(profile: dict[str, Any], scan: dict[str, Any]) -> list[dict[str, float]]:
    axes = []
    lens = profile["lens"]
    for name in ("pan", "tilt"):
        axis = profile[f"{name}_axis"]
        minimum, maximum = scan[f"{name}_min"], scan[f"{name}_max"]
        if not axis["position_min"] <= minimum <= maximum <= axis["position_max"]:
            raise _error(
                "invalid_scan_limits", "The scan must remain inside the profile limits", 422
            )
        # Use the narrowest edge-to-edge angle, including principal point and
        # Brown distortion. A centred pinhole shortcut can silently leave gaps.
        edges = (
            [((0, y), (lens["width"] - 1, y)) for y in (0, lens["cy"], lens["height"] - 1)]
            if name == "pan"
            else [((x, 0), (x, lens["height"] - 1)) for x in (0, lens["cx"], lens["width"] - 1)]
        )
        try:
            field_of_view = min(
                math.acos(
                    float(
                        np.clip(
                            np.dot(
                                image_pixel_to_ray(*first, lens, 0, 0),
                                image_pixel_to_ray(*last, lens, 0, 0),
                            ),
                            -1,
                            1,
                        )
                    )
                )
                for first, last in edges
            )
        except (ValueError, cv2.error):
            raise _error(
                "invalid_optical_profile",
                "The optical profile cannot produce a reliable capture grid",
                422,
            ) from None
        if not math.isfinite(field_of_view) or field_of_view < 1e-6:
            raise _error(
                "invalid_optical_profile",
                "The optical profile cannot produce a reliable capture grid",
                422,
            )
        step = field_of_view * (1 - scan["overlap"])
        count = max(1, math.ceil(abs(_angle(maximum, axis) - _angle(minimum, axis)) / step) + 1)
        if count > _MAX_CAPTURES:
            raise _error(
                "scan_too_large",
                "Choose a smaller scan area; at most 48 captures are supported",
                422,
            )
        axes.append(np.linspace(minimum, maximum, count).tolist())
    if len(axes[0]) * len(axes[1]) > _MAX_CAPTURES:
        raise _error(
            "scan_too_large", "Choose a smaller scan area; at most 48 captures are supported", 422
        )
    if (
        len(axes[0]) * len(axes[1]) * profile["lens"]["width"] * profile["lens"]["height"]
        > _MAX_DECODED_PIXELS
    ):
        raise _error("scan_memory_limit", "Choose a smaller scan area or image resolution", 422)
    return [
        {"pan": pan, "tilt": tilt, "zoom": profile["zoom"]}
        for row, tilt in enumerate(axes[1])
        for pan in (axes[0] if row % 2 == 0 else list(reversed(axes[0])))
    ]


class PanoramaService:
    def __init__(
        self,
        app: FastAPI,
        services: Any,
        authorize: Any,
        read_settings: Any,
        capture_snapshot: Any,
        reference_coordinator: PanoramaReferenceCoordinator | None = None,
    ):
        self.app, self.services, self.authorize = app, services, authorize
        self.read_settings, self.capture_snapshot = read_settings, capture_snapshot
        self.reference_coordinator = reference_coordinator or PanoramaReferenceCoordinator()
        self.store = app.state.config_store
        self.root = self.store.paths.data_dir / "runtime" / "cameras" / "panorama"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.busy: dict[str, str] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.cancelled: set[str] = set()
        self._native_preparations: dict[str, tuple[str, PrepareNativeReference, asyncio.Task]] = {}
        self.leases: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self._localizers: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._localization_lock = asyncio.Lock()
        self.camera_factory: Any = None
        self._navigation_scanners: dict[str, Any] = {}
        self._attention_responses: dict[str, dict[str, Any]] = {}
        for path in self.root.glob("*/job.json"):
            job = self._read(path)
            if (
                not job
                or not _IDENTIFIER.fullmatch(path.parent.name)
                or job.get("id") != path.parent.name
            ):
                continue
            if job.get("state") in {"capturing", "processing"}:
                job.update(
                    state="interrupted",
                    error={
                        "code": "process_interrupted",
                        "message": "Capture was interrupted; start a new capture deliberately",
                    },
                )
                self._save(job)
            if job.get("navigation", {}).get("phase") not in (None, "complete", "failed", "interrupted"):
                job["navigation"].update(phase="interrupted", physical_state="unknown")
                job["stop_confirmed"] = False
                self._save(job)
            self.jobs[job["id"]] = job

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
        temporary.replace(path)

    def _save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = time.time()
        self._atomic(self.root / job["id"] / "job.json", job)

    async def _public(
        self, job: dict[str, Any], *, pointer: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = {key: value for key, value in job.items() if not key.startswith("_")}
        context_validated = pointer is not None
        if pointer is None:
            pointer = await self._pointer(job)
        result["active"] = (
            pointer.get("job_id") == job["id"] and pointer.get("revision") == job["revision"]
        )
        result["permissions"] = {
            "map_validated": self._map_validated(job),
            "can_activate": self._map_usable(job),
            "can_verify_aim": self._map_validated(job),
            "aim_enabled": self._validated(job),
        }
        if job.get("source_panorama"):
            if not result.get("coverage_bounds"):
                try:
                    with Image.open(self.root / job["id"] / "coverage.png") as mask:
                        bounds = mask.getbbox()
                        if bounds:
                            left, top, right, bottom = bounds
                            result["coverage_bounds"] = {
                                "min_x": left / mask.width, "max_x": right / mask.width,
                                "min_y": top / mask.height, "max_y": bottom / mask.height,
                            }
                except OSError:
                    pass  # Missing imagery must not hide the draft's recovery controls.
            settings = (await self.store.get_settings()).extensions.get("com.toposync.cameras", {})
            camera = get_camera_device(settings, camera_id=job["camera_id"])
            source = get_camera_source(camera, source_id=job["source_id"]) if camera else None
            control_available = bool(camera and camera.get("enabled", True)
                                     and camera.get("control", {}).get("type") == "onvif"
                                     and source and source.get("enabled", True) and camera_source_has_ptz(source))
            control_available &= job["source_panorama"]["geometry"]["projection"] != "image_plane"
            result["permissions"]["can_verify_aim"] &= control_available
            result["permissions"]["aim_enabled"] &= control_available
            result["physical_blockers"] = [] if self._validated(job) or job["source_panorama"]["geometry"]["projection"] == "image_plane" else ["visual_positioning_not_verified"]
        if isinstance(job.get("solution"), dict):
            preview = preview_panorama_mapping(job["solution"])
            try:
                if not context_validated:
                    await self._current(job)
            except HTTPException as error:
                result["permissions"] = {key: False for key in result["permissions"]}
                preview = {
                    "eligible": False,
                    "status": "blocked",
                    "reasons": [*preview["reasons"], error.detail["code"]],
                }
            binding = job.get("source_panorama") or {}
            preview["context"] = {
                "job_id": job["id"],
                "revision": job["revision"],
                "source_id": job["source_id"],
                "source_artifact_id": binding.get("id"),
                "source_artifact_revision": binding.get("revision"),
            }
            result["solution"] = {**job["solution"], "preview": preview}
        return result

    async def _pointer(self, job: dict[str, Any]) -> dict[str, Any]:
        config = await self.store.get_config()
        for composition in config.compositions:
            if composition.id != job["composition_id"]:
                continue
            for element in composition.elements:
                if (
                    element.id == job["element_id"]
                    and element.props.get("camera_id") == job["camera_id"]
                ):
                    value = element.props.get("panorama_mapping")
                    return value if isinstance(value, dict) else {}
        return {}

    def _authorize(
        self, request: Request, camera_id: str, *, write: bool = False, control: bool = False
    ) -> None:
        self.authorize(
            request,
            action="core:camera:control" if control else "core:camera:read",
            resource_type="core:camera",
            resource_selector=camera_id,
        )
        self.authorize(
            request, action="core:compositions:write" if write else "core:compositions:read"
        )

    @staticmethod
    def _composition_digest(composition: Any, camera_element: Any) -> str:
        # Names, colours and editor presentation do not change world geometry.
        geometry_keys = {
            "vertices",
            "points",
            "width",
            "height",
            "depth",
            "length",
            "scale",
            "url",
            "image_url",
            "asset_id",
            "file_id",
            "src",
            "elevation",
            "closed",
        }
        geometry = [
            {
                "id": element.id,
                "type": element.type,
                "position": element.position.model_dump(),
                "rotation": element.rotation.model_dump(),
                "props": {
                    key: value for key, value in element.props.items() if key in geometry_keys
                },
            }
            for element in composition.elements
            if element.id == camera_element.id or not element.props.get("camera_id")
        ]
        return _digest(sorted(geometry, key=lambda item: item["id"]))

    @staticmethod
    def _composition_binding(composition: Any, camera_element: Any) -> dict[str, Any]:
        # Area contours select observations; their planar centre is not a calibration reference.
        geometry = []
        geometry_keys = {"vertices", "points", "width", "height", "depth", "length",
                         "scale", "url", "image_url", "asset_id", "file_id", "src",
                         "elevation", "closed"}
        for element in composition.elements:
            if element.id != camera_element.id and element.props.get("camera_id"):
                continue
            area = element.type == "com.toposync.structural.area"
            props = {key: value for key, value in element.props.items() if key in geometry_keys}
            position = element.position.model_dump()
            if area:
                for key in ("vertices", "points", "width", "depth", "length", "closed"):
                    props.pop(key, None)
                position = {"y": position["y"]}
            geometry.append({"id": element.id, "type": element.type,
                             "position": position, "rotation": element.rotation.model_dump(),
                             "props": props})
        return {"version": 2, "composition_id": composition.id,
                "geometry": sorted(geometry, key=lambda item: item["id"])}

    async def _context(
        self, camera_id: str, element_id: str, *, request: Request | None = None
    ) -> tuple[Any, Any, dict[str, Any]]:
        config = await self.store.get_config()
        matches = [
            (composition, element)
            for composition in config.compositions
            for element in composition.elements
            if element.id == element_id and str(element.props.get("camera_id", "")) == camera_id
        ]
        if len(matches) != 1:
            raise _error(
                "ambiguous_composition", "Select a unique camera element in a composition", 422
            )
        settings = (
            await self.read_settings(request)
            if request
            else (await self.store.get_settings()).extensions.get("com.toposync.cameras", {})
        )
        camera = get_camera_device(settings, camera_id=camera_id)
        if camera is None:
            raise _error("unknown_camera", "The camera is no longer available", 404)
        return matches[0][0], matches[0][1], camera

    def _source_digest(self, camera: dict[str, Any], source: dict[str, Any]) -> str:
        return _digest(
            {
                "camera": {
                    key: value
                    for key, value in camera.items()
                    if key not in {"sources", "name", "label", "metadata"}
                },
                "mount_revision": camera.get("metadata", {}).get("panorama_mount_revision"),
                "source": {
                    key: value
                    for key, value in source.items()
                    if key not in {"name", "label", "metadata"}
                },
                "profile": source.get("metadata", {}).get("panorama_profile"),
            }
        )

    def _source_artifact(
        self,
        camera: dict[str, Any],
        source: dict[str, Any],
        artifact_id: str,
        revision: int | None = None,
        *,
        require_active: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Path]]:
        """Validate the existing renderer's pixel/ray contract, never an actuator profile."""
        if not isinstance(artifact_id, str) or not _IDENTIFIER.fullmatch(artifact_id):
            raise _error("source_panorama_unavailable", "Choose a saved panorama from this source")
        metadata = source.get("metadata")
        pointer = metadata.get("panorama", {}) if isinstance(metadata, dict) else {}
        active_reference = pointer.get("active") if isinstance(pointer, dict) else None
        if require_active and (
            not isinstance(active_reference, dict)
            or active_reference.get("artifact_id") != artifact_id
        ):
            # A candidate is deliberately visible in source settings for review,
            # but it cannot become calibration or pointing geometry before the
            # source service promotes it to the current active pointer.
            raise _error(
                "source_panorama_unavailable", "Choose the active panorama from this source"
            )
        directory = self.root.parent / "source-panorama" / "artifacts" / artifact_id
        artifact = self._read(directory / "artifact.json")
        if not artifact or any(
            (
                artifact.get("id") != artifact_id,
                artifact.get("camera_id") != camera["id"],
                artifact.get("source_id") != source["id"],
            )
        ):
            raise _error("source_panorama_unavailable", "Choose a saved panorama from this source")
        if revision is not None and artifact.get("revision") != revision:
            raise _error("source_panorama_revision_conflict", "The saved panorama revision changed")
        if not _artifact_identity_matches(
            artifact, source_panorama_identity(camera, source),
            source_panorama_geometry_identity(camera, source),
        ):
            raise _error(
                "source_panorama_changed", "The source changed; choose a compatible panorama"
            )
        try:
            paths = {
                key: SourcePanoramaService._private_path(directory, artifact["_files"][key])
                for key in ("panorama", "coverage", "model")
            }
            model = self._read(paths["model"])
            quality = artifact.get("quality")
            planar = bool(model and model.get("projection") == "image_plane")
            if planar:
                from .processing.panorama_localization import frame_identity
                if (artifact.get("algorithm_version") != "single_frame_v1"
                        or model.get("algorithm_version") != "single_frame_v1"
                        or artifact.get("projection") != "image_plane"
                        or not isinstance(model.get("source_size"), list) or len(model["source_size"]) != 2
                        or any(type(value) is not int or value < 2 for value in model["source_size"])
                        or model.get("framing") not in {"fixed", "movable"}
                        or frame_identity(model.get("capture_evidence")) is None
                        or model.get("image_size") != [artifact.get("width"), artifact.get("height")]
                        or not isinstance(quality, dict) or artifact.get("status") != "ready" or quality.get("status") != "ready"
                        or quality.get("reasons") != [] or artifact.get("quality_approved") is not True):
                    raise ValueError("Invalid image plane geometry")
            else:
                if (
                    not model
                    or artifact.get("algorithm_version") not in {
                        "automatic_rotation_brown_v1",
                        "automatic_rotation_brown_v2",
                        "automatic_rotation_brown_v3",
                    }
                    or model.get("algorithm_version") != artifact["algorithm_version"]
                    or model.get("reference_frame")
                    != "estimated_presentation_right_down_forward_SO3_then_canonical_panorama_basis"
                    or model.get("status") != "estimated_for_visual_panorama_only"
                    or artifact.get("status") not in {"ready", "partial"}
                    or not isinstance(quality, dict)
                    or quality.get("status") != "ready"
                    or not isinstance(quality.get("reasons"), list)
                    or quality["reasons"]
                    or (
                        "quality_approved" in artifact
                        and artifact.get("quality_approved") is not True
                    )
                    or type(artifact.get("revision")) is not int
                    or artifact["revision"] < 1
                    or not model.get("captures")
                ):
                    raise ValueError("Unsupported reconstruction geometry")
                _rotation_basis(model["presentation"]["rotation_matrix"])
                for capture in model["captures"]:
                    _rotation_basis(capture["rotation_matrix"])
            width, height = artifact["width"], artifact["height"]
            if (
                type(width) is not int
                or type(height) is not int
                or not (2 <= width <= 8192 and 2 <= height <= 4096)
            ):
                raise ValueError("Invalid canonical image size")
            for key in ("panorama", "coverage"):
                if paths[key].stat().st_size > width * height * 4 + 65536:
                    raise ValueError("Image exceeds mapping storage limit")
                with Image.open(paths[key]) as image:
                    if image.size != (width, height) or image.format != "PNG":
                        raise ValueError(
                            "Image and coverage must use the same canonical dimensions"
                        )
                    if planar and key == "coverage" and not np.all(np.asarray(image.convert("L")) == 255):
                        raise ValueError("Static coverage must cover the captured frame")
            geometry = {
                "projection": "image_plane" if planar else "equirectangular",
                "reference_frame": "source_image" if planar else "canonical_panorama",
                "pixel_to_ray": None if planar else "panorama_pixel_to_ray_v1",
                **({"source_size": model["source_size"], "framing": model["framing"]} if planar else {}),
                "model_digest": _digest(model),
            }
        except (KeyError, TypeError, ValueError, OSError, HTTPException):
            raise _error(
                "source_panorama_geometry_incompatible",
                "This panorama has no compatible mapping geometry; prepare a compatible reference",
            ) from None
        reference = next((value for value in pointer.values()
                          if isinstance(value, dict) and value.get("artifact_id") == artifact_id), None)
        return {
            "id": artifact_id,
            "revision": artifact["revision"],
            "compatible": True,
            "blockers": [],
            "image_url": f"/api/cameras/panorama-artifacts/{artifact_id}/files/panorama",
            "coverage_url": f"/api/cameras/panorama-artifacts/{artifact_id}/files/coverage",
            "width": width,
            "height": height,
            "crop": reference.get("crop", artifact.get("crop")) if reference else artifact.get("crop"),
            "crop_revision": reference.get("crop_revision", 1) if reference else artifact.get("crop_revision", 1),
            "source_identity": artifact.get("_geometry_identity", artifact["_identity"]),
            "geometry": geometry,
        }, paths

    async def _current(self, job: dict[str, Any], request: Request | None = None) -> None:
        composition, element, camera = await self._context(
            job["camera_id"], job["element_id"], request=request
        )
        self._validate_current(job, composition, element, camera)

    def _validate_current(
        self, job: dict[str, Any], composition: Any, element: Any, camera: dict[str, Any]
    ) -> None:
        source = get_camera_source(camera, source_id=job["source_id"], enabled_only=True)
        binding = self._composition_binding(composition, element)
        saved_binding = job.get("_composition_binding")
        if (composition.id != job["composition_id"]
            or (saved_binding is not None and saved_binding != binding)
            or (saved_binding is None
                and self._composition_digest(composition, element) != job["_composition_digest"])):
            raise _error(
                "composition_changed",
                "The composition geometry changed; capture and validate a new mapping",
            )
        identity = (
            (source_panorama_geometry_identity(camera, source)
             if str(job["source_panorama"].get("source_identity", "")).startswith("geometry-v1:")
             else source_panorama_identity(camera, source))
            if source is not None and job.get("source_panorama")
            else self._source_digest(camera, source)
            if source is not None
            else None
        )
        if source is None or identity != job["_source_digest"]:
            raise _error(
                "camera_configuration_changed",
                "The camera configuration changed; capture and validate a new mapping",
            )
        if job.get("source_panorama"):
            binding = job["source_panorama"]
            if binding.get("geometry", {}).get("projection") == "image_plane":
                active = (source.get("metadata", {}).get("panorama", {}).get("active") or {})
                if active.get("artifact_id") != binding["id"]:
                    raise _error("source_panorama_changed", "The static reference changed; review the mapping")
            current, _ = self._source_artifact(
                camera,
                source,
                binding["id"],
                binding["revision"],
                require_active=False,
            )
            if current["geometry"] != binding["geometry"]:
                raise _error("source_panorama_changed", "The saved panorama geometry changed")
        if saved_binding is None:
            # Upgrade only a demonstrably valid legacy context, without changing its solution/checks.
            updated = {**job, "_composition_binding": self._composition_binding(composition, element)}
            self._save(updated)
            job.update(updated)

    def _job(self, camera_id: str, job_id: str, revision: int | None = None) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None or job["camera_id"] != camera_id:
            raise _error("unknown_job", "This calibration is not available", 404)
        if revision is not None and revision != job["revision"]:
            raise _error(
                "revision_conflict",
                "This calibration changed in another window; reload before continuing",
            )
        return job

    async def preflight(self, request: Request, camera_id: str, element_id: str) -> dict[str, Any]:
        self._authorize(request, camera_id)
        composition, element, camera = await self._context(camera_id, element_id, request=request)
        sources = iter_camera_sources(camera, kind="video", enabled_only=True)
        pointer = element.props.get("panorama_mapping")
        pointer = pointer if isinstance(pointer, dict) else None
        previous = element.props.get("previous_panorama_mapping") or {}
        candidates = [
            job
            for job in self.jobs.values()
            if job["camera_id"] == camera_id
            and job["element_id"] == element_id
            and job["composition_id"] == composition.id
            and ((job.get("_base_mapping") == pointer and previous.get("job_id") != job["id"])
                 or (pointer and pointer.get("job_id") == job["id"]))
        ]
        job = max(candidates, key=lambda value: value["updated_at"]) if candidates else None
        profile = (
            job["profile"]
            if job
            else next(
                (
                    source.get("metadata", {}).get("panorama_profile")
                    for source in sources
                    if source.get("metadata", {}).get("panorama_profile")
                ),
                None,
            )
        )
        if profile is not None:
            try:
                profile = Profile.model_validate(profile).model_dump()
            except ValueError:
                profile = None
        blockers = []
        if not sources:
            blockers.append("video_source_required")
        if not isinstance(camera.get("control"), dict) or camera["control"].get("type") in {
            None,
            "none",
        }:
            blockers.append("absolute_position_control_required")
        if profile is None:
            blockers.append("optical_and_axis_profile_required")
        if job:
            try:
                await self._current(job, request)
            except HTTPException as error:
                blockers.append(error.detail["code"])
        source_profiles = []
        for source in sources:
            try:
                source_profile = Profile.model_validate(
                    source.get("metadata", {}).get("panorama_profile")
                ).model_dump()
            except ValueError:
                source_profile = None
            panorama = None
            references = source.get("metadata", {}).get("panorama", {})
            active_reference = references.get("active") if isinstance(references, dict) else None
            if isinstance(active_reference, dict) and active_reference.get("artifact_id"):
                try:
                    panorama, _ = self._source_artifact(
                        camera, source, active_reference["artifact_id"]
                    )
                except HTTPException as error:
                    panorama = {
                        "id": active_reference["artifact_id"],
                        "compatible": False,
                        "blockers": [error.detail["code"]],
                    }
            source_profiles.append(
                {
                    "id": source["id"],
                    "label": source.get("name") or source["id"],
                    "profile": source_profile,
                    "panorama": panorama,
                }
            )
        if any((item.get("panorama") or {}).get("compatible") for item in source_profiles):
            blockers = [
                item
                for item in blockers
                if item
                not in {
                    "optical_and_axis_profile_required",
                    "absolute_position_control_required",
                }
            ]
        return {
            "camera_id": camera_id,
            "element_id": element_id,
            "composition_id": composition.id,
            "sources": source_profiles,
            "profile": profile,
            "job": await self._public(job) if job else None,
            "active": {key: pointer.get(key) for key in ("job_id", "revision")}
            if pointer
            else None,
            "previous": element.props.get("previous_panorama_mapping"),
            "blockers": blockers,
        }

    async def create(self, request: Request, camera_id: str, body: Create) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        composition, element, camera = await self._context(
            camera_id, body.element_id, request=request
        )
        source = get_camera_source(camera, source_id=body.source_id, enabled_only=True)
        if source is None or source.get("kind") != "video":
            raise _error("unknown_source", "Choose an enabled video source", 422)
        binding, paths = None, {}
        if body.source_artifact_id:
            binding, paths = self._source_artifact(
                camera, source, body.source_artifact_id, body.source_artifact_revision
            )
        elif not isinstance(camera.get("control"), dict) or camera["control"].get("type") in {
            None,
            "none",
        }:
            raise _error(
                "absolute_position_control_required",
                "This camera requires absolute position control",
                422,
            )
        profile = body.profile.model_dump() if body.profile else None
        scan = body.scan.model_dump() if body.scan else None
        grid = _grid(profile, scan) if profile and scan else []
        async with self.lock:
            review = None
            active = element.props.get("panorama_mapping")
            if body.review_job_id:
                review = self._job(camera_id, body.review_job_id, body.review_revision)
                self._validate_current(review, composition, element, camera)
                review_binding = review.get("source_panorama") or {}
                if (
                    review["element_id"] != body.element_id
                    or not active or active.get("job_id") != review["id"]
                    or active.get("revision") != review["revision"]
                    or review["source_id"] != body.source_id
                    or any(review_binding.get(key) != binding[key] for key in ("id", "revision", "geometry"))
                ):
                    raise _error("active_changed", "Reload the active calibration before reviewing it")
            if binding and body.reuse_draft:
                for existing in sorted(self.jobs.values(), key=lambda item: item["updated_at"], reverse=True):
                    if (existing["camera_id"] != camera_id or existing["element_id"] != body.element_id
                        or existing["composition_id"] != composition.id or existing["source_id"] != body.source_id
                        or existing["state"] != "ready" or existing.get("_base_mapping") != active
                        or (active and active.get("job_id") == existing["id"])
                        or (element.props.get("previous_panorama_mapping") or {}).get("job_id") == existing["id"]
                        or existing.get("source_panorama", {}).get("id") != binding["id"]
                        or existing.get("source_panorama", {}).get("revision") != binding["revision"]):
                        continue
                    try:
                        self._validate_current(existing, composition, element, camera)
                    except HTTPException:
                        continue
                    return await self._public(existing)
            if camera_id in self.busy:
                raise _error("camera_busy", "Finish or cancel the current camera operation")
            if len(self.jobs) >= 128:
                raise _error("storage_limit", "The calibration history limit has been reached", 507)
            identifier = uuid.uuid4().hex
            job = {
                "id": identifier,
                "revision": 1,
                "camera_id": camera_id,
                "source_id": body.source_id,
                "element_id": body.element_id,
                "composition_id": composition.id,
                "state": "ready" if binding else "draft",
                "profile": profile,
                "scan": scan,
                "progress": {"captured": 0, "total": len(grid), "stage": "draft"},
                "panorama_url": None,
                "coverage_url": None,
                "points": [],
                "solution": None,
                "checks": [],
                "active": False,
                "error": None,
                "updated_at": time.time(),
                "_composition_digest": self._composition_digest(composition, element),
                "_composition_binding": self._composition_binding(composition, element),
                "_source_digest": self._source_digest(camera, source),
                "_captures": [],
                "_base_mapping": element.props.get("panorama_mapping"),
            }
            if review:
                job["points"] = copy.deepcopy(review["points"])
                job["solution"] = copy.deepcopy(review["solution"])
            if binding:
                mask = cv2.imread(str(paths["coverage"]), cv2.IMREAD_GRAYSCALE)
                if mask is None or not np.any(mask):
                    raise _error(
                        "coverage_unavailable", "The saved panorama has no captured coverage"
                    )
                job.update(
                    source_panorama=binding,
                    physical_blockers=["visual_positioning_not_verified"],
                    panorama_url=self._image_url(job, "panorama.png"),
                    coverage_url=self._image_url(job, "coverage.png"),
                    width=binding["width"],
                    height=binding["height"],
                    progress={"captured": 0, "total": 0, "stage": "ready"},
                    _source_digest=binding["source_identity"],
                )
                (self.root / identifier).mkdir()
                try:
                    for key in ("panorama", "coverage"):
                        self._image_write(
                            job,
                            f"{key}.png",
                            paths[key].read_bytes(),
                            maximum_bytes=binding["width"] * binding["height"] * 4 + 65536,
                        )
                except Exception:
                    shutil.rmtree(self.root / identifier)
                    raise
            self._save(job)
            self.jobs[identifier] = job
        return await self._public(job)

    def _image_url(self, job: dict[str, Any], filename: str) -> str:
        return f"/api/cameras/cameras/{quote(job['camera_id'], safe='')}/panorama/{job['id']}/images/{filename}"

    def _image_write(
        self,
        job: dict[str, Any],
        filename: str,
        payload: bytes,
        *,
        maximum_bytes: int = _MAX_IMAGE_BYTES,
    ) -> None:
        used = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        if len(payload) > maximum_bytes or used + len(payload) > _MAX_STORAGE:
            raise _error(
                "storage_limit", "The calibration image storage limit has been reached", 507
            )
        destination = self.root / job["id"] / filename
        temporary = destination.with_name(filename + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(destination)

    async def _reserve(self, camera_id: str, job_id: str) -> None:
        async with self.lock:
            if camera_id in self.busy:
                raise _error("camera_busy", "Finish or cancel the current camera operation")
            self.busy[camera_id] = job_id
            self.cancelled.discard(job_id)

    async def _acquire(self, job: dict[str, Any]) -> None:
        lease = await self.services.call(
            "cameras.control.acquire",
            camera_id=job["camera_id"],
            camera_source_id=job["source_id"],
            owner_kind="manual",
            owner_id=f"panorama:{job['id']}",
            ttl_s=15.0,
        )
        if not isinstance(lease, dict) or not lease.get("lease_id") or not lease.get("fence"):
            raise _error("control_unavailable", "The camera control could not be acquired")
        self.leases[job["id"]] = lease

    def _not_cancelled(self, job: dict[str, Any]) -> None:
        if job["id"] in self.cancelled:
            raise _error("capture_cancelled", "The camera operation was cancelled")

    async def _snapshot(self, job: dict[str, Any], *, physical: bool = True) -> dict[str, Any]:
        snapshot = await self.services.call(
            "cameras.control.snapshot",
            camera_id=job["camera_id"],
            source_id=job["source_id"],
            refresh_physical=physical,
            include_readiness=False,
        )
        lease = self.leases.get(job["id"], {})
        active = snapshot.get("active_lease") or {}
        if (
            not lease
            or active.get("lease_id") != lease["lease_id"]
            or active.get("fence") != lease["fence"]
        ):
            raise _error(
                "control_lost", "Another operation acquired the camera; this operation stopped"
            )
        if snapshot.get("transport_binding_current") is False:
            raise _error("camera_configuration_changed", "The camera control configuration changed")
        return snapshot

    async def _renew(self, job: dict[str, Any]) -> None:
        lease = self.leases[job["id"]]
        await self.services.call(
            "cameras.control.renew", lease_id=lease["lease_id"], fence=lease["fence"], ttl_s=15.0
        )

    @staticmethod
    def _at_target(
        snapshot: dict[str, Any], target: dict[str, float], profile: dict[str, Any]
    ) -> bool:
        pose = snapshot.get("pose") or {}
        if snapshot.get("geometry_safe") is not True:
            return False
        for axis in ("pan", "tilt", "zoom"):
            value = pose.get(axis)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                return False
            if abs(value - target[axis]) > profile["position_tolerance"]:
                return False
        return True

    async def _move(self, job: dict[str, Any], target: dict[str, float]) -> dict[str, Any]:
        self._not_cancelled(job)
        await self._renew(job)
        lease = self.leases[job["id"]]
        command_id = f"panorama_{uuid.uuid4().hex}"
        receipt = await self.services.call(
            "cameras.control.submit",
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            command_id=command_id,
            command={"kind": "absolute_move", **target},
        )
        if not self._receipt_matches(receipt, lease, command_id):
            raise _error("movement_unconfirmed", "The camera did not confirm this movement")
        deadline = time.monotonic() + job["profile"]["settle_timeout_seconds"]
        while time.monotonic() < deadline:
            self._not_cancelled(job)
            snapshot = await self._snapshot(job)
            if self._at_target(snapshot, target, job["profile"]):
                return snapshot
            await asyncio.sleep(0.25)
            await self._renew(job)
        raise _error(
            "position_unconfirmed", "The camera position did not stabilize at the requested target"
        )

    async def _frame(
        self, request: Request, job: dict[str, Any], target: dict[str, float]
    ) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        before = await self._snapshot(job)
        if not self._at_target(before, target, job["profile"]):
            raise _error("position_unconfirmed", "The camera moved before the image was captured")
        await self._renew(job)
        started = time.time()
        try:
            response = await asyncio.wait_for(
                self.capture_snapshot(
                    request,
                    job["camera_id"],
                    source_id=job["source_id"],
                    fresh=True,
                    freshness="physical",
                ),
                timeout=12.0,
            )
        except Exception:
            raise _error(
                "fresh_frame_unavailable", "A current physical camera image is unavailable"
            ) from None
        self._not_cancelled(job)
        headers = response.headers
        if (
            headers.get("x-toposync-snapshot-capture-evidence") != "verified"
            or headers.get("x-toposync-snapshot-freshness") != "verified"
        ):
            raise _error("freshness_unverified", "A fresh physical image could not be verified")
        try:
            captured_at = float(headers.get("x-toposync-snapshot-captured-timestamp", "nan"))
        except ValueError:
            captured_at = math.nan
        if not math.isfinite(captured_at) or captured_at < started or captured_at > time.time() + 1:
            raise _error("freshness_unverified", "The image capture timestamp is not current")
        after = await self._snapshot(job)
        if not self._at_target(after, target, job["profile"]) or before.get(
            "motion_epoch"
        ) != after.get("motion_epoch"):
            raise _error(
                "camera_moved_during_capture", "The camera moved while the image was captured"
            )
        payload = bytes(response.body)
        if not payload or len(payload) > _MAX_IMAGE_BYTES:
            raise _error("invalid_image", "The captured image is empty or exceeds its size limit")
        lens = job["profile"]["lens"]
        try:
            with Image.open(io.BytesIO(payload)) as header:
                if header.size != (lens["width"], lens["height"]):
                    raise ValueError("Image dimensions differ")
        except Exception:
            raise _error(
                "image_profile_mismatch", "The image dimensions differ from the optical profile"
            ) from None
        image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape != (lens["height"], lens["width"], 3):
            raise _error(
                "image_profile_mismatch", "The image dimensions differ from the optical profile"
            )
        evidence = {
            "captured_at": captured_at,
            "verified": True,
            "motion_epoch": after.get("motion_epoch"),
            "pose": after["pose"],
        }
        return payload, after, evidence

    async def _finish(self, job: dict[str, Any]) -> None:
        navigator = self._navigation_scanners.get(job["id"])
        if navigator is not None:
            job["stop_confirmed"] = await navigator._stop()
        lease = self.leases.pop(job["id"], None)
        if lease:
            job["stop_confirmed"] = False
            try:
                command_id = f"panorama_stop_{uuid.uuid4().hex}"
                receipt = await self.services.call(
                    "cameras.control.submit",
                    lease_id=lease["lease_id"],
                    fence=lease["fence"],
                    command_id=command_id,
                    command={"kind": "stop"},
                )
                if not self._receipt_matches(receipt, lease, command_id):
                    raise RuntimeError("Stop not confirmed")
            except Exception:
                try:
                    stopped = await self.services.call(
                        "cameras.control.emergency_stop",
                        camera_id=job["camera_id"],
                        camera_source_id=job["source_id"],
                        expected_lease_id=lease["lease_id"],
                        expected_fence=lease["fence"],
                    )
                    job["stop_confirmed"] = (
                        stopped.get("state_published") is True and stopped.get("ok") is not False
                    )
                except Exception:
                    # Fenced fallback cannot stop a replacement owner.
                    job["stop_confirmed"] = False
            else:
                job["stop_confirmed"] = True
            finally:
                try:
                    await self.services.call(
                        "cameras.control.release", lease_id=lease["lease_id"], fence=lease["fence"]
                    )
                except Exception:
                    pass

    @staticmethod
    def _receipt_matches(receipt: Any, lease: dict[str, Any], command_id: str) -> bool:
        return isinstance(receipt, dict) and all(
            (
                receipt.get("accepted") is True,
                receipt.get("stale_after_execution") is False,
                receipt.get("lease_id") == lease["lease_id"],
                receipt.get("fence") == lease["fence"],
                receipt.get("command_id") == command_id,
            )
        )

    async def start_capture(
        self, request: Request, camera_id: str, job_id: str, body: Revision
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True, control=True)
        job = self._job(camera_id, job_id, body.revision)
        if job.get("source_panorama"):
            raise _error(
                "source_panorama_immutable", "Capture a new panorama in the source settings"
            )
        if (await self._public(job))["active"]:
            raise _error(
                "active_mapping_immutable",
                "Create a new calibration before changing the active mapping",
            )
        if job["state"] not in {"draft", "failed", "cancelled", "interrupted"}:
            raise _error(
                "capture_already_created", "Create a new calibration to replace this panorama"
            )
        await self._current(job, request)
        await self._reserve(camera_id, job_id)
        job.update(state="capturing", error=None)
        job["_captures"] = []
        job["progress"].update(captured=0, stage="capturing")
        self._save(job)
        self.tasks[job_id] = asyncio.create_task(
            self._capture(request, job), name=f"panorama-{job_id}"
        )
        return await self._public(job)

    async def _capture(self, request: Request, job: dict[str, Any]) -> None:
        try:
            await self._acquire(job)
            for index, target in enumerate(_grid(job["profile"], job["scan"])):
                self._not_cancelled(job)
                await self._current(job, request)
                await self._move(job, target)
                payload, snapshot, evidence = await self._frame(request, job, target)
                filename = f"capture-{index:03d}.jpg"
                self._image_write(job, filename, payload)
                pose = snapshot["pose"]
                job["_captures"].append(
                    {
                        "id": str(index),
                        "filename": filename,
                        "pan_radians": _angle(pose["pan"], job["profile"]["pan_axis"]),
                        "tilt_radians": _angle(pose["tilt"], job["profile"]["tilt_axis"]),
                        "evidence": evidence,
                    }
                )
                job["progress"]["captured"] = index + 1
                self._save(job)
            await self._finish(job)
            self._not_cancelled(job)
            if job.get("stop_confirmed") is not True:
                raise _error(
                    "stop_unconfirmed",
                    "The camera operation ended, but its stop could not be confirmed",
                )
            job["state"], job["progress"]["stage"] = "processing", "processing"
            self._save(job)
            captures = [
                {
                    **capture,
                    "lens": job["profile"]["lens"],
                    "image": cv2.imread(str(self.root / job["id"] / capture["filename"])),
                }
                for capture in job["_captures"]
            ]
            rendered = await asyncio.to_thread(render_panorama, captures)
            self._not_cancelled(job)
            for name, image in (
                ("panorama.jpg", rendered["image"]),
                ("coverage.png", rendered["coverage_mask"]),
            ):
                success, encoded = cv2.imencode(Path(name).suffix, image)
                if not success:
                    raise _error("image_encoding_failed", "The panorama could not be saved")
                self._image_write(job, name, encoded.tobytes())
            rows, columns = np.nonzero(rendered["coverage_mask"])
            if not len(rows):
                raise _error(
                    "empty_panorama", "The captured images did not produce a usable panorama"
                )
            height, width = rendered["coverage_mask"].shape
            job["coverage_bounds"] = {
                "min_x": float(columns.min() / width),
                "max_x": float((columns.max() + 1) / width),
                "min_y": float(rows.min() / height),
                "max_y": float((rows.max() + 1) / height),
            }
            job.update(
                state="ready",
                panorama_url=self._image_url(job, "panorama.jpg"),
                coverage_url=self._image_url(job, "coverage.png"),
                coverage_ratio=rendered["coverage_ratio"],
            )
            job["progress"]["stage"] = "ready"
        except asyncio.CancelledError:
            job.update(
                state="interrupted",
                error={
                    "code": "process_interrupted",
                    "message": "The camera operation was interrupted",
                },
            )
            raise
        except Exception as error:
            if job["id"] in self.cancelled:
                job.update(state="cancelled", error=None)
            else:
                detail = (
                    error.detail
                    if isinstance(error, HTTPException) and isinstance(error.detail, dict)
                    else {
                        "code": "capture_failed",
                        "message": "The capture could not be completed; the saved work is preserved",
                    }
                )
                job.update(state="failed", error=detail)
        finally:
            await self._finish(job)
            self._save(job)
            self.tasks.pop(job["id"], None)
            if self.busy.get(job["camera_id"]) == job["id"]:
                self.busy.pop(job["camera_id"], None)

    async def cancel(
        self, request: Request, camera_id: str, job_id: str, body: Revision
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True, control=True)
        job = self._job(camera_id, job_id, body.revision)
        self.cancelled.add(job_id)
        await self._finish(job)
        if job["state"] in {"capturing", "processing", "draft"}:
            job.update(state="cancelled", error=None)
        self._save(job)
        return await self._public(job)

    async def points(
        self, request: Request, camera_id: str, job_id: str, body: Points, *, preview: bool = False
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        job = self._job(camera_id, job_id, body.revision)
        await self._current(job, request)
        if job["state"] != "ready" or camera_id in self.busy:
            raise _error(
                "panorama_not_ready", "Wait for the camera operation before editing the places"
            )
        if (await self._public(job))["active"]:
            raise _error(
                "active_mapping_immutable",
                "Create a new calibration before changing the active mapping",
            )
        if len({point.id for point in body.points}) != len(body.points):
            raise _error("duplicate_point", "Every place must have a unique identifier", 422)
        mask = cv2.imread(str(self.root / job_id / "coverage.png"), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            raise _error("coverage_unavailable", "The panorama coverage is unavailable")
        geometry_points = []
        for point in body.points:
            column = min(mask.shape[1] - 1, int(point.panorama.x * mask.shape[1]))
            row = min(mask.shape[0] - 1, int(point.panorama.y * mask.shape[0]))
            if not mask[row, column]:
                raise _error(
                    "point_outside_capture",
                    "Choose a visible place inside the captured panorama",
                    422,
                )
            geometry_points.append(
                {
                    "id": point.id,
                    "world_x": point.world.x,
                    "world_z": point.world.z,
                    **({"image_u": point.panorama.x, "image_v": point.panorama.y}
                       if (job.get("source_panorama") or {}).get("geometry", {}).get("projection") == "image_plane"
                       else {"ray": panorama_pixel_to_ray(point.panorama.x, point.panorama.y)}),
                }
            )
        estimator = estimate_image_plane_mapping if (job.get("source_panorama") or {}).get("geometry", {}).get("projection") == "image_plane" else estimate_panorama_mapping
        solution = await asyncio.to_thread(estimator, geometry_points)
        if preview:
            self._job(camera_id, job_id, body.revision)
            return await self._public({**job, "points": [point.model_dump() for point in body.points], "solution": solution})
        async with self.lock:
            self._job(camera_id, job_id, body.revision)
            await self._current(job, request)
            if (await self._public(job))["active"]:
                raise _error(
                    "active_mapping_immutable",
                    "Create a new calibration before changing the active mapping",
                )
            if camera_id in self.busy:
                raise _error("camera_busy", "Wait for the current camera operation")
            updated = dict(job,
                points=[point.model_dump() for point in body.points],
                solution=solution,
                checks=[],
                revision=body.revision + 1,
                error=None,
            )
            self._save(updated)
            job.update(updated)
        return await self._public(job)

    def _target(self, job: dict[str, Any], world: dict[str, float]) -> dict[str, Any]:
        if (job.get("source_panorama") or {}).get("geometry", {}).get("projection") == "image_plane":
            raise _error("movement_not_applicable", "This reference is a single image without camera positioning")
        # Extrapolated observations do not widen the physical navigation envelope.
        if mapping_extrapolated(job.get("solution") or {}, world["x"], world["z"]):
            raise _error("outside_validated_region", "Choose a place inside the mapped region", 422)
        if job.get("source_panorama"):
            if not self._map_validated(job):
                raise _error("mapping_not_ready", "Validate the mapped places before pointing")
            ray = map_world_to_ray(job["solution"], world["x"], world["z"])
            if ray is None:
                raise _error("outside_validated_region", "Choose a place inside the mapped region", 422)
            return {"ray": list(ray)}
        if (
            job["state"] != "ready"
            or not job.get("solution")
            or job["solution"]["quality"]["status"] != "ready"
        ):
            raise _error(
                "mapping_not_ready",
                "Review the places and independent checks before pointing the camera",
            )
        ray = map_world_to_ray(job["solution"], world["x"], world["z"])
        if ray is None:
            raise _error("outside_validated_region", "Choose a place inside the mapped region", 422)
        pan, tilt = ray_to_pan_tilt(ray)
        pan_axis = job["profile"]["pan_axis"]
        scan_pan_axis = {
            "position_min": job["scan"]["pan_min"],
            "position_max": job["scan"]["pan_max"],
            "angle_min_radians": _angle(job["scan"]["pan_min"], pan_axis),
            "angle_max_radians": _angle(job["scan"]["pan_max"], pan_axis),
        }
        if scan_pan_axis["position_min"] == scan_pan_axis["position_max"]:
            difference = math.remainder(pan - scan_pan_axis["angle_min_radians"], 2 * math.pi)
            if abs(difference) > 1e-6:
                raise _error(
                    "outside_capture_limits",
                    "This place is outside the captured camera limits",
                    422,
                )
            pan_position = scan_pan_axis["position_min"]
        else:
            pan_position = _position(pan, scan_pan_axis, wrap=True)
        target = {
            "pan": pan_position,
            "tilt": _position(tilt, job["profile"]["tilt_axis"]),
            "zoom": job["profile"]["zoom"],
        }
        if any(
            not job["scan"][f"{axis}_min"] - 1e-9
            <= target[axis]
            <= job["scan"][f"{axis}_max"] + 1e-9
            for axis in ("pan", "tilt")
        ):
            raise _error(
                "outside_capture_limits", "This place is outside the captured camera limits", 422
            )
        return target

    async def _point_and_capture(
        self, request: Request, job: dict[str, Any], target: dict[str, float]
    ) -> tuple[str, dict[str, Any]]:
        if job.get("source_panorama"):
            return await self._visual_operation(request, job, target)
        await self._reserve(job["camera_id"], job["id"])
        try:
            await self._acquire(job)
            await self._move(job, target)
            payload, _, evidence = await self._frame(request, job, target)
            filename = f"check-{uuid.uuid4().hex}.jpg"
            self._image_write(job, filename, payload)
            result = self._image_url(job, filename), evidence
        except HTTPException:
            raise
        except Exception:
            raise _error(
                "verification_failed",
                "The camera verification did not complete; try again when it is available",
            ) from None
        finally:
            await self._finish(job)
            if self.busy.get(job["camera_id"]) == job["id"]:
                self.busy.pop(job["camera_id"], None)
        if job.get("stop_confirmed") is not True:
            raise _error(
                "stop_unconfirmed",
                "The camera operation ended, but its stop could not be confirmed",
            )
        return result

    async def _visual_operation(
        self, request: Request, job: dict[str, Any], target: dict[str, Any] | None,
        *, borrowed_control: dict | None = None, maximum_error_pixels: float = 12,
    ) -> tuple[str, dict[str, Any]]:
        # Pin the exact source reference before any camera reservation. Every
        # supported pointer writer takes the same per-source fence, so active
        # geometry cannot change between validation and a motor command.
        async with self.reference_coordinator.hold(job["camera_id"], job["source_id"]):
            await self._current(job, request)
            return await self._visual_operation_with_pinned_reference(request, job, target,
                **({"borrowed_control": borrowed_control, "maximum_error_pixels": maximum_error_pixels} if borrowed_control is not None else {}))

    async def _visual_operation_with_pinned_reference(
        self, request: Request, job: dict[str, Any], target: dict[str, Any] | None,
        *, borrowed_control: dict | None = None, maximum_error_pixels: float = 12,
        qualification_home: dict[str, Any] | None = None,
        target_provider: Callable[[], list[float]] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        from .panorama_scan import _Scan, _Stopped, _write_image, _match
        from .panorama_capture import PanoramaCaptureError
        from .panorama_navigation import VisualNavigator, _arrival_stop_is_current

        if self.camera_factory is None:
            raise _error("visual_control_unavailable", "Visual camera control is unavailable")
        directory = self.root / job["id"] / "navigation"
        checkpoint = {} if borrowed_control is not None or qualification_home is not None else dict(job.get("_navigation_checkpoint") or {})
        checkpoint["active_seconds"] = 0.0
        checkpoint.pop("navigation_commands", None)
        preparation = {"started_at": time.time()}
        preparation_started = time.monotonic()
        preparation_elapsed_ms = {}

        def prepared(stage: str) -> None:
            preparation[f"{stage}_at"] = time.time()
            preparation_elapsed_ms[stage] = (time.monotonic() - preparation_started) * 1000

        camera = self.camera_factory(services=self.services, camera_id=job["camera_id"],
                                     source_id=job["source_id"], settings={}, job_id=job["id"], output_dir=directory,
                                     **({"borrowed_control": borrowed_control} if borrowed_control is not None else {}))

        async def progress(event: dict[str, Any]) -> None:
            if event.get("checkpoint") and borrowed_control is None:
                job["_navigation_checkpoint"] = event["checkpoint"]
            job["navigation"] = {"phase": event.get("phase"), "physical_state": event.get("physical_state", "unknown")}
            self._save(job)

        scanner = _Scan(camera, directory, progress, lambda: job["id"] in self.cancelled, checkpoint)
        await self._reserve(job["camera_id"], job["id"])
        prepared("reserved")
        self._navigation_scanners[job["id"]] = scanner
        image_url, evidence = "", {}
        failure_code = None
        cleanup_cancellation = None
        navigator = None
        response_fields = ("response", "response_rotations", "pulse_limits",
                           "direction_responses", "stationary_durations")
        response_binding = None
        response_observed = None
        job["stop_confirmed"] = False
        try:
            scanner.capabilities = await camera.discover()
            prepared("discovered")
            await camera.acquire()
            prepared("acquired")
            scanner.acquired = True
            if not await scanner._stop():
                raise PanoramaCaptureError("stop_unconfirmed")
            prepared("stopped")
            scanner.last_frame = await scanner._reference_window()
            prepared("reference")
            scanner.last_pose = await camera.position()
            prepared("position")
            scanner.physical_state = "stopped"
            if target is None:
                if scanner.saved_return:
                    await scanner._restore()
                elif scanner.checkpoint.get("visual_original_ray"):
                    binding = job["source_panorama"]
                    await self.localize_frame(camera_id=job["camera_id"], source_id=job["source_id"], job_id=job["id"], revision=job["revision"], image=scanner.last_frame["image"], capture_evidence=scanner.last_frame.get("capture_evidence", {}))
                    localizer = self._localizers[(binding["id"], binding["geometry"]["model_digest"])]
                    await VisualNavigator(scanner, localizer).aim(scanner.checkpoint["visual_original_ray"])
                    original = scanner._private_image(scanner.checkpoint["initial_path"])
                    comparison = await asyncio.to_thread(_match, original, scanner.last_frame["image"])
                    if comparison.get("verified") and comparison.get("overlap", 0) >= .85 and comparison["displacement"] <= 3:
                        scanner.physical_state = "restored"
                if scanner.physical_state != "restored":
                    raise PanoramaCaptureError("return_framing_unconfirmed")
                evidence = {"verified": True, "kind": "visual_return_verified", "physical_capture_verified": False}
                if scanner.saved_return:
                    try:
                        await camera.remove_return(scanner.saved_return)
                    except Exception:
                        scanner.issues.append({"code": "return_cleanup_unconfirmed"})
                    else:
                        scanner.saved_return = None
                        scanner.checkpoint["return"] = None
                if not scanner.saved_return:
                    scanner.checkpoint.pop("visual_original_ray", None)
                    scanner.checkpoint.pop("initial_path", None)
            else:
                if qualification_home is None and borrowed_control is None and not scanner.saved_return and not scanner.checkpoint.get("visual_original_ray"):
                    path = directory / "initial-reference.jpg"
                    await asyncio.to_thread(_write_image, path, scanner.last_frame["image"])
                    scanner.checkpoint.update(initial_path=str(path), initial_zoom=scanner.last_pose.get("zoom"),
                                              initial_reference_evidence=scanner.last_frame.get("return_reference_evidence"))
                    await scanner._persist()
                    if not await scanner._save_original_destination():
                        raise PanoramaCaptureError("return_reference_unavailable")
                located = await self.localize_frame(
                    camera_id=job["camera_id"], source_id=job["source_id"], job_id=job["id"], revision=job["revision"],
                    image=scanner.last_frame["image"], capture_evidence=scanner.last_frame.get("capture_evidence", {}),
                )
                prepared("localized")
                if located.get("status") != "localized":
                    raise PanoramaCaptureError(located.get("reason", "panorama_visual_localization_failed"))
                if qualification_home is None and borrowed_control is None and not scanner.checkpoint.get("visual_original_ray"):
                    scanner.checkpoint["visual_original_ray"] = _rotation_basis(located["rotation_matrix"])[:, 2].tolist()
                    await scanner._persist()
                binding = job["source_panorama"]
                localizer = self._localizers[(binding["id"], binding["geometry"]["model_digest"])]
                navigator = VisualNavigator(scanner, localizer)
                if borrowed_control is not None or qualification_home is not None:
                    response_binding = _digest({
                        "job_id": job["id"], "revision": job["revision"],
                        "source": job.get("_source_digest"), "composition": job.get("_composition_digest"),
                        "panorama": binding, "optical_identity": scanner.capabilities.get("source_identity"),
                    })
                    cached = getattr(self, "_attention_responses", {}).get(job["camera_id"], {})
                    if (borrowed_control is not None and cached.get("binding") == response_binding
                            and 0 <= time.monotonic() - cached.get("observed_monotonic", 0) <= 300):
                        for name in response_fields:
                            setattr(navigator, name, copy.deepcopy(cached["response"][name]))
                        response_observed = cached["observed_monotonic"]
                        prepared("response_reused")
                if qualification_home is not None:
                    stages = []
                    for label, destination, tolerance in (
                        ("home", qualification_home, 3),
                        ("arrival", target, 12),
                        ("return", qualification_home, 3),
                    ):
                        started = time.monotonic()
                        observed = await navigator.aim(destination["ray"], maximum_error_pixels=tolerance)
                        stage_filename = f"check-{uuid.uuid4().hex}.jpg"
                        okay, encoded = await asyncio.to_thread(cv2.imencode, ".jpg", scanner.last_frame["image"])
                        if not okay:
                            raise PanoramaCaptureError("capture_write_failed")
                        self._image_write(job, stage_filename, encoded.tobytes())
                        stages.append({"stage": label, "target": destination, "evidence": observed,
                                       "image_url": self._image_url(job, stage_filename),
                                       "elapsed_seconds": time.monotonic() - started})
                        job["_attention_trial"] = {"status": "running", "stages": stages}
                        self._save(job)
                    evidence = {"verified": True, "kind": "attention_round_trip_verified", "stages": stages}
                else:
                    evidence = await navigator.aim(target["ray"],
                        **({"maximum_error_pixels": maximum_error_pixels} if borrowed_control is not None else {}),
                        **({"target_provider": target_provider} if target_provider is not None else {}))
                evidence["lens"] = localizer.lens
            await self._current(job, request)
        except (PanoramaCaptureError, _Stopped) as error:
            failure_code = getattr(error, "code", "capture_cancelled")
            raise _error(getattr(error, "code", "capture_cancelled"), "The visual camera operation could not be confirmed") from None
        except asyncio.CancelledError:
            failure_code = "capture_cancelled"
            raise
        except HTTPException as error:
            failure_code = error.detail.get("code", "capture_failed") if isinstance(error.detail, dict) else "capture_failed"
            raise
        except Exception:
            failure_code = "capture_failed"
            raise _error(failure_code, "The visual camera operation could not be completed") from None
        finally:
            try:
                restored = scanner.physical_state == "restored"
                if scanner.acquired:
                    control_stopped = True
                    arrival_reused = False
                    if borrowed_control is not None:
                        control_stopped = False
                        try:
                            if (not failure_code and await _arrival_stop_is_current(
                                    scanner, camera, evidence, maximum_error_pixels=maximum_error_pixels)):
                                control_stopped = await self._attention_stopped_control(job, borrowed_control)
                                # Native status can wait; recheck image age and the same Stop afterward.
                                arrival_reused = control_stopped and await _arrival_stop_is_current(
                                    scanner, camera, evidence, maximum_error_pixels=maximum_error_pixels)
                        except asyncio.CancelledError as error:
                            # Cancelling the shortcut must still run the mandatory Stop path.
                            cleanup_cancellation = error
                            failure_code = "capture_cancelled"
                        if not arrival_reused:
                            control_stopped = False
                            if await scanner._stop():
                                status_task = asyncio.create_task(self._attention_stopped_control(
                                    job, borrowed_control))
                                try:
                                    await scanner._confirm_stop(reuse_accepted=True)
                                finally:
                                    control_stopped = await asyncio.shield(status_task)
                    else:
                        await scanner._confirm_stop()
                    job["stop_confirmed"] = scanner.physical_state == "stopped" and control_stopped
                    scanner.checkpoint["final_stop_reused"] = arrival_reused
                    if borrowed_control is not None and job["stop_confirmed"] and not failure_code and not arrival_reused:
                        # Cleanup observes newer video; arrival must refer to that
                        # stopped frame, not to the frame preceding Stop.
                        try:
                            scanner.last_frame = await scanner.refresh_stopped_frame()
                            located = await navigator.locate()
                            measured = await navigator._target_measurement(np.asarray(evidence["target_ray"]), located)
                            if not measured or measured["center_error_pixels"] > maximum_error_pixels:
                                raise PanoramaCaptureError("visual_arrival_unconfirmed")
                            evidence = {**evidence, "measurement": measured,
                                "capture_evidence": scanner.last_frame.get("capture_evidence", {}),
                                "arrival_delivery": {"localized_at": time.time()}}
                        except PanoramaCaptureError as error:
                            failure_code = error.code
                # Attention saves the same endpoint as JPEG below; encoding a
                # second full-resolution PNG here can expire its arrival proof.
                if scanner.last_frame is not None and borrowed_control is None:
                    try:
                        await asyncio.to_thread(_write_image, directory / "last-observed-frame.png", scanner.last_frame["image"])
                        scanner.checkpoint["last_observed_frame"] = {
                            "path": str(directory / "last-observed-frame.png"),
                            "capture_evidence": scanner.last_frame.get("capture_evidence", {}),
                        }
                    except Exception:
                        scanner.checkpoint["last_observed_frame"] = {"unavailable": True}
                if borrowed_control is None:
                    job["_navigation_checkpoint"] = scanner.checkpoint
                else:
                    job["_attention_navigation_trace"] = {key: scanner.checkpoint.get(key, [])
                        for key in ("navigation_commands", "navigation_observation_timings")}
                    job["_attention_navigation_trace"].update(preparation=preparation,
                        preparation_elapsed_ms=preparation_elapsed_ms, timing_clock="process_monotonic",
                        final_stop_reused=scanner.checkpoint.get("final_stop_reused") is True,
                        finished_at=time.time(), error_code=failure_code, target=target)
                    job["_attention_navigation_history"] = [
                        *job.get("_attention_navigation_history", []),
                        job["_attention_navigation_trace"],
                    ][-8:]
                if response_binding is not None and navigator is not None:
                    if (not failure_code and job.get("stop_confirmed")
                            and getattr(navigator, "commands", 0) > 0 and getattr(navigator, "response", None)):
                        # shortcut: reuse expires after five minutes; longer reuse needs a durable actuator calibration.
                        self._attention_responses[job["camera_id"]] = {
                            "binding": response_binding, "observed_monotonic": response_observed or time.monotonic(),
                            "response": {name: copy.deepcopy(getattr(navigator, name)) for name in response_fields},
                        }
                    elif (not job.get("stop_confirmed")
                            or failure_code not in {None, "capture_cancelled", "following_observation_expired"}):
                        getattr(self, "_attention_responses", {}).pop(job["camera_id"], None)
                return_checkpoint = job.get("_navigation_checkpoint") or {}
                job["navigation"] = {"phase": "failed" if failure_code else "complete", "error_code": failure_code, "physical_state": "restored" if restored and job.get("stop_confirmed") else "stopped" if job.get("stop_confirmed") else scanner.physical_state,
                                     "can_return": bool((return_checkpoint.get("return") or return_checkpoint.get("visual_original_ray")) and return_checkpoint.get("initial_path"))}
                self._save(job)
            finally:
                try:
                    await camera.close()
                    if "arrival_delivery" in evidence:
                        evidence["arrival_delivery"]["capture_closed_at"] = time.time()
                finally:
                    self._navigation_scanners.pop(job["id"], None)
                    if self.busy.get(job["camera_id"]) == job["id"]:
                        self.busy.pop(job["camera_id"], None)
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if job.get("stop_confirmed") is not True:
            raise _error("stop_unconfirmed", "The camera stop could not be confirmed")
        if failure_code:
            raise _error(failure_code, "The stopped visual arrival could not be confirmed")
        filename = f"check-{uuid.uuid4().hex}.jpg"
        okay, encoded = await asyncio.to_thread(cv2.imencode, ".jpg", scanner.last_frame["image"])
        if not okay:
            raise _error("capture_write_failed", "The stopped camera image could not be saved")
        self._image_write(job, filename, encoded.tobytes())
        image_url = self._image_url(job, filename)
        if "arrival_delivery" in evidence:
            evidence["arrival_delivery"]["image_saved_at"] = time.time()
        return image_url, evidence

    async def _attention_stopped_control(self, job: dict, borrowed_control: dict) -> bool:
        """Refresh native idle evidence alongside the visual Stop window."""
        for attempt in range(6):
            try:
                snapshot = await self.services.call("cameras.control.snapshot",
                    camera_id=job["camera_id"], source_id=job["source_id"], refresh_physical=True)
            except Exception:
                return False
            owned = snapshot.get("active_lease") or {}
            if (owned.get("lease_id") != borrowed_control["lease_id"]
                    or owned.get("fence") != borrowed_control["fence"]
                    or snapshot.get("automation_ready") is not True):
                return False
            if snapshot.get("geometry_safe") is True and snapshot.get("motion_state") == "stable":
                return True
            if attempt < 5:
                await asyncio.sleep(.25)
        return False

    async def return_framing(self, request: Request, camera_id: str, job_id: str, body: Revision) -> dict[str, Any]:
        self._authorize(request, camera_id, control=True)
        job = self._job(camera_id, job_id, body.revision)
        await self._current(job, request)
        checkpoint = job.get("_navigation_checkpoint") or {}
        if not (checkpoint.get("return") or checkpoint.get("visual_original_ray")):
            raise _error("return_reference_unavailable", "The original camera framing is unavailable")
        image_url, _ = await self._visual_operation(request, job, None)
        return {**await self._public(job), "aim_image_url": image_url}

    async def check(
        self, request: Request, camera_id: str, job_id: str, body: Check
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True, control=True)
        job = self._job(camera_id, job_id, body.revision)
        await self._current(job, request)
        if (await self._public(job))["active"] and not job.get("source_panorama"):
            raise _error(
                "active_mapping_immutable",
                "Create a new calibration before changing the active mapping",
            )
        point = next(
            (
                point
                for point in job["points"]
                if point["id"] == body.point_id
            ),
            None,
        )
        if point is None:
            raise _error("unknown_point", "Choose a mapped place", 422)
        target = self._target(job, point["world"])
        # Invalidate earlier evidence before a repeat, even when this attempt fails.
        job["checks"] = [check for check in job["checks"] if check["point_id"] != body.point_id]
        self._save(job)
        image_url, evidence = await self._point_and_capture(request, job, target)
        self._job(camera_id, job_id, body.revision)
        await self._current(job, request)
        job["checks"].append(
            {
                "id": uuid.uuid4().hex,
                "point_id": body.point_id,
                "revision": body.revision,
                "image_url": image_url,
                "result": None,
                "evidence": evidence,
            }
        )
        self._save(job)
        return await self._public(job)

    async def check_result(
        self, request: Request, camera_id: str, job_id: str, body: CheckResult
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        job = self._job(camera_id, job_id, body.revision)
        await self._current(job, request)
        if (await self._public(job))["active"] and not job.get("source_panorama"):
            raise _error(
                "active_mapping_immutable",
                "Create a new calibration before changing the active mapping",
            )
        if camera_id in self.busy:
            raise _error("camera_busy", "Wait for the camera verification to finish")
        check = next(
            (
                check
                for check in job["checks"]
                if check["id"] == body.check_id
                and check["point_id"] == body.point_id
                and check["revision"] == body.revision
            ),
            None,
        )
        if not check or check.get("evidence", {}).get("verified") is not True:
            raise _error(
                "physical_check_required",
                "Point the camera and wait for its fresh image before confirming",
            )
        async with self.lock:
            self._job(camera_id, job_id, body.revision)
            if ((await self._public(job))["active"] and not job.get("source_panorama")) or camera_id in self.busy:
                raise _error(
                    "calibration_changed", "The calibration changed before this result was saved"
                )
            if not any(candidate is check for candidate in job["checks"]):
                raise _error(
                    "physical_check_required", "The camera check changed; review its current image"
                )
            check.update(
                result=body.result,
                observed_image=body.observed_image.model_dump() if body.observed_image else None,
            )
            self._save(job)
        return await self._public(job)

    async def activate(
        self, request: Request, camera_id: str, job_id: str, body: Revision
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        job = self._job(camera_id, job_id, body.revision)
        await self._current(job, request)
        if (
            camera_id in self.busy
            or not job.get("solution")
            or job["solution"]["quality"]["status"] != "ready"
        ):
            raise _error("mapping_not_ready", "Complete the calibration before activating it")
        if not self._map_usable(job):
            raise _error(
                "mapping_not_ready",
                "The mapping must have a usable solution before activation",
            )
        async with self.lock:
            self._job(camera_id, job_id, body.revision)
            await self._current(job, request)
            if camera_id in self.busy or not self._map_usable(job):
                raise _error(
                    "mapping_not_ready",
                    "The mapping changed before activation",
                )
            previous = await self._pointer(job)
            pointer = {
                "job_id": job_id,
                "revision": job["revision"],
                "source_id": job["source_id"],
                "status": "ready",
            }
            if previous != pointer and (previous or None) != job.get("_base_mapping"):
                raise _error(
                    "activation_conflict",
                    "A newer mapping was activated after this draft was created",
                )
            changes: dict[str, Any] = {"panorama_mapping": pointer}
            if previous and previous != pointer:
                changes["previous_panorama_mapping"] = previous
            try:
                await self.store.patch_element_props(
                    composition_id=job["composition_id"],
                    element_id=job["element_id"],
                    changes=changes,
                    expected={"panorama_mapping": previous or None},
                )
            except (KeyError, ValueError):
                raise _error(
                    "activation_conflict",
                    "The composition changed before activation; reload and review it",
                ) from None
        return await self._public(job)

    @staticmethod
    def _map_validated(job: dict[str, Any]) -> bool:
        return (job.get("state") == "ready" and len(job.get("points", [])) >= 4
                and isinstance(job.get("solution"), dict)
                and preview_panorama_mapping(job["solution"])["eligible"])

    @classmethod
    def _map_usable(cls, job: dict[str, Any]) -> bool:
        return cls._map_validated(job)

    @staticmethod
    def _validated(job: dict[str, Any]) -> bool:
        if (
            job["state"] != "ready"
            or not job.get("solution")
            or job["solution"]["quality"]["status"] != "ready"
        ):
            return False
        valid_checks = {point["id"] for point in job["points"]}
        confirmed = {
            check["point_id"]
            for check in job["checks"]
            if check["point_id"] in valid_checks
            and check["revision"] == job["revision"]
            and check["result"] == "correct"
            and check.get("evidence", {}).get("verified") is True
            and (
                not job.get("source_panorama")
                or check.get("evidence", {}).get("kind") == "visual_aim_verified"
            )
        }
        return (
            len(confirmed) >= 2
            and confirmed <= valid_checks
            and all(
                check["result"] == "correct"
                for check in job["checks"]
                if check["revision"] == job["revision"]
            )
        )

    async def get_active(
        self, *, camera_id: str, source_id: str = "", composition_id: str = "", element_id: str = ""
    ) -> dict[str, Any]:
        candidates = []
        config = await self.store.get_config()
        camera_elements = []
        for composition in config.compositions:
            for element in composition.elements:
                if element.props.get("camera_id") != camera_id:
                    continue
                camera_elements.append((composition, element))
                if composition_id and composition.id != composition_id:
                    continue
                if element_id and element.id != element_id:
                    continue
                pointer = element.props.get("panorama_mapping")
                if pointer is not None:
                    pointer = pointer if isinstance(pointer, dict) else {}
                    candidates.append(
                        (pointer, self.jobs.get(pointer.get("job_id")) if isinstance(pointer.get("job_id"), str) else None, composition, element)
                    )
        if not candidates:
            return {"applies": False}
        if len(candidates) != 1:
            return {"applies": True, "reason": "ambiguous_panorama_mapping"}
        pointer, job, composition, element = candidates[0]
        if (
            not job
            or job["composition_id"] != composition.id
            or job["element_id"] != element.id
            or job["camera_id"] != camera_id
            or pointer.get("status") != "ready"
            or type(pointer.get("revision")) is not int
        ):
            return {"applies": True, "reason": "panorama_reference_invalid"}
        if sum(item.id == job["element_id"] for _, item in camera_elements) != 1:
            return {"applies": True, "reason": "ambiguous_composition"}
        camera = get_camera_device(
            config.settings.extensions.get("com.toposync.cameras", {}), camera_id=camera_id
        )
        if camera is None:
            return {"applies": True, "reason": "unknown_camera"}
        if not source_id:
            source = get_camera_source(camera, enabled_only=True)
            source_id = source.get("id", "") if source else ""
        if not source_id or job["source_id"] != source_id:
            return {"applies": True, "reason": "panorama_source_mismatch"}
        if (
            pointer.get("revision") != job["revision"]
            or pointer.get("source_id") != job["source_id"]
        ):
            return {"applies": True, "reason": "panorama_revision_mismatch"}
        if not self._map_usable(job):
            return {"applies": True, "reason": "panorama_validation_required"}
        try:
            self._validate_current(job, composition, element, camera)
        except HTTPException as error:
            return {"applies": True, "reason": error.detail["code"]}
        return {"applies": True, "job": await self._public(job, pointer=pointer)}

    async def mapping_summary(self, *, camera_id: str, composition_id: str, element: Any) -> dict[str, Any]:
        """Persisted configuration only; never opens a stream or contacts a device."""
        pointer = element.props.get("panorama_mapping")
        source_id = pointer.get("source_id", "") if isinstance(pointer, dict) else ""
        active = await self.get_active(camera_id=camera_id, composition_id=composition_id,
                                       element_id=element.id, source_id=source_id)
        job = active.get("job")
        reason = active.get("reason")
        draft = None
        if pointer is None:
            drafts = [item for item in self.jobs.values() if item["camera_id"] == camera_id
                      and item["composition_id"] == composition_id and item["element_id"] == element.id]
            draft = max(drafts, key=lambda item: item["updated_at"]) if drafts else None
            if draft:
                source_id = draft["source_id"]
                try:
                    await self._current(draft)
                except HTTPException as error:
                    reason = error.detail["code"]
        reference = "available" if (job or draft or {}).get("source_panorama") and not reason else "absent"
        if reference == "available":
            binding = (job or draft)["source_panorama"]
            artifact = self._read(self.root.parent / "source-panorama" / "artifacts" / binding["id"] / "artifact.json") or {}
            reference = "partial" if artifact.get("status") == "partial" else "available"
        if not job and not draft:
            configuration = await self.store.get_config()
            camera = get_camera_device(configuration.settings.extensions.get("com.toposync.cameras", {}), camera_id=camera_id)
            sources = iter_camera_sources(camera, kind="video", enabled_only=True) if camera else []
            references = []
            for source in sources:
                if source_id and source["id"] != source_id:
                    continue
                metadata = source.get("metadata")
                panorama = metadata.get("panorama") if isinstance(metadata, dict) else None
                saved = panorama.get("active") if isinstance(panorama, dict) else None
                references.append((source, saved))
            references = [(source, saved) for source, saved in references if isinstance(saved, dict)]
            if len(references) > 1:
                reference = "incompatible"
                reason = reason or "ambiguous_panorama_source"
            elif references:
                source, saved = references[0]
                source_id = source["id"]
                try:
                    self._source_artifact(camera, source, saved.get("artifact_id"), saved.get("revision"))
                    artifact = self._read(self.root.parent / "source-panorama" / "artifacts" / saved["artifact_id"] / "artifact.json") or {}
                    reference = "partial" if artifact.get("status") == "partial" else "available"
                except HTTPException as error:
                    reference = "incompatible"
                    reason = reason or error.detail["code"]
        return {
            "source_id": source_id or None,
            "revision": job["revision"] if job else pointer.get("revision") if isinstance(pointer, dict) else draft["revision"] if draft else None,
            "reference": reference,
            "mapping": "active" if job else "draft" if draft and not reason else "review_required" if reason or active["applies"] or element.props.get("calibrated_views") or element.props.get("control_point_sets") else "absent",
            "positioning": "not_applicable" if ((job or draft or {}).get("source_panorama") or {}).get("geometry", {}).get("projection") == "image_plane" else "available" if job and job["permissions"]["aim_enabled"] else "unqualified",
            "reason": reason,
            "control_points_pairs": len((job or draft or {}).get("points", [])),
        }

    async def localize_frame(
        self, *, camera_id: str, source_id: str, job_id: str, revision: int,
        image: np.ndarray, capture_evidence: dict[str, Any], image_geometry: dict | None = None,
    ) -> dict[str, Any]:
        """Internal frame consumer; no device I/O or latest-pose substitution."""
        job = self._job(camera_id, job_id, revision)
        binding = job.get("source_panorama")
        if not binding or job["source_id"] != source_id:
            return {"status": "unlocalized", "reason": "panorama_source_mismatch"}
        await self._current(job)
        localizer = await self.reference_localizer(camera_id, source_id, binding)
        located = await asyncio.to_thread(localizer.locate, image, capture_evidence, image_geometry)
        return {**located, "source_artifact_id": binding["id"],
                "geometry_digest": binding["geometry"]["model_digest"], "source_id": source_id, "revision": revision,
                "_coverage_mask": localizer.coverage_mask}

    async def resolve_target(self, *, camera_id: str, source_id: str, composition_id: str,
                             target: dict[str, Any], element_id: str = "") -> dict[str, Any]:
        """Resolve composition intent without movement or guessing a preset."""
        active = await self.get_active(camera_id=camera_id, source_id=source_id,
                                       composition_id=composition_id, element_id=element_id)
        if not active.get("job"):
            return {"status": "unavailable", "reason": active.get("reason", "active_mapping_required")}
        job = self.jobs[active["job"]["id"]]
        if job["solution"].get("type") == "panorama_image_plane_v1":
            return {"status": "unavailable", "reason": "movement_not_applicable"}
        world = (target.get("world") or target.get("world_anchor")) if isinstance(target, dict) else None
        if world is None and isinstance(target, dict) and isinstance(target.get("world_envelope"), dict):
            world = target["world_envelope"].get("center")
        if not isinstance(world, dict):
            return {"status": "unavailable", "reason": "panorama_world_target_required"}
        try:
            if any(type(world.get(axis)) not in (int, float) or not math.isfinite(world[axis]) for axis in ("x", "z")):
                raise ValueError("Invalid world point")
            if mapping_extrapolated(job["solution"], world["x"], world["z"]):
                raise _error("outside_validated_region", "Choose a place inside the mapped region", 422)
            ray = map_world_to_ray(job["solution"], world["x"], world["z"])
            if ray is None:
                raise ValueError("World point has no ray")
            resolved = {"ray": list(ray)}
        except (HTTPException, KeyError, TypeError, ValueError) as error:
            reason = error.detail.get("code") if isinstance(error, HTTPException) and isinstance(error.detail, dict) else "invalid_target"
            return {"status": "unavailable", "reason": reason}
        return {"status": "ready", "camera_id": camera_id, "source_id": job["source_id"],
                "composition_id": composition_id, "element_id": job["element_id"],
                "job_id": job["id"], "revision": job["revision"], "target": resolved,
                "positioning_available": active["job"]["permissions"]["aim_enabled"],
                "attention_qualified": self._attention_qualified(job)}

    @staticmethod
    def _attention_qualified(job: dict) -> bool:
        """Software delivery never promotes a device to physical qualification."""
        binding = job.get("source_panorama") or {}
        expected = {"job_id": job.get("id"), "revision": job.get("revision"),
                    "source_identity": job.get("_source_digest"),
                    "composition_digest": job.get("_composition_digest"),
                    "artifact_id": binding.get("id"), "artifact_revision": binding.get("revision"),
                    "geometry_digest": (binding.get("geometry") or {}).get("model_digest")}
        qualification = job.get("_attention_qualification")
        return bool(all(value is not None for value in expected.values()) and isinstance(qualification, dict)
                    and qualification.get("arrival_verified") is True and qualification.get("return_verified") is True
                    and type(qualification.get("revision")) is int and type(qualification.get("artifact_revision")) is int
                    and qualification == {**expected, "arrival_verified": True, "return_verified": True})

    async def qualify_attention(self, request: Request, camera_id: str, job_id: str,
                                body: QualifyAttention) -> dict:
        """Produce qualification only from a supervised, visually verified round trip."""
        self._authorize(request, camera_id, write=True, control=True)
        job = self._job(camera_id, job_id, body.revision)
        async with self.reference_coordinator.hold(camera_id, job["source_id"]):
            await self._current(job, request)
            active = await self.get_active(camera_id=camera_id, source_id=job["source_id"],
                composition_id=job["composition_id"], element_id=job["element_id"])
            if not active.get("job") or active["job"].get("id") != job_id:
                raise _error("active_mapping_required", "Qualify the active mapping only")
            if not job.get("source_panorama") or job["solution"].get("type") != "panorama_ray_plane_v1":
                raise _error("movement_not_applicable", "A visual panoramic mapping is required")
            home = self._target(job, body.home.model_dump())
            target = self._target(job, body.target.model_dump())
            if np.linalg.norm(np.asarray(home["ray"]) - target["ray"]) < 0.03:
                raise _error("qualification_targets_too_close", "Choose a distinct test target", 422)
            if camera_id in self.busy:
                raise _error("camera_busy", "Another camera operation is active")
            navigation = job.get("_navigation_checkpoint") or {}
            if navigation.get("return") or navigation.get("pending_returns") or navigation.get("visual_original_ray"):
                raise _error("return_cleanup_required", "Restore the pending camera return before qualification")
            job.pop("_attention_qualification", None)
            job["_attention_trial"] = {"status": "running", "stages": []}
            self._save(job)
            try:
                image_url, evidence = await self._visual_operation_with_pinned_reference(
                    request, job, target, qualification_home=home)
                stages = evidence.get("stages", [])
                if (evidence.get("verified") is not True or job.get("stop_confirmed") is not True
                        or [stage.get("stage") for stage in stages] != ["home", "arrival", "return"]):
                    raise _error("attention_arrival_unconfirmed", "The physical round trip was not verified")
                for stage, limit in zip(stages, (3, 12, 3)):
                    observed = stage.get("evidence", {})
                    error = observed.get("measurement", {}).get("center_error_pixels")
                    if (observed.get("verified") is not True or observed.get("kind") != "visual_aim_verified"
                            or type(error) not in (int, float) or not 0 <= error <= limit):
                        raise _error("attention_arrival_unconfirmed", "The physical round trip was not verified")
                commands = [stage["evidence"].get("commands") for stage in stages]
                if (not all(type(count) is int for count in commands)
                        or not 0 <= commands[0] < commands[1] < commands[2]):
                    raise _error("attention_motion_unconfirmed", "The trial must observe outward and return movements")
                await self._current(job, request)
                binding = job["source_panorama"]
                job["_attention_qualification"] = {
                    "job_id": job["id"], "revision": job["revision"],
                    "source_identity": job["_source_digest"], "composition_digest": job["_composition_digest"],
                    "artifact_id": binding["id"], "artifact_revision": binding["revision"],
                    "geometry_digest": binding["geometry"]["model_digest"],
                    "arrival_verified": True, "return_verified": True,
                }
                job["_attention_trial"] = {"status": "verified", "stages": stages,
                    "image_url": image_url, "verified_at": time.time()}
                self._save(job)
                return {"qualified": self._attention_qualified(job), "trial": job["_attention_trial"]}
            except BaseException:
                job.pop("_attention_qualification", None)
                job["_attention_trial"]["status"] = "failed"
                self._save(job)
                raise

    async def navigate_attention(self, *, destination: dict, lease_id: str, fence: int,
                                 command_id: str, returning: bool = False,
                                 target_provider: Callable[[], dict] | None = None) -> dict:
        if (not isinstance(destination, dict) or not isinstance(lease_id, str) or not lease_id.strip()
                or type(fence) is not int or fence < 1 or not isinstance(command_id, str) or not command_id.strip()
                or type(returning) is not bool
                or any(not isinstance(destination.get(key), str) or not destination[key].strip() for key in
                    ("camera_id", "source_id", "composition_id", "element_id", "job_id"))
                or type(destination.get("revision")) is not int or destination["revision"] < 1):
            raise _error("invalid_target", "The attention command binding is invalid", 422)
        camera_id, source_id = destination.get("camera_id"), destination.get("source_id")
        async with self.reference_coordinator.hold(camera_id, source_id):
            active = await self.get_active(camera_id=camera_id, source_id=source_id,
                composition_id=destination.get("composition_id"), element_id=destination.get("element_id"))
            public = active.get("job")
            if not public or any(public.get(key) != destination.get(key) for key in
                ("revision", "camera_id", "source_id", "composition_id", "element_id")) or public.get("id") != destination.get("job_id"):
                raise _error("panorama_reference_changed", "The attention mapping changed")
            job = self.jobs[public["id"]]
            if not self._attention_qualified(job):
                raise _error("attention_physical_qualification_required", "Physical attention qualification is required")
            ray = np.asarray(destination.get("ray"), dtype=float)
            if ray.shape != (3,) or not np.isfinite(ray).all() or abs(np.linalg.norm(ray) - 1) > 1e-6:
                raise _error("invalid_target", "The attention ray is invalid", 422)
            world = map_ray_to_world(job["solution"], ray)
            if world is None or mapping_extrapolated(job["solution"], *world):
                raise _error("outside_validated_region", "The attention target is outside the mapped region", 422)
            def current_ray() -> list[float]:
                from .panorama_capture import PanoramaCaptureError
                try:
                    updated = target_provider()
                except RuntimeError:
                    raise PanoramaCaptureError("following_observation_expired") from None
                if (not isinstance(updated, dict) or any(updated.get(key) != destination.get(key)
                        for key in ("camera_id", "source_id", "composition_id", "element_id", "job_id", "revision"))):
                    raise PanoramaCaptureError("panorama_reference_changed")
                vector = np.asarray(updated.get("ray"), dtype=float)
                if vector.shape != (3,) or not np.isfinite(vector).all() or abs(np.linalg.norm(vector) - 1) > 1e-6:
                    raise PanoramaCaptureError("invalid_target")
                point = map_ray_to_world(job["solution"], vector)
                if point is None or mapping_extrapolated(job["solution"], *point):
                    raise PanoramaCaptureError("outside_validated_region")
                return vector.tolist()
            if target_provider is not None and (returning or not callable(target_provider)):
                raise _error("invalid_target", "Return navigation cannot follow a moving target", 422)
            snapshot = await self.services.call("cameras.control.snapshot", camera_id=camera_id,
                source_id=source_id, refresh_physical=False)
            owned = snapshot.get("active_lease") or {}
            if (owned.get("lease_id") != lease_id or owned.get("fence") != fence
                    or owned.get("owner_kind") != "automation" or snapshot.get("automation_ready") is not True):
                raise _error("control_lost", "Attention no longer owns exclusive camera control")
            _, evidence = await self._visual_operation_with_pinned_reference(None, job, {"ray": ray.tolist()},
                borrowed_control={"lease_id": lease_id, "fence": fence}, maximum_error_pixels=3 if returning else 12,
                **({"target_provider": current_ray} if target_provider is not None else {}))
            final = await self.services.call("cameras.control.snapshot", camera_id=camera_id,
                source_id=source_id, refresh_physical=False)
            owned = final.get("active_lease") or {}
            if (owned.get("lease_id") != lease_id or owned.get("fence") != fence
                    or final.get("automation_ready") is not True or job.get("stop_confirmed") is not True):
                raise _error("control_lost", "The stopped attention result no longer owns control")
            reached = {**destination, "ray": evidence["target_ray"]} if target_provider is not None else destination
            result = {"destination": reached, "command_id": command_id, "lease_id": lease_id,
                    "fence": fence, "motion_epoch": final.get("motion_epoch"), "stop_confirmed": True,
                    "returning": returning, "evidence": evidence}
            job["_attention_navigation_result"] = {**result, "observed_at": time.time(),
                "control": {key: final.get(key) for key in ("geometry_safe", "motion_state", "physical_updated_at")}}
            self._save(job)
            return result

    def native_references(self, camera_id: str, source_id: str, binding: dict) -> list[dict]:
        """Read separate native points only for this exact panorama geometry."""
        from .panorama_navigation import MAXIMUM_LIVE_CENTER_ERROR_PIXELS
        if not _IDENTIFIER.fullmatch(str(binding.get("id", ""))):
            return []
        directory = self.root.parent / "source-panorama" / "artifacts" / binding["id"] / "native-references"
        references = []
        for path in sorted(directory.glob("*/reference.json")):
            record = self._read(path) or {}
            if not _IDENTIFIER.fullmatch(path.parent.name):
                continue
            if (record.get("schema_version") != 1 or record.get("status") != "ready"
                    or record.get("camera_id") != camera_id or record.get("source_id") != source_id
                    or record.get("artifact_id") != binding["id"]
                    or record.get("artifact_revision") != binding["revision"]
                    or record.get("geometry") != binding["geometry"]
                    or record.get("source_identity") != binding["source_identity"]
                    or record.get("id") != path.parent.name):
                continue
            validation = record.get("validation") or {}
            if not isinstance(validation, dict) or not isinstance(validation.get("measurement"), dict):
                continue
            error = (validation.get("measurement") or {}).get("center_error_pixels")
            if (record.get("physical_state") != "stopped" or validation.get("state") != "observed"
                    or type(error) not in (int, float) or not 0 <= error <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS):
                continue
            destination = record.get("destination")
            if (not isinstance(destination, dict) or destination.get("role") != "reference"
                    or destination.get("owner_id") != f"native-{record['id']}"
                    or destination.get("preserve_zoom") is not True):
                continue
            try:
                ray = np.asarray(record["ray"], dtype=float)
                if ray.shape != (3,) or not np.isfinite(ray).all() or abs(np.linalg.norm(ray) - 1) > 1e-6:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            references.append(record)
        return references

    def invalidate_native_reference(self, record: dict, reason: str) -> None:
        """A changed device point needs preparation again, not another blind recall."""
        if not all(_IDENTIFIER.fullmatch(str(record.get(key, ""))) for key in ("id", "artifact_id")):
            return
        path = (self.root.parent / "source-panorama" / "artifacts" / record["artifact_id"]
                / "native-references" / record["id"] / "reference.json")
        current = self._read(path)
        if current == record:
            self._atomic(path, {**current, "status": "unverified", "invalidated_reason": reason,
                                "revision": current["revision"] + 1})

    async def list_native_references(self, request: Request, camera_id: str, source_id: str,
                                     artifact_id: str, revision: int) -> dict:
        sources = self.app.state.camera_source_panorama
        sources._authorize(request, camera_id)
        if not _IDENTIFIER.fullmatch(artifact_id) or revision < 1:
            raise _error("native_reference_unavailable", "Referência panorâmica inválida.")
        await sources._context(camera_id, source_id, request)
        artifact = self._read(self.root.parent / "source-panorama" / "artifacts" / artifact_id / "artifact.json") or {}
        if (artifact.get("id") != artifact_id or artifact.get("camera_id") != camera_id
                or artifact.get("source_id") != source_id or artifact.get("revision") != revision):
            raise _error("native_reference_unavailable", "Referência panorâmica inválida.")
        directory = self.root.parent / "source-panorama" / "artifacts" / artifact_id / "native-references"
        records = []
        for path in sorted(directory.glob("*/reference.json")):
            record = self._read(path) or {}
            identifier = path.parent.name
            if (not _IDENTIFIER.fullmatch(identifier) or record.get("id") != identifier
                    or record.get("camera_id") != camera_id or record.get("source_id") != source_id
                    or record.get("artifact_id") != artifact_id or record.get("status") == "retired"):
                continue
            active = self._native_preparations.get(identifier)
            running = (active is not None and active[0] == camera_id and active[1].source_id == source_id
                       and active[1].artifact_id == artifact_id and active[1].revision == revision
                       and not active[2].done())
            status = ("stopping" if f"native-{identifier}" in self.cancelled else "preparing") if running else (
                "unverified")
            # Device tokens, native coordinates and ownership records remain
            # private. A stale 'preparing' journal is never a running operation.
            records.append({"id": identifier, "status": status, "created_at": record.get("created_at"),
                            "physical_state": record.get("physical_state", "unknown"),
                            "cleanup_confirmed": record.get("cleanup_confirmed") is True})
        known = {record["id"] for record in records}
        for identifier, (owner_camera, body, task) in self._native_preparations.items():
            if (identifier not in known and owner_camera == camera_id and body.source_id == source_id
                    and body.artifact_id == artifact_id and body.revision == revision and not task.done()):
                records.append({"id": identifier,
                                "status": "stopping" if f"native-{identifier}" in self.cancelled else "queued",
                                "physical_state": "unknown", "cleanup_confirmed": False})
        return {"references": records}

    async def stop_native_reference(self, request: Request, camera_id: str, body: PrepareNativeReference) -> dict:
        self.app.state.camera_source_panorama._authorize(request, camera_id, control=True, write=True)
        active = self._native_preparations.get(body.preparation_id)
        if active is None:
            return {"id": body.preparation_id, "status": "not_running"}
        if active[:2] != (camera_id, body):
            raise _error("native_reference_unavailable", "Preparação indisponível.")
        self.cancelled.add(f"native-{body.preparation_id}")
        return {"id": body.preparation_id, "status": "stopping"}

    async def prepare_native_reference(self, request: Request | None, camera_id: str, body: PrepareNativeReference, *, generated_job: dict | None = None) -> dict:
        self.app.state.camera_source_panorama._authorize(request, camera_id, control=True, write=True)
        raise _error("native_reference_retired", "A aproximação agora é gerada pela aquisição da panorâmica.", 410)

    async def remove_native_reference(self, request: Request, camera_id: str, body: PrepareNativeReference) -> dict:
        """Retire only this preparation's device resource; keep its audit record."""
        sources = self.app.state.camera_source_panorama
        sources._authorize(request, camera_id, control=True, write=True)
        async with self.reference_coordinator.hold(camera_id, body.source_id):
            await sources._context(camera_id, body.source_id, request)
            directory = (self.root.parent / "source-panorama" / "artifacts" / body.artifact_id
                         / "native-references" / body.preparation_id)
            path = directory / "reference.json"
            record = self._read(path)
            if (not record or record.get("camera_id") != camera_id or record.get("source_id") != body.source_id
                    or record.get("artifact_id") != body.artifact_id or record.get("artifact_revision") != body.revision
                    or record.get("id") != body.preparation_id):
                raise _error("native_reference_unavailable", "Ponto de referência indisponível.")
            if record.get("cleanup_confirmed") is True:
                self._atomic(path, {**record, "status": "retired", "revision": record["revision"] + 1})
                return {"id": body.preparation_id, "status": "retired"}
            camera = self.camera_factory(services=self.services, camera_id=camera_id, source_id=body.source_id,
                                         settings={}, job_id=f"native-{body.preparation_id}", output_dir=directory)
            try:
                await camera.discover()
                # Cleanup verifies resource ownership and binding without moving the head.
                if record.get("destination"):
                    await camera.remove_return(record["destination"])
                self._atomic(path, {**record, "status": "retired", "cleanup_confirmed": True,
                                    "revision": record["revision"] + 1})
            finally:
                await camera.close()
            return {"id": body.preparation_id, "status": "retired"}

    async def reference_localizer(self, camera_id: str, source_id: str, binding: dict) -> Any:
        """Share immutable descriptors between calibration and live viewing."""
        if binding.get("geometry", {}).get("projection") == "image_plane":
            raise _error("movement_not_applicable", "This reference is a single image without camera positioning")
        key = (binding["id"], binding["geometry"]["model_digest"])
        async with self._localization_lock:
            from .processing.panorama_localization import PanoramaLocalizer

            localizer = self._localizers.get(key)
            if localizer is None:
                def load() -> PanoramaLocalizer:
                    root = self.root.parent / "source-panorama"
                    directory = root / "artifacts" / binding["id"]
                    artifact = self._read(directory / "artifact.json") or {}
                    source_job_id = artifact.get("_job_id", "")
                    if not isinstance(source_job_id, str) or not _IDENTIFIER.fullmatch(source_job_id):
                        raise ValueError("Reference capture unavailable")
                    source_directory = root / "jobs" / source_job_id
                    source_job = self._read(source_directory / "job.json") or {}
                    if source_job.get("camera_id") != camera_id or source_job.get("source_id") != source_id:
                        raise ValueError("Reference capture source changed")
                    photographs = source_job.get("_captures") or source_job.get("_checkpoint", {}).get("captures", [])
                    photographs = [
                        {**photo, "path": str(SourcePanoramaService._private_path(source_directory, photo["path"]))}
                        for photo in photographs
                    ]
                    model_path = SourcePanoramaService._private_path(directory, artifact["_files"]["model"])
                    model = self._read(model_path)
                    if not model or _digest(model) != key[1]:
                        raise ValueError("Reconstruction geometry changed")
                    prepared = PanoramaLocalizer(
                        model, photographs, model_directory=self.root.parent / "panorama-matching-models")
                    # Optional causal records select only the direction of a
                    # short live probe. Missing legacy diagnostics grant no gain
                    # model and must not prevent viewing the reference.
                    from .panorama_navigation import reference_probe_observations

                    checkpoint = source_job.get("_checkpoint") or {}
                    diagnostics = checkpoint.get("diagnostics") or {}
                    attempts = list(diagnostics.get("attempts") or [])
                    for photo in photographs:
                        replay = photo.get("observation_replay_id")
                        if isinstance(replay, str) and re.fullmatch(r"replay-accepted-[a-f0-9]{32}", replay):
                            record = self._read(source_directory / f"{replay}.json") or {}
                            if record.get("complete") is True and isinstance(record.get("attempt"), dict):
                                attempts.append(record["attempt"])
                    prepared.probe_observations = reference_probe_observations(
                        model, photographs, checkpoint.get("region_commands", []), attempts)
                    from .panorama_approach import geometry_digest
                    approach = artifact.get("approach")
                    approach = approach if isinstance(approach, dict) else {}
                    if (approach.get("geometry_digest") == geometry_digest(model)
                            and approach.get("source_identity") == artifact.get("_geometry_identity", artifact.get("_identity"))):
                        prepared.approach = approach
                    coverage_path = SourcePanoramaService._private_path(directory, artifact["_files"]["coverage"])
                    prepared.coverage_mask = cv2.imread(str(coverage_path), cv2.IMREAD_GRAYSCALE)
                    if prepared.coverage_mask is None:
                        raise ValueError("Reference coverage unavailable")
                    return prepared

                localizer = await asyncio.to_thread(load)
                self._localizers[key] = localizer
                while len(self._localizers) > 2:
                    self._localizers.popitem(last=False)
            self._localizers.move_to_end(key)
            return localizer

    async def aim(self, request: Request, camera_id: str, body: Aim) -> dict[str, Any]:
        self._authorize(request, camera_id, control=True)
        _, element, _ = await self._context(camera_id, body.element_id, request=request)
        pointer = element.props.get("panorama_mapping")
        if (
            not isinstance(pointer, dict)
            or not pointer.get("job_id")
            or not isinstance(pointer.get("revision"), int)
        ):
            raise _error(
                "active_mapping_required",
                "Activate a validated mapping before pointing from the composition",
            )
        job = self._job(camera_id, pointer["job_id"], pointer["revision"])
        await self._current(job, request)
        if job["solution"].get("type") == "panorama_image_plane_v1":
            raise _error("movement_not_applicable", "A referência estática não controla movimento.")
        if not self._validated(job):
            raise _error(
                "physical_checks_required", "The active mapping no longer has valid physical checks"
            )
        target = self._target(job, body.world.model_dump())
        image_url, _ = await self._point_and_capture(request, job, target)
        self._save(job)
        return {**await self._public(job), "aim_image_url": image_url}

    async def restore(self, request: Request, camera_id: str, body: Restore) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        composition, element, _ = await self._context(camera_id, body.element_id, request=request)
        current = element.props.get("panorama_mapping")
        previous = element.props.get("previous_panorama_mapping")
        if (
            not isinstance(current, dict)
            or current.get("job_id") != body.expected_job_id
            or current.get("revision") != body.expected_revision
        ):
            raise _error(
                "activation_conflict", "The active mapping changed; reload before restoring"
            )
        if (
            not isinstance(previous, dict)
            or not previous.get("job_id")
            or not isinstance(previous.get("revision"), int)
        ):
            raise _error(
                "previous_mapping_unavailable", "There is no previous validated mapping to restore"
            )
        job = self._job(camera_id, previous["job_id"], previous["revision"])
        if (
            job["composition_id"] != composition.id
            or job["element_id"] != element.id
            or not self._map_usable(job)
        ):
            raise _error(
                "previous_mapping_invalid", "The previous mapping is not valid for this composition"
            )
        await self._current(job, request)
        async with self.lock:
            if camera_id in self.busy:
                raise _error("camera_busy", "Wait for the camera operation before restoring")
            try:
                await self.store.patch_element_props(
                    composition_id=composition.id,
                    element_id=element.id,
                    changes={"panorama_mapping": previous, "previous_panorama_mapping": current},
                    expected={"panorama_mapping": current, "previous_panorama_mapping": previous},
                )
            except (KeyError, ValueError):
                raise _error(
                    "activation_conflict",
                    "The composition changed before restoration; reload and review it",
                ) from None
        return await self._public(job)

    async def delete(
        self, request: Request, camera_id: str, job_id: str, revision: int
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        async with self.lock:
            self._job(camera_id, job_id, revision)
            if job_id in self.tasks or job_id in self.busy.values():
                raise _error(
                    "camera_busy", "Finish the camera operation before discarding its draft"
                )
            navigation = self.jobs[job_id].get("_navigation_checkpoint") or {}
            if navigation.get("return") or navigation.get("pending_returns"):
                raise _error("return_cleanup_required", "Restore the camera and release its saved return before discarding this draft")
            config = await self.store.get_config()
            references = [
                element.props.get(name)
                for composition in config.compositions
                for element in composition.elements
                for name in ("panorama_mapping", "previous_panorama_mapping")
            ]
            if any(
                isinstance(reference, dict) and reference.get("job_id") == job_id
                for reference in references
            ):
                raise _error("mapping_in_use", "The active and previous mappings must be preserved")
            destination = self.root / job_id
            if (
                not _IDENTIFIER.fullmatch(job_id)
                or destination.is_symlink()
                or destination.resolve().parent != self.root.resolve()
            ):
                raise _error("invalid_artifact_path", "The draft artifact location is invalid")
            try:
                await asyncio.to_thread(shutil.rmtree, destination)
            except OSError:
                raise _error(
                    "discard_failed",
                    "The draft could not be fully discarded; retry the same operation",
                ) from None
            self.jobs.pop(job_id, None)
            self.cancelled.discard(job_id)
        return {"deleted": True, "id": job_id}

    async def shutdown(self) -> None:
        native_tasks = []
        for identifier, (_, _, task) in list(self._native_preparations.items()):
            self.cancelled.add(f"native-{identifier}")
            native_tasks.append(task)
        if native_tasks:
            # Cooperative cancellation follows the same physical Stop/finally
            # path as the button; never abandon cleanup during shutdown.
            await asyncio.gather(*native_tasks, return_exceptions=True)
        for job_id in set(self.leases) | set(self._navigation_scanners):
            self.cancelled.add(job_id)
            await self._finish(self.jobs[job_id])
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def register_panorama_routes(
    app: FastAPI,
    *,
    services: Any,
    authorize: Any,
    read_settings: Any,
    capture_snapshot: Any,
    reference_coordinator: PanoramaReferenceCoordinator | None = None,
) -> PanoramaService:
    service = PanoramaService(
        app,
        services,
        authorize,
        read_settings,
        capture_snapshot,
        reference_coordinator=reference_coordinator,
    )
    app.state.camera_panorama = service
    services.register("cameras.panorama.get_active", service.get_active)
    services.register("cameras.panorama.localize_frame", service.localize_frame)
    services.register("cameras.panorama.resolve_target", service.resolve_target)
    services.register("cameras.panorama.navigate_attention", service.navigate_attention)

    @app.get(_PREFIX)
    async def preflight(request: Request, camera_id: str, element_id: str) -> dict[str, Any]:
        return await service.preflight(request, camera_id, element_id)

    @app.post(_PREFIX)
    async def create(request: Request, camera_id: str, body: Create) -> dict[str, Any]:
        return await service.create(request, camera_id, body)

    @app.post(_PREFIX + "/aim")
    async def aim(request: Request, camera_id: str, body: Aim) -> dict[str, Any]:
        return await service.aim(request, camera_id, body)

    @app.post(_PREFIX + "/{job_id}/qualify-attention")
    async def qualify_attention(request: Request, camera_id: str, job_id: str, body: QualifyAttention) -> dict:
        return await service.qualify_attention(request, camera_id, job_id, body)

    @app.post(_PREFIX + "/native-reference")
    async def prepare_native_reference(request: Request, camera_id: str, body: PrepareNativeReference) -> dict:
        service._authorize(request, camera_id, control=True)
        raise _error("native_reference_retired", "A aproximação agora é gerada pela aquisição da panorâmica.", 410)

    @app.get(_PREFIX + "/native-reference")
    async def list_native_references(request: Request, camera_id: str, source_id: str,
                                     artifact_id: str, revision: int) -> dict:
        return await service.list_native_references(request, camera_id, source_id, artifact_id, revision)

    @app.post(_PREFIX + "/native-reference/stop")
    async def stop_native_reference(request: Request, camera_id: str, body: PrepareNativeReference) -> dict:
        return await service.stop_native_reference(request, camera_id, body)

    @app.delete(_PREFIX + "/native-reference")
    async def remove_native_reference(request: Request, camera_id: str, body: PrepareNativeReference) -> dict:
        return await service.remove_native_reference(request, camera_id, body)

    @app.post(_PREFIX + "/restore")
    async def restore(request: Request, camera_id: str, body: Restore) -> dict[str, Any]:
        return await service.restore(request, camera_id, body)

    @app.get(_PREFIX + "/{job_id}")
    async def get_job(request: Request, camera_id: str, job_id: str) -> dict[str, Any]:
        service._authorize(request, camera_id)
        return await service._public(service._job(camera_id, job_id))

    @app.delete(_PREFIX + "/{job_id}")
    async def delete(
        request: Request, camera_id: str, job_id: str, revision: int
    ) -> dict[str, Any]:
        return await service.delete(request, camera_id, job_id, revision)

    @app.get(_PREFIX + "/{job_id}/images/{filename}")
    async def get_image(
        request: Request, camera_id: str, job_id: str, filename: str
    ) -> FileResponse:
        service._authorize(request, camera_id)
        service._job(camera_id, job_id)
        if not _IMAGE_NAME.fullmatch(filename):
            raise _error("unknown_image", "This image is unavailable", 404)
        path = service.root / job_id / filename
        if not path.is_file():
            raise _error("unknown_image", "This image is unavailable", 404)
        return FileResponse(
            path,
            media_type="image/png" if filename.endswith(".png") else "image/jpeg",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.post(_PREFIX + "/{job_id}/capture")
    async def capture(
        request: Request, camera_id: str, job_id: str, body: Revision
    ) -> dict[str, Any]:
        return await service.start_capture(request, camera_id, job_id, body)

    @app.post(_PREFIX + "/{job_id}/cancel")
    async def cancel(
        request: Request, camera_id: str, job_id: str, body: Revision
    ) -> dict[str, Any]:
        return await service.cancel(request, camera_id, job_id, body)

    @app.put(_PREFIX + "/{job_id}/points")
    async def points(request: Request, camera_id: str, job_id: str, body: Points) -> dict[str, Any]:
        return await service.points(request, camera_id, job_id, body)

    @app.post(_PREFIX + "/{job_id}/preview")
    async def preview(request: Request, camera_id: str, job_id: str, body: Points) -> dict[str, Any]:
        return await service.points(request, camera_id, job_id, body, preview=True)

    @app.post(_PREFIX + "/{job_id}/check")
    async def check(request: Request, camera_id: str, job_id: str, body: Check) -> dict[str, Any]:
        return await service.check(request, camera_id, job_id, body)

    @app.post(_PREFIX + "/{job_id}/check-result")
    async def check_result(
        request: Request, camera_id: str, job_id: str, body: CheckResult
    ) -> dict[str, Any]:
        return await service.check_result(request, camera_id, job_id, body)

    @app.post(_PREFIX + "/{job_id}/activate")
    async def activate(
        request: Request, camera_id: str, job_id: str, body: Revision
    ) -> dict[str, Any]:
        return await service.activate(request, camera_id, job_id, body)

    @app.post(_PREFIX + "/{job_id}/return-framing")
    async def return_framing(request: Request, camera_id: str, job_id: str, body: Revision) -> dict[str, Any]:
        return await service.return_framing(request, camera_id, job_id, body)

    return service
