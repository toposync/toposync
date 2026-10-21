from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .settings import AiProfileConfig, AiProviderConfig
from .visual_state import VisualStateRequest, VisualStateResponse, strict_visual_json, validate_visual_response
from .visual_transport import BoundedVisualTransport, visual_completion_client


class AiProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EncodedImage:
    data_b64: str
    mime_type: str = "image/jpeg"
    width: int | None = None
    height: int | None = None


def _normalize_bbox01_values(value: Any) -> list[float] | None:
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(v) for v in value]
    except Exception:
        return None
    max_value = max(abs(x1), abs(y1), abs(x2), abs(y2))
    if max_value > 1.0 and max_value <= 1000.0:
        x1 /= 1000.0
        y1 /= 1000.0
        x2 /= 1000.0
        y2 /= 1000.0
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


class AiAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    provider_id: str
    provider_kind: str
    model: str
    ok: bool
    latency_ms: float = 0.0
    error: str = ""
    retry_at: float | None = None


def _normalize_region_aliases(data: dict[str, Any]) -> None:
    for field, aliases in (
        ("bbox01", ("bbox", "box", "bbox_2d", "box_2d", "bounding_box", "boundingBox", "coordinates")),
        ("confidence", ("score", "probability", "certainty", "confidence_score")),
        ("label", ("name", "class", "category", "description")),
    ):
        missing = data.get(field) in {None, ""} if field == "label" else data.get(field) is None
        if missing:
            for alias in aliases:
                value = data.get(alias)
                if value is not None and (field != "label" or value):
                    data[field] = value
                    break
        for alias in aliases:
            data.pop(alias, None)


class RegionDetection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bbox01: list[float]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    label: str = ""
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        data = dict(values)
        _normalize_region_aliases(data)
        data.pop("found", None)
        return data

    @field_validator("label", "reason", mode="before")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _validate_bbox(self) -> "RegionDetection":
        bbox = _normalize_bbox01_values(self.bbox01)
        if bbox is None:
            msg = "bbox01 must contain a valid [left, top, right, bottom] region"
            raise ValueError(msg)
        self.bbox01 = bbox
        return self


class RegionDetectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    bbox01: list[float] | None = None
    detections: list[RegionDetection] = Field(default_factory=list)
    label: str = ""
    reason: str = ""
    profile_id: str = ""
    provider_id: str = ""
    model: str = ""
    attempts: list[AiAttempt] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        data = dict(values)
        _normalize_region_aliases(data)
        if data.get("detections") is None:
            for key in ("regions", "objects", "items", "boxes", "bounding_boxes", "boundingBoxes"):
                if data.get(key) is not None:
                    data["detections"] = data.get(key)
                    break
        if data.get("detections") is not None and isinstance(data.get("detections"), dict):
            data["detections"] = [data["detections"]]
        detections = data.get("detections")
        if not detections and data.get("bbox01") is not None:
            data["detections"] = [
                {
                    "bbox01": data.get("bbox01"),
                    "confidence": data.get("confidence", 0.0),
                    "label": data.get("label", ""),
                    "reason": data.get("reason", ""),
                }
            ]
        elif isinstance(detections, list) and len(detections) == 1 and isinstance(detections[0], dict):
            confidence_present = any(key in detections[0] for key in ("confidence", "score", "probability", "certainty"))
            if not confidence_present and data.get("confidence") is not None:
                detections[0]["confidence"] = data.get("confidence")
            if not detections[0].get("label") and data.get("label"):
                detections[0]["label"] = data.get("label")
        if data.get("found") is None and (data.get("bbox01") is not None or data.get("detections")):
            data["found"] = True
        for key in ("regions", "objects", "items", "boxes", "bounding_boxes", "boundingBoxes"):
            data.pop(key, None)
        return data

    @field_validator("label", "reason", "profile_id", "provider_id", "model", mode="before")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _validate_bbox(self) -> "RegionDetectionResult":
        if self.detections:
            primary = max(enumerate(self.detections), key=lambda item: (item[1].confidence, -item[0]))[1]
            self.bbox01 = list(primary.bbox01)
            self.confidence = max(float(self.confidence), float(primary.confidence))
            if not self.label:
                self.label = primary.label
            self.found = True
            return self
        if self.bbox01 is None:
            self.found = False
            return self
        bbox = _normalize_bbox01_values(self.bbox01)
        if bbox is None:
            self.bbox01 = None
            self.found = False
            return self
        self.bbox01 = bbox
        self.detections = [
            RegionDetection(
                bbox01=bbox,
                confidence=float(self.confidence),
                label=self.label,
                reason=self.reason,
            )
        ]
        return self


class ConditionEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observable: bool | None = Field(default=None, strict=True)
    matches: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    profile_id: str = ""
    provider_id: str = ""
    model: str = ""
    attempts: list[AiAttempt] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        data = dict(values)
        if data.get("matches") is None:
            for key in ("match", "present", "detected", "answer", "result"):
                if data.get(key) is not None:
                    data["matches"] = data.get(key)
                    break
        if data.get("confidence") is None:
            for key in ("score", "probability", "certainty"):
                if data.get(key) is not None:
                    data["confidence"] = data.get(key)
                    break
        for key in ("match", "present", "detected", "answer", "result", "score", "probability", "certainty"):
            data.pop(key, None)
        return data

    @model_validator(mode="after")
    def _unobservable_is_not_confirmation(self) -> "ConditionEvaluationResult":
        if self.observable is False:
            self.matches = False
            self.confidence = 0.0
            self.reason = self.reason or "evidence_not_observable"
        return self

    @field_validator("reason", "profile_id", "provider_id", "model", mode="before")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


def encode_image_base64(image: Any, *, max_side_px: int, jpeg_quality: int) -> EncodedImage:
    max_side = max(128, int(max_side_px or 1280))
    quality = max(30, min(100, int(jpeg_quality or 85)))

    if isinstance(image, (bytes, bytearray, memoryview)):
        raw = bytes(image)
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as opened:
                pil_image = opened.copy()
            return _encode_pil_image(pil_image, max_side_px=max_side, jpeg_quality=quality)
        except Exception:
            return EncodedImage(data_b64=base64.b64encode(raw).decode("ascii"), mime_type="application/octet-stream")

    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise AiProviderError("Pillow is required to encode non-byte images for AI providers") from exc

    if isinstance(image, Image.Image):
        pil_image = image
    else:
        try:
            # Pipeline arrays follow the camera/OpenCV BGR convention; PIL objects are already RGB.
            shape = getattr(image, "shape", ())
            if len(shape) == 3 and shape[2] == 3:
                image = image[..., ::-1]
            elif len(shape) == 3 and shape[2] == 4:
                image = image[..., [2, 1, 0, 3]]
            pil_image = Image.fromarray(image)
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("Unsupported image artifact data for AI inference") from exc

    return _encode_pil_image(pil_image, max_side_px=max_side, jpeg_quality=quality)


def decode_image_base64(value: str) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise AiProviderError("image is required")
    if "," in raw and raw.split(",", 1)[0].lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise AiProviderError("image must be valid base64 or a data URL") from exc


def _encode_pil_image(image: Any, *, max_side_px: int, jpeg_quality: int) -> EncodedImage:
    pil_image = image
    if pil_image.mode not in {"RGB", "L"}:
        pil_image = pil_image.convert("RGB")
    elif pil_image.mode == "L":
        pil_image = pil_image.convert("RGB")

    width, height = pil_image.size
    if width > max_side_px or height > max_side_px:
        resized = pil_image.copy()
        resized.thumbnail((max_side_px, max_side_px))
        pil_image = resized
        width, height = pil_image.size

    buf = io.BytesIO()
    pil_image.save(buf, format="JPEG", quality=jpeg_quality)
    return EncodedImage(
        data_b64=base64.b64encode(buf.getvalue()).decode("ascii"),
        mime_type="image/jpeg",
        width=int(width),
        height=int(height),
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise AiProviderError("AI provider returned an empty response")
    try:
        parsed = json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise AiProviderError("AI provider did not return a JSON object")
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise AiProviderError("AI provider JSON response is not an object")
    return parsed


def _build_region_prompt(description: str) -> list[dict[str, Any]]:
    target = str(description or "").strip()
    return [
        {
            "role": "system",
            "content": (
                "You locate visual regions in images. Return only valid JSON with keys: "
                "found, detections, confidence, bbox01, label, reason. detections must be an "
                "array of objects with bbox01, confidence, label, reason. Each bbox01 must be "
                "normalized as [left, top, right, bottom] from 0 to 1. bbox01/confidence may "
                "repeat the best detection for compatibility. Match the complete requested target: "
                "when multiple objects or parts are required together, all must be visibly present "
                "in the same region. If the description requires an entire object, a cropped or "
                "occluded fragment is insufficient. Do not infer an unseen object from related "
                "objects, actions, or context. If any required part is absent or unverifiable, set "
                "found=false, detections=[], confidence=0, bbox01=null."
            ),
        },
        {
            "role": "user",
            "content": f"Find every visible region matching this description: {target}",
        },
    ]


def _build_condition_prompt(description: str) -> list[dict[str, Any]]:
    condition = str(description or "").strip()
    return [
        {
            "role": "system",
            "content": (
                "You evaluate a visual condition in an image. Return only valid JSON with keys: "
                "observable, matches, confidence, reason. observable is true only when the "
                "relevant subject and visual evidence allow evaluating the condition. "
                "Occlusion, an ambiguous subject or a missing required reference means "
                "observable=false, matches=false, confidence=0. A clearly visible negative "
                "has observable=true and matches=false. matches is true only when the "
                "condition is clearly visible. Do not infer intent or hidden actions."
            ),
        },
        {
            "role": "user",
            "content": f"Does this image match the following condition? {condition}",
        },
    ]


def _prepare_visual_state(*, profile: AiProfileConfig, image: Any, classes: list[dict[str, Any]],
                          reference_images: dict[str, Any], scene_description: str):
    if not {"vision", "structured_json", "multiple_images"} <= set(profile.capabilities):
        raise AiProviderError("visual_state_profile_incompatible")
    try:
        request = VisualStateRequest.model_validate({"classes": classes, "scene_description": scene_description})
    except ValueError:
        raise AiProviderError("invalid_visual_state_request") from None
    identifiers = [identifier for item in request.classes for identifier in item.reference_ids]
    if not isinstance(reference_images, dict) or set(reference_images) != set(identifiers):
        raise AiProviderError("visual_state_reference_mismatch")
    if 1 + len(identifiers) > profile.max_images_per_request:
        raise AiProviderError("visual_state_image_limit")
    images = [image, *(reference_images[identifier] for identifier in identifiers)]
    total_bytes = 0
    try:
        from PIL import Image
        import numpy as np

        for item in images:
            if isinstance(item, (bytes, bytearray, memoryview)):
                if len(item) > 8 * 1024 * 1024:
                    raise ValueError("image too large")
                with Image.open(io.BytesIO(item)) as opened:
                    width, height = opened.size
                total_bytes += len(item)
            elif isinstance(item, Image.Image):
                width, height = item.size
                total_bytes += width * height * len(item.getbands())
            elif isinstance(item, np.ndarray) and item.dtype == np.uint8 and item.ndim in (2, 3):
                height, width = item.shape[:2]
                if item.ndim == 3 and item.shape[2] not in (3, 4):
                    raise ValueError("unsupported channels")
                total_bytes += item.nbytes
            else:
                raise ValueError("unsupported image")
            if min(width, height) < 1 or width * height > 4_194_304 or total_bytes > 64 * 1024 * 1024:
                raise ValueError("image capacity exceeded")
        encoded = []
        encoded_size = 0
        for item in images:
            value = encode_image_base64(item, max_side_px=profile.max_image_side_px, jpeg_quality=profile.jpeg_quality)
            encoded_size += len(value.data_b64)
            if value.mime_type != "image/jpeg" or not value.width or not value.height or encoded_size > 16 * 1024 * 1024:
                raise ValueError("invalid encoded image")
            encoded.append(value)
    except (ValueError, TypeError, OSError, Image.DecompressionBombError):
        raise AiProviderError("invalid_visual_state_image") from None
    return request, encoded


async def prepare_visual_state(**kwargs):
    task = asyncio.create_task(asyncio.to_thread(_prepare_visual_state, **kwargs))
    cancelled = False
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


def _validate_visual_state(content: dict[str, Any], request: VisualStateRequest) -> VisualStateResponse:
    try:
        return validate_visual_response(content, request)
    except ValueError:
        raise AiProviderError("invalid_visual_state_response") from None


def _parse_visual_json(content: str) -> dict[str, Any]:
    try:
        return strict_visual_json(content)
    except ValueError:
        raise AiProviderError("invalid_visual_state_response") from None


class OllamaProvider:
    def __init__(self, provider: AiProviderConfig, profile: AiProfileConfig) -> None:
        self._provider = provider
        self._profile = profile

    async def classify_visual_state(self, *, image: Any, classes: list[dict[str, Any]],
                                    reference_images: dict[str, Any], scene_description: str = "") -> VisualStateResponse:
        request, encoded = await prepare_visual_state(profile=self._profile, image=image, classes=classes,
            reference_images=reference_images, scene_description=scene_description)
        return await self.classify_prepared_visual_state(request=request, encoded=encoded)

    async def classify_prepared_visual_state(self, *, request, encoded) -> VisualStateResponse:
        content = await self._chat_json(image=None, messages=request.messages(),
            encoded_images=encoded, response_schema=request.response_schema())
        return _validate_visual_state(content, request)

    async def locate_region(self, *, image: Any, description: str) -> RegionDetectionResult:
        content = await self._chat_json(image=image, messages=_build_region_prompt(description))
        result = RegionDetectionResult.model_validate(content)
        return self._attach_region_meta(result)

    async def evaluate_condition(self, *, image: Any, description: str) -> ConditionEvaluationResult:
        content = await self._chat_json(image=image, messages=_build_condition_prompt(description))
        result = ConditionEvaluationResult.model_validate(content)
        return self._attach_condition_meta(result)

    async def generate_json(self, *, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._chat_json(image=None, messages=messages)

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("httpx is required to query Ollama") from exc

        base_url = self._provider.host.rstrip("/")
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            response = await client.get(f"{base_url}/api/tags")
            response.raise_for_status()
            body = response.json()
        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            return []
        return [item for item in models if isinstance(item, dict)]

    async def pull_model(self, *, model: str | None = None) -> dict[str, Any]:
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("httpx is required to pull Ollama models") from exc

        base_url = self._provider.host.rstrip("/")
        model_name = str(model or self._profile.model or "").strip()
        if not model_name:
            raise AiProviderError("Ollama model is required")
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(f"{base_url}/api/pull", json={"model": model_name, "stream": False})
            response.raise_for_status()
            body = response.json()
        return body if isinstance(body, dict) else {"ok": True}

    async def stream_pull_model(self, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("httpx is required to pull Ollama models") from exc

        base_url = self._provider.host.rstrip("/")
        model_name = str(model or self._profile.model or "").strip()
        if not model_name:
            raise AiProviderError("Ollama model is required")
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{base_url}/api/pull",
                json={"model": model_name, "stream": True},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    text = str(line or "").strip()
                    if not text:
                        continue
                    try:
                        item = json.loads(text)
                    except Exception:
                        item = {"status": text}
                    if isinstance(item, dict):
                        yield item

    async def _chat_json(self, *, image: Any, messages: list[dict[str, Any]],
                         encoded_images: list[EncodedImage] | None = None,
                         response_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("httpx is required to call Ollama") from exc

        ollama_messages = [dict(item) for item in messages]
        if encoded_images is not None:
            ollama_messages[-1]["images"] = [item.data_b64 for item in encoded_images]
        elif image is not None:
            encoded = encode_image_base64(
                image,
                max_side_px=self._profile.max_image_side_px,
                jpeg_quality=self._profile.jpeg_quality,
            )
            ollama_messages[-1]["images"] = [encoded.data_b64]

        body = {
            "model": self._profile.model,
            "messages": ollama_messages,
            "stream": False,
            "format": response_schema if response_schema is not None else "json",
            "options": {
                "temperature": float(self._profile.temperature),
            },
        }
        # Structured visual and textual results use bounded context, without silent truncation.
        body["think"] = False
        body["options"].update(num_ctx=32768, num_predict=8192)
        body.update(truncate=False, shift=False, keep_alive="30s")
        base_url = self._provider.host.rstrip("/")
        timeout = httpx.Timeout(float(self._profile.timeout_seconds), connect=5.0)
        options = {"transport": BoundedVisualTransport()} if response_schema is not None else {}
        async with httpx.AsyncClient(timeout=timeout, **options) as client:
            version_response = await client.get(f"{base_url}/api/version")
            version_response.raise_for_status()
            version = str(version_response.json().get("version", "")).split("-")[0].split(".")
            if len(version) != 3 or not all(part.isdigit() for part in version) or tuple(map(int, version)) < (0, 34, 4):
                raise AiProviderError("Structured inference requires Ollama 0.34.4 or newer to reject context truncation")
            response = await client.post(f"{base_url}/api/chat", json=body)
            response.raise_for_status()
            payload = response.json()
        if (not isinstance(payload, dict) or payload.get("done") is not True or payload.get("done_reason") != "stop"):
            raise AiProviderError("Assistant response did not finish completely; simplify the request")
        message = payload.get("message") if isinstance(payload, dict) else None
        if response_schema is not None:
            if not isinstance(message, dict) or message.get("tool_calls") or message.get("refusal"):
                raise AiProviderError("invalid_visual_state_response")
            return _parse_visual_json(message.get("content"))
        content = message.get("content") if isinstance(message, dict) else ""
        if len(str(content or "")) > 262144:
            raise AiProviderError("Assistant response is too large; simplify the request")
        return _extract_json_object(str(content or ""))

    def _attach_region_meta(self, result: RegionDetectionResult) -> RegionDetectionResult:
        result.profile_id = self._profile.id
        result.provider_id = self._provider.id
        result.model = self._profile.model
        return result

    def _attach_condition_meta(self, result: ConditionEvaluationResult) -> ConditionEvaluationResult:
        result.profile_id = self._profile.id
        result.provider_id = self._provider.id
        result.model = self._profile.model
        return result


class LiteLLMProvider:
    def __init__(self, provider: AiProviderConfig, profile: AiProfileConfig) -> None:
        self._provider = provider
        self._profile = profile

    async def classify_visual_state(self, *, image: Any, classes: list[dict[str, Any]],
                                    reference_images: dict[str, Any], scene_description: str = "") -> VisualStateResponse:
        request, encoded = await prepare_visual_state(profile=self._profile, image=image, classes=classes,
            reference_images=reference_images, scene_description=scene_description)
        return await self.classify_prepared_visual_state(request=request, encoded=encoded)

    async def classify_prepared_visual_state(self, *, request, encoded) -> VisualStateResponse:
        content = await self._completion_json(image=None, messages=request.messages(),
            encoded_images=encoded, response_schema=request.response_schema())
        return _validate_visual_state(content, request)

    async def locate_region(self, *, image: Any, description: str) -> RegionDetectionResult:
        parsed = await self._completion_json(image=image, messages=_build_region_prompt(description))
        result = RegionDetectionResult.model_validate(parsed)
        result.profile_id = self._profile.id
        result.provider_id = self._provider.id
        result.model = self._profile.model
        return result

    async def evaluate_condition(self, *, image: Any, description: str) -> ConditionEvaluationResult:
        parsed = await self._completion_json(image=image, messages=_build_condition_prompt(description))
        result = ConditionEvaluationResult.model_validate(parsed)
        result.profile_id = self._profile.id
        result.provider_id = self._provider.id
        result.model = self._profile.model
        return result

    async def _completion_json(self, *, image: Any, messages: list[dict[str, Any]],
                               encoded_images: list[EncodedImage] | None = None,
                               response_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from litellm import acompletion
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("LiteLLM is not installed in this Toposync environment") from exc

        if encoded_images is None:
            encoded_images = [encode_image_base64(image, max_side_px=self._profile.max_image_side_px,
                                                 jpeg_quality=self._profile.jpeg_quality)]
        converted = [
            {"role": messages[0]["role"], "content": messages[0]["content"]},
            {"role": "user", "content": [
                {"type": "text", "text": str(messages[-1]["content"])},
                *[{"type": "image_url", "image_url": {"url": f"data:{item.mime_type};base64,{item.data_b64}"}}
                  for item in encoded_images],
            ]},
        ]
        kwargs: dict[str, Any] = {
            "model": _litellm_model_name(self._provider, self._profile),
            "messages": converted,
            "response_format": {"type": "json_object"},
            "temperature": float(self._profile.temperature),
            "timeout": float(self._profile.timeout_seconds),
        }
        if response_schema is not None:
            kwargs.update(response_format={"type": "json_schema", "json_schema": {
                "name": "visual_state", "strict": True, "schema": response_schema}},
                num_retries=0, max_retries=0, max_tokens=4096, caching=False)
        if self._provider.api_key:
            kwargs["api_key"] = self._provider.api_key
        if self._provider.host:
            kwargs["api_base"] = self._provider.host
        if response_schema is not None:
            async with visual_completion_client(model=kwargs["model"], api_key=self._provider.api_key,
                                                 api_base=self._provider.host, timeout=self._profile.timeout_seconds) as client:
                response = await acompletion(**kwargs, client=client)
        else:
            response = await acompletion(**kwargs)
        if response_schema is not None:
            def field(item, name):
                return item.get(name) if isinstance(item, dict) else getattr(item, name, None)
            choices = field(response, "choices")
            if not isinstance(choices, list) or len(choices) != 1 or field(choices[0], "finish_reason") != "stop":
                raise AiProviderError("invalid_visual_state_response")
            message = field(choices[0], "message")
            if field(message, "tool_calls") or field(message, "refusal"):
                raise AiProviderError("invalid_visual_state_response")
            return _parse_visual_json(field(message, "content"))
        content = _first_message_content(response)
        return _extract_json_object(content)


def build_provider(provider: AiProviderConfig, profile: AiProfileConfig) -> OllamaProvider | LiteLLMProvider:
    if provider.kind == "ollama":
        return OllamaProvider(provider, profile)
    if provider.kind in {"openai", "anthropic", "google", "litellm"}:
        return LiteLLMProvider(provider, profile)
    raise AiProviderError(f"Unsupported AI provider kind: {provider.kind}")


def _first_message_content(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return ""
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None and isinstance(first, dict):
        message = first.get("message")
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return str(content or "")


def _litellm_model_name(provider: AiProviderConfig, profile: AiProfileConfig) -> str:
    model = str(profile.model or "").strip()
    if not model:
        raise AiProviderError("AI model is required")
    if "/" in model or provider.kind == "litellm":
        return model
    if provider.kind == "anthropic":
        return f"anthropic/{model}"
    if provider.kind == "google":
        return f"gemini/{model}"
    return model


def attempt_from_error(
    *,
    profile: AiProfileConfig,
    provider: AiProviderConfig,
    started: float,
    error: Exception,
) -> AiAttempt:
    return AiAttempt(
        profile_id=profile.id,
        provider_id=provider.id,
        provider_kind=provider.kind,
        model=profile.model,
        ok=False,
        latency_ms=(time.monotonic() - started) * 1000.0,
        error=str(error),
    )


def attempt_from_success(
    *,
    profile: AiProfileConfig,
    provider: AiProviderConfig,
    started: float,
) -> AiAttempt:
    return AiAttempt(
        profile_id=profile.id,
        provider_id=provider.id,
        provider_kind=provider.kind,
        model=profile.model,
        ok=True,
        latency_ms=(time.monotonic() - started) * 1000.0,
    )
