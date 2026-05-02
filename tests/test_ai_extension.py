from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fastapi.testclient import TestClient

from toposync.app import create_app
from toposync.runtime.config_store import AppSettings
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_ai.constants import EXTENSION_ID
from toposync_ext_ai.pipelines import register_ai_pipeline_operators
from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime, AiSmartCropRuntime
from toposync_ext_ai.providers import ConditionEvaluationResult, RegionDetectionResult
from toposync_ext_ai.router import AiRouter


class _FakeServices:
    def __init__(self, *, region: dict[str, Any] | None = None, condition: dict[str, Any] | None = None) -> None:
        self.region = region or {}
        self.condition = condition or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, service_id: str, **kwargs: Any) -> Any:
        self.calls.append((service_id, kwargs))
        if service_id == "ai.infer.locate_region":
            return dict(self.region)
        if service_id == "ai.infer.evaluate_condition":
            return dict(self.condition)
        raise KeyError(service_id)


class _FakeConfigStore:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = AppSettings(core={}, extensions={EXTENSION_ID: dict(settings or {})})

    async def get_settings(self) -> AppSettings:
        return self.settings

    async def replace_settings(self, settings: AppSettings) -> AppSettings:
        self.settings = settings
        return settings


def test_ai_extension_registers_initial_operators() -> None:
    registry = OperatorRegistry()
    register_ai_pipeline_operators(registry)

    smart_crop = registry.get("ai.smart_crop")
    condition_filter = registry.get("ai.condition_filter")

    assert smart_crop is not None
    assert condition_filter is not None
    assert smart_crop.owner == "com.toposync.ai"
    assert condition_filter.owner == "com.toposync.ai"
    assert smart_crop.definition.defaults["profile_id"] == "local_qwen3_vl_quality"
    assert "frame_original" in smart_crop.definition.requires_artifacts
    assert "object_bbox01" in smart_crop.definition.produces_payload_keys
    assert "ai" in condition_filter.definition.produces_payload_keys


def test_ai_result_parsers_accept_common_model_aliases() -> None:
    region = RegionDetectionResult.model_validate(
        {"bbox": [200, 100, 800, 900], "score": 0.7, "category": "pool"}
    )
    assert region.found is True
    assert region.confidence == pytest.approx(0.7)
    assert region.bbox01 == pytest.approx([0.2, 0.1, 0.8, 0.9])
    assert region.label == "pool"
    assert len(region.detections) == 1

    condition = ConditionEvaluationResult.model_validate({"answer": True, "score": 0.62})
    assert condition.matches is True
    assert condition.confidence == pytest.approx(0.62)


def test_ai_region_result_supports_multiple_detections() -> None:
    region = RegionDetectionResult.model_validate(
        {
            "objects": [
                {"bbox": [100, 100, 300, 300], "score": 0.62, "category": "person"},
                {"bbox": [450, 250, 900, 900], "score": 0.91, "category": "person"},
            ],
            "reason": "two people",
        }
    )

    assert region.found is True
    assert len(region.detections) == 2
    assert region.confidence == pytest.approx(0.91)
    assert region.bbox01 == pytest.approx([0.45, 0.25, 0.9, 0.9])


def test_ai_smart_crop_uses_ai_bbox_and_updates_frame() -> None:
    async def scenario() -> Packet:
        import numpy as np

        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={
                "frame_original": Artifact(name="frame_original", data=frame, mime_type="image/raw"),
                "frame": Artifact(name="frame", data=frame, mime_type="image/raw"),
            },
        )
        services = _FakeServices(
            region={
                "found": True,
                "confidence": 0.82,
                "bbox01": [0.25, 0.2, 0.75, 0.6],
                "label": "sofa",
                "profile_id": "local_qwen3_vl_quality",
                "provider_id": "ollama_local",
                "model": "qwen3-vl:30b",
            }
        )
        runtime = AiSmartCropRuntime(
            {
                "target_description": "sofa",
                "padding_ratio": 0.0,
                "confidence_threshold": 0.5,
                "refresh_on_ptz_idle": False,
            },
            PipelineRuntimeDependencies(services=services),
        )
        out_packets = await runtime.process_packet(packet, None)
        assert len(out_packets) == 1
        return out_packets[0]

    out = asyncio.run(scenario())
    assert "ai_crop" in out.artifacts
    assert "frame" in out.artifacts
    assert tuple(getattr(out.artifacts["ai_crop"].data, "shape", ())) == (40, 100, 3)
    assert tuple(getattr(out.artifacts["frame"].data, "shape", ())) == (40, 100, 3)
    assert out.payload["object_bbox01"] == pytest.approx([0.25, 0.2, 0.75, 0.6])
    assert out.payload["object_confidence"] == pytest.approx(0.82)
    assert out.payload["object_category_label"] == "sofa"
    assert out.payload["frame_crop"]["bbox01"] == pytest.approx([0.25, 0.2, 0.75, 0.6])
    assert out.payload["ai"]["smart_crop"]["status"] == "found"
    assert out.payload["ai"]["smart_crop"]["model"] == "qwen3-vl:30b"
    assert len(out.payload["ai"]["smart_crop"]["detections"]) == 1


def test_ai_smart_crop_can_union_multiple_detections() -> None:
    async def scenario() -> Packet:
        import numpy as np

        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={
                "frame_original": Artifact(name="frame_original", data=frame, mime_type="image/raw"),
                "frame": Artifact(name="frame", data=frame, mime_type="image/raw"),
            },
        )
        services = _FakeServices(
            region={
                "found": True,
                "detections": [
                    {"confidence": 0.82, "bbox01": [0.1, 0.1, 0.2, 0.3], "label": "cat"},
                    {"confidence": 0.91, "bbox01": [0.6, 0.4, 0.9, 0.9], "label": "cat"},
                ],
                "profile_id": "local_qwen3_vl_quality",
                "provider_id": "ollama_local",
                "model": "qwen3-vl:30b",
            }
        )
        runtime = AiSmartCropRuntime(
            {
                "target_description": "cat",
                "padding_ratio": 0.0,
                "confidence_threshold": 0.5,
                "detection_strategy": "union",
                "refresh_on_ptz_idle": False,
            },
            PipelineRuntimeDependencies(services=services),
        )
        out_packets = await runtime.process_packet(packet, None)
        assert len(out_packets) == 1
        return out_packets[0]

    out = asyncio.run(scenario())
    assert tuple(getattr(out.artifacts["ai_crop"].data, "shape", ())) == (80, 160, 3)
    assert out.payload["object_bbox01"] == pytest.approx([0.1, 0.1, 0.9, 0.9])
    assert out.payload["object_confidence"] == pytest.approx(0.82)
    assert out.payload["frame_crop"]["detection_strategy"] == "union"
    assert out.payload["frame_crop"]["selected_detection_index"] is None
    assert len(out.payload["detected_objects"]) == 2
    assert len(out.payload["ai"]["smart_crop"]["detections"]) == 2


def test_ai_condition_filter_emits_only_matching_packets() -> None:
    async def scenario(matches: bool) -> list[Packet]:
        import numpy as np

        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            artifacts={
                "frame_original": Artifact(name="frame_original", data=frame, mime_type="image/raw"),
                "frame": Artifact(name="frame", data=frame, mime_type="image/raw"),
            },
        )
        services = _FakeServices(
            condition={
                "matches": matches,
                "confidence": 0.9,
                "reason": "test",
                "profile_id": "local_qwen3_vl_quality",
                "provider_id": "ollama_local",
                "model": "qwen3-vl:30b",
            }
        )
        runtime = AiConditionFilterRuntime(
            {
                "condition_description": "someone is sitting on the sofa",
                "confidence_threshold": 0.5,
                "evaluation_interval_seconds": 0.0,
                "reuse_last_decision_seconds": 0.0,
            },
            PipelineRuntimeDependencies(services=services),
        )
        return await runtime.process_packet(packet, None)

    assert asyncio.run(scenario(False)) == []
    passed = asyncio.run(scenario(True))
    assert len(passed) == 1
    assert passed[0].payload["ai"]["condition_filter"]["matches"] is True
    assert passed[0].payload["ai"]["condition_filter"]["model"] == "qwen3-vl:30b"


def test_ai_router_falls_back_between_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> RegionDetectionResult:
        class _Provider:
            def __init__(self, model: str) -> None:
                self.model = model

            async def locate_region(self, *, image: Any, description: str) -> RegionDetectionResult:  # noqa: ARG002
                if self.model == "bad-model":
                    raise RuntimeError("model failed")
                return RegionDetectionResult(
                    found=True,
                    confidence=0.91,
                    bbox01=[0.1, 0.2, 0.7, 0.8],
                    label="target",
                    model=self.model,
                )

        def _build_provider(provider, profile):  # noqa: ANN001
            return _Provider(profile.model)

        monkeypatch.setattr("toposync_ext_ai.router.build_provider", _build_provider)
        router = AiRouter(
            config_store=_FakeConfigStore(
                {
                    "default_profile_id": "first",
                    "providers": [
                        {"id": "local", "name": "Local", "kind": "ollama", "host": "http://localhost:11434"},
                    ],
                    "profiles": [
                        {
                            "id": "first",
                            "name": "First",
                            "provider_id": "local",
                            "model": "bad-model",
                            "fallback_profile_ids": ["second"],
                        },
                        {"id": "second", "name": "Second", "provider_id": "local", "model": "good-model"},
                    ],
                    "limits": {
                        "max_concurrency": 1,
                        "requests_per_minute": 10,
                        "requests_per_hour": 10,
                        "requests_per_day": 10,
                    },
                }
            )
        )
        return await router.locate_region(
            image=b"image",
            description="target",
            min_confidence=0.5,
            fallback_on_low_confidence=True,
        )

    result = asyncio.run(scenario())
    assert result.found is True
    assert result.model == "good-model"
    assert [attempt.ok for attempt in result.attempts] == [False, True]


def test_ai_router_blocks_cloud_profiles_without_image_upload_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    def _build_provider(_provider, _profile):  # noqa: ANN001
        raise AssertionError("cloud provider should not be called")

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", _build_provider)

    async def scenario() -> RegionDetectionResult:
        router = AiRouter(
            config_store=_FakeConfigStore(
                {
                    "default_profile_id": "cloud",
                    "providers": [
                        {
                            "id": "openai",
                            "name": "OpenAI",
                            "kind": "openai",
                            "api_key": "test",
                            "allow_image_upload": False,
                        },
                    ],
                    "profiles": [
                        {"id": "cloud", "name": "Cloud", "provider_id": "openai", "model": "gpt-4o-mini"},
                    ],
                }
            )
        )
        return await router.locate_region(image=b"image", description="target", min_confidence=0.5)

    result = asyncio.run(scenario())
    assert result.found is False
    assert result.reason == "all_ai_profiles_failed"
    assert result.attempts[0].error == "image_upload_not_allowed"


def test_ai_settings_and_preview_api(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    class _Provider:
        async def locate_region(self, *, image: Any, description: str) -> RegionDetectionResult:  # noqa: ARG002
            return RegionDetectionResult(
                found=True,
                confidence=0.88,
                bbox01=[0.2, 0.2, 0.8, 0.8],
                label="box",
                model="fake-model",
            )

        async def evaluate_condition(self, *, image: Any, description: str) -> ConditionEvaluationResult:  # noqa: ARG002
            return ConditionEvaluationResult(matches=True, confidence=0.77, reason="yes", model="fake-model")

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda _provider, _profile: _Provider())
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

    with TestClient(create_app()) as client:
        settings = client.get("/api/ai/settings")
        assert settings.status_code == 200
        assert settings.json()["default_profile_id"] == "local_qwen3_vl_quality"

        patched = client.patch("/api/ai/settings", json={"limits": {"max_concurrency": 2}})
        assert patched.status_code == 200
        assert patched.json()["limits"]["max_concurrency"] == 2

        provider_test = client.post(
            "/api/ai/providers/test",
            json={
                "provider": {
                    "id": "openai",
                    "name": "OpenAI",
                    "kind": "openai",
                    "api_key": "",
                    "allow_image_upload": True,
                },
                "model": "gpt-4o-mini",
            },
        )
        assert provider_test.status_code == 200
        assert provider_test.json()["litellm_available"] is True
        assert provider_test.json()["missing_api_key"] is True

        preview = client.post(
            "/api/ai/preview/locate_region",
            json={"image_base64": "aGVsbG8=", "description": "box", "min_confidence": 0.5},
        )
        assert preview.status_code == 200
        assert preview.json()["found"] is True
        assert preview.json()["bbox01"] == pytest.approx([0.2, 0.2, 0.8, 0.8])
        assert len(preview.json()["detections"]) == 1
