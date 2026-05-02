from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .settings import AiProfileConfig, AiProviderConfig


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
        if data.get("bbox01") is None:
            for key in ("bbox", "box", "bbox_2d", "box_2d", "bounding_box", "boundingBox", "coordinates"):
                if data.get(key) is not None:
                    data["bbox01"] = data.get(key)
                    break
        if data.get("confidence") is None:
            for key in ("score", "probability", "certainty", "confidence_score"):
                if data.get(key) is not None:
                    data["confidence"] = data.get(key)
                    break
        if data.get("label") in {None, ""}:
            for key in ("name", "class", "category", "description"):
                if data.get(key):
                    data["label"] = data.get(key)
                    break
        for key in (
            "bbox",
            "box",
            "bbox_2d",
            "box_2d",
            "bounding_box",
            "boundingBox",
            "coordinates",
            "score",
            "probability",
            "certainty",
            "confidence_score",
            "name",
            "class",
            "category",
            "description",
            "found",
        ):
            data.pop(key, None)
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
        if data.get("bbox01") is None:
            for key in ("bbox", "box", "bbox_2d", "box_2d", "bounding_box", "boundingBox", "coordinates"):
                if data.get(key) is not None:
                    data["bbox01"] = data.get(key)
                    break
        if data.get("detections") is None:
            for key in ("regions", "objects", "items", "boxes", "bounding_boxes", "boundingBoxes"):
                if data.get(key) is not None:
                    data["detections"] = data.get(key)
                    break
        if data.get("detections") is not None and isinstance(data.get("detections"), dict):
            data["detections"] = [data["detections"]]
        if data.get("confidence") is None:
            for key in ("score", "probability", "certainty", "confidence_score"):
                if data.get(key) is not None:
                    data["confidence"] = data.get(key)
                    break
        if data.get("label") in {None, ""}:
            for key in ("name", "class", "category", "description"):
                if data.get(key):
                    data["label"] = data.get(key)
                    break
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
        for key in (
            "bbox",
            "box",
            "bbox_2d",
            "box_2d",
            "bounding_box",
            "boundingBox",
            "coordinates",
            "regions",
            "objects",
            "items",
            "boxes",
            "bounding_boxes",
            "boundingBoxes",
            "score",
            "probability",
            "certainty",
            "confidence_score",
            "name",
            "class",
            "category",
            "description",
        ):
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
                "repeat the best detection for compatibility. If the target is absent, set "
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
                "matches, confidence, reason. The answer must be boolean-first: matches is true "
                "only when the condition is clearly visible."
            ),
        },
        {
            "role": "user",
            "content": f"Does this image match the following condition? {condition}",
        },
    ]


class OllamaProvider:
    def __init__(self, provider: AiProviderConfig, profile: AiProfileConfig) -> None:
        self._provider = provider
        self._profile = profile

    async def locate_region(self, *, image: Any, description: str) -> RegionDetectionResult:
        content = await self._chat_json(image=image, messages=_build_region_prompt(description))
        result = RegionDetectionResult.model_validate(content)
        return self._attach_region_meta(result)

    async def evaluate_condition(self, *, image: Any, description: str) -> ConditionEvaluationResult:
        content = await self._chat_json(image=image, messages=_build_condition_prompt(description))
        result = ConditionEvaluationResult.model_validate(content)
        return self._attach_condition_meta(result)

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

    async def _chat_json(self, *, image: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("httpx is required to call Ollama") from exc

        encoded = encode_image_base64(
            image,
            max_side_px=self._profile.max_image_side_px,
            jpeg_quality=self._profile.jpeg_quality,
        )
        ollama_messages = [dict(item) for item in messages]
        ollama_messages[-1]["images"] = [encoded.data_b64]

        body = {
            "model": self._profile.model,
            "messages": ollama_messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": float(self._profile.temperature),
            },
        }
        base_url = self._provider.host.rstrip("/")
        timeout = httpx.Timeout(float(self._profile.timeout_seconds), connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/api/chat", json=body)
            response.raise_for_status()
            payload = response.json()
        message = payload.get("message") if isinstance(payload, dict) else None
        content = message.get("content") if isinstance(message, dict) else ""
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

    async def _completion_json(self, *, image: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            from litellm import acompletion
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("LiteLLM is not installed in this Toposync environment") from exc

        encoded = encode_image_base64(
            image,
            max_side_px=self._profile.max_image_side_px,
            jpeg_quality=self._profile.jpeg_quality,
        )
        data_url = f"data:{encoded.mime_type};base64,{encoded.data_b64}"
        converted = [
            {"role": messages[0]["role"], "content": messages[0]["content"]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": str(messages[-1]["content"])},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        kwargs: dict[str, Any] = {
            "model": _litellm_model_name(self._provider, self._profile),
            "messages": converted,
            "response_format": {"type": "json_object"},
            "temperature": float(self._profile.temperature),
            "timeout": float(self._profile.timeout_seconds),
        }
        if self._provider.api_key:
            kwargs["api_key"] = self._provider.api_key
        if self._provider.host:
            kwargs["api_base"] = self._provider.host
        response = await acompletion(**kwargs)
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
