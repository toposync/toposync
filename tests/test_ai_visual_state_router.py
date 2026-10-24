from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from toposync.app import create_app
from toposync_ext_ai.constants import EXTENSION_ID
from toposync_ext_ai.router import AiRouter
from toposync_ext_ai.visual_state import VisualStateResponse


def arguments(**overrides):
    return dict(image=np.zeros((8, 8, 3), dtype=np.uint8),
        classes=[{"class_id": "closed", "label": "Closed", "description": "Opening covered"},
                 {"class_id": "open", "label": "Open", "description": "Opening visible"}],
        reference_images={}, sensor_id="door", profile_id="first", deadline_monotonic=time.monotonic() + 1,
        **overrides)


def observation(**changes):
    return VisualStateResponse.model_validate(dict(observable=True, class_id="closed", reason="Opening covered",
        declared_confidence=.9, reference_ids=[]) | changes)


def settings():
    return {"default_profile_id": "first", "providers": [
        {"id": "local", "kind": "ollama", "host": "http://127.0.0.1:11434"}],
        "profiles": [{"id": identifier, "provider_id": "local", "model": identifier,
                      "fallback_profile_ids": ["second", "first", "third"],
                      "capabilities": ["vision", "structured_json", "multiple_images"]}
                     for identifier in ["first", "second", "third"]]}


class Store:
    def __init__(self, raw=None):
        self.raw = settings() if raw is None else raw

    async def get_settings(self):
        return SimpleNamespace(extensions={EXTENSION_ID: self.raw})


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


def test_success_is_validated_and_attributed_by_router(tmp_path, monkeypatch):
    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, *, request, encoded):
            calls.append((request, encoded))
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=Store(), data_dir=tmp_path)
        result = await router.classify_visual_state(**arguments())
        assert result.observation.class_id == "closed"
        assert (result.profile_id, result.provider_id, result.model) == ("first", "local", "first")
        assert len(result.attempts) == 1 and result.attempts[0].ok
        assert len(result.configuration_revision) == 64
        assert result.attempts[0].latency_ms > 0
        assert len(calls) == 1 and len(calls[0][1]) == 1
        assert (await router.usage_snapshot())["profiles"]["first"]["day"] == 1
        await router.shutdown()
        assert not router._visual_jobs

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["model", "temperature", "credential", "disabled"])
def test_profile_or_connection_change_during_inference_discards_result(tmp_path, monkeypatch, change):
    store = Store()

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            if change == "credential":
                store.raw["providers"][0]["api_key"] = "changed-secret"
            elif change == "disabled":
                store.raw["profiles"][0]["enabled"] = False
            else:
                store.raw["profiles"][0][change] = "other-model" if change == "model" else .5
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=store, data_dir=tmp_path)
        result = await router.classify_visual_state(**arguments())
        assert result.reason == "visual_state_configuration_changed" and result.observation is None
        assert result.configuration_revision is None and len(result.attempts) == 1
        assert (await router.usage_snapshot())["profiles"]["first"]["day"] == 1
        assert "changed-secret" not in result.model_dump_json()
        await router.shutdown()
    asyncio.run(scenario())


def test_revocation_during_preparation_cannot_start_transport_or_alternative(tmp_path, monkeypatch):
    import toposync_ext_ai.router as module
    store = Store()
    prepare = module.prepare_visual_state

    async def revoke(**arguments):
        result = await prepare(**arguments)
        store.raw["providers"][0]["enabled"] = False
        return result

    monkeypatch.setattr(module, "prepare_visual_state", revoke)
    monkeypatch.setattr(module, "build_provider", lambda *_: pytest.fail("revoked provider must not send"))

    async def scenario():
        router = AiRouter(config_store=store, data_dir=tmp_path)
        result = await router.classify_visual_state(**arguments(max_attempts=3))
        assert result.reason == "visual_state_configuration_changed" and result.observation is None
        assert len(result.attempts) == 1 and not result.attempts[0].ok
        await router.shutdown()
    asyncio.run(scenario())


def test_configuration_read_failure_after_valid_response_does_not_try_another_model(monkeypatch):
    store = Store()
    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(1)
            store.raw = {"unexpected_configuration_field": True}
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=store)
        result = await router.classify_visual_state(**arguments(max_attempts=3))
        assert result.reason == "visual_state_configuration_unavailable" and result.observation is None
        assert len(result.attempts) == 1 and calls == [1]
        await router.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("observable", [True, False])
def test_abstention_does_not_cherry_pick_a_different_profile(monkeypatch, observable):
    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(1)
            return observation(observable=observable, class_id=None)

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=Store())
        result = await router.classify_visual_state(**arguments(max_attempts=3))
        assert result.observation.class_id is None and result.observation.observable is observable
        assert result.attempts[0].ok and calls == [1]
        await router.shutdown()

    asyncio.run(scenario())


def test_fallback_uses_remaining_deadline_and_never_repeats_profile(monkeypatch):
    calls = []

    class Provider:
        def __init__(self, profile):
            self.profile = profile

        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append((self.profile.id, self.profile.timeout_seconds))
            await asyncio.sleep(.025)
            if self.profile.id == "first":
                raise TimeoutError("uncertain")
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda _, profile: Provider(profile))

    async def scenario():
        router = AiRouter(config_store=Store())
        supplied = arguments(max_attempts=3, fallback_profile_ids=["first", "second", "third"])
        result = await router.classify_visual_state(**supplied)
        assert result.profile_id == "second"
        assert [item[0] for item in calls] == ["first", "second"]
        assert 0 < calls[1][1] < calls[0][1] < 1
        assert result.attempts[0].error == "visual_state_attempt_timeout"
        counts = await router.usage_snapshot()
        assert counts["profiles"]["first"]["day"] == counts["profiles"]["second"]["day"] == 1
        await router.shutdown()

    asyncio.run(scenario())


def test_default_has_no_alternative_and_errors_do_not_leak_provider_data(monkeypatch):
    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            raise RuntimeError("secret-key data:image/private localhost/private-path")

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=Store())
        result = await router.classify_visual_state(**arguments())
        assert result.reason == "visual_state_provider_failed" and len(result.attempts) == 1
        assert "secret" not in result.model_dump_json() and "private" not in result.model_dump_json()
        await router.shutdown()

    asyncio.run(scenario())


def test_per_profile_timeout_leaves_only_remaining_time_for_alternative(monkeypatch):
    calls = []
    raw = settings()
    raw["profiles"][0]["timeout_seconds"] = 1

    class Provider:
        def __init__(self, profile):
            self.profile = profile

        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append((self.profile.id, self.profile.timeout_seconds))
            if self.profile.id == "first":
                await asyncio.sleep(10)
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda _, profile: Provider(profile))

    async def scenario():
        router = AiRouter(config_store=Store(raw))
        supplied = arguments(max_attempts=2)
        supplied["deadline_monotonic"] = time.monotonic() + 1.5
        result = await router.classify_visual_state(**supplied)
        assert result.profile_id == "second"
        assert result.attempts[0].error == "visual_state_attempt_timeout"
        assert 0 < calls[1][1] <= .5
        await router.shutdown()
    asyncio.run(scenario())


def test_alternative_chain_is_finite_and_deduplicated(monkeypatch):
    calls = []

    class Provider:
        def __init__(self, profile):
            self.profile = profile

        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(self.profile.id)
            raise RuntimeError("failure")

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda _, profile: Provider(profile))

    async def scenario():
        router = AiRouter(config_store=Store())
        result = await router.classify_visual_state(**arguments(max_attempts=3, fallback_profile_ids=["third", "first", "second"]))
        assert result.observation is None and calls == ["first", "second", "third"]
        assert len(result.attempts) == 3
        await router.shutdown()
    asyncio.run(scenario())


def test_total_admission_is_bounded_and_shutdown_closes_service(monkeypatch):
    entered = asyncio.Event()

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=Store())
        tasks = [asyncio.create_task(router.classify_visual_state(**(arguments() | {
            "sensor_id": f"sensor-{index}", "deadline_monotonic": time.monotonic() + 5}))) for index in range(32)]
        await entered.wait()
        await until(lambda: len(router._visual_jobs) == 32)
        assert (await router.classify_visual_state(**arguments())).reason == "visual_state_busy"
        await router.shutdown()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(item, asyncio.CancelledError) for item in outcomes)
        assert not router._visual_jobs and not router._limiter._active
        assert (await router.classify_visual_state(**arguments())).reason == "visual_state_busy"
    asyncio.run(scenario())


def test_extension_registers_operation_and_closes_its_router(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    app = create_app()
    with TestClient(app) as client:
        assert client.patch("/api/ai/settings", json=settings()).status_code == 200
        service = app.state.services._services["ai.infer.classify_visual_state"]
        router = service.__self__
        assert not router._visual_closed
        # Service resolution and the elapsed-deadline path need no model or network.
        async def invoke():
            return await app.state.services.call("ai.infer.classify_visual_state", **(
                arguments() | {"deadline_monotonic": time.monotonic() - 1}))
        result = client.portal.call(invoke)
        assert result.reason == "visual_state_deadline"
    assert router._visual_closed and not router._visual_jobs


def test_queued_deadline_does_not_reserve_or_contact_provider(monkeypatch):
    calls = []
    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: calls.append(1))

    async def scenario():
        router = AiRouter(config_store=Store())
        policy = (await router.settings()).limits
        async with router._limiter.slot(profile_id="first", limits=policy):
            supplied = arguments()
            supplied["deadline_monotonic"] = time.monotonic() + .03
            result = await router.classify_visual_state(**supplied)
            assert result.reason == "visual_state_deadline"
            assert calls == []
            assert (await router.usage_snapshot())["profiles"]["first"]["day"] == 0
        await router.shutdown()

    asyncio.run(scenario())


def test_native_preparation_outlives_deadline_without_releasing_slot(tmp_path, monkeypatch):
    from toposync_ext_ai import providers

    entered, release = threading.Event(), threading.Event()
    original = providers._prepare_visual_state
    calls = []

    def prepare(**kwargs):
        entered.set()
        if not release.wait(3):
            raise AssertionError("fixture did not release native work")
        return original(**kwargs)

    monkeypatch.setattr(providers, "_prepare_visual_state", prepare)
    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: calls.append(1))

    async def scenario():
        router = AiRouter(config_store=Store(), data_dir=tmp_path)
        supplied = arguments()
        supplied["deadline_monotonic"] = time.monotonic() + .05
        task = asyncio.create_task(router.classify_visual_state(**supplied))
        try:
            await until(entered.is_set)
            result = await asyncio.wait_for(task, .5)
            assert result.reason == "visual_state_deadline"
            snapshot = result.model_dump_json()
            assert router._limiter._active[("profile", "first")] == 1
            assert len(router._visual_jobs) == 1
            for job in router._visual_jobs:
                job.cancel()
            supplied["deadline_monotonic"] = time.monotonic() + .03
            second = await router.classify_visual_state(**supplied)
            assert second.reason == "visual_state_deadline" and calls == []
            assert router._limiter._active[("profile", "first")] == 1
        finally:
            release.set()
            await router.shutdown()
        assert result.model_dump_json() == snapshot
        assert not router._visual_jobs and not router._limiter._active
        assert (await router.usage_snapshot())["profiles"]["first"]["day"] == 0

    asyncio.run(scenario())


def test_deadline_during_persistent_reservation_never_starts_transport(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: calls.append(1))

    async def scenario():
        router = AiRouter(config_store=Store(), data_dir=tmp_path)
        original = router._limiter._write_state

        def write():
            entered.set()
            if not release.wait(3):
                raise AssertionError("fixture did not release storage")
            original()

        monkeypatch.setattr(router._limiter, "_write_state", write)
        supplied = arguments() | {"deadline_monotonic": time.monotonic() + .05}
        task = asyncio.create_task(router.classify_visual_state(**supplied))
        try:
            await until(entered.is_set)
            result = await asyncio.wait_for(task, .5)
            assert result.reason == "visual_state_deadline" and calls == []
            assert router._limiter._active[("profile", "first")] == 1
        finally:
            release.set()
            await router.shutdown()
        restored = AiRouter(config_store=Store(), data_dir=tmp_path)
        assert (await restored.usage_snapshot())["profiles"]["first"]["day"] == 1
        assert calls == []
    asyncio.run(scenario())


def test_invalid_settings_return_a_safe_unavailable_reason():
    async def scenario():
        router = AiRouter(config_store=Store({"secret-field": "private-value"}))
        result = await router.classify_visual_state(**arguments())
        assert result.reason == "visual_state_configuration_unavailable"
        assert "private-value" not in result.model_dump_json()
        assert result.observation is None and not result.attempts
        await router.shutdown()
    asyncio.run(scenario())


def test_provider_ignoring_cancellation_cannot_publish_or_start_fallback(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(1)
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=Store(), data_dir=tmp_path)
        task = asyncio.create_task(router.classify_visual_state(**arguments(max_attempts=3)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert router._visual_jobs
        assert router._limiter._active[("profile", "first")] == 1
        release.set()
        await router.shutdown()
        assert calls == [1]
        restored = AiRouter(config_store=Store(), data_dir=tmp_path)
        assert (await restored.usage_snapshot())["profiles"]["first"]["day"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("kind,host,upload,consent,allowed", [
    ("ollama", "http://127.0.0.1:11434", False, False, True),
    ("ollama", "http://provider.invalid", False, False, False),
    ("ollama", "http://provider.invalid", False, True, True),
    ("ollama", "http://[private-invalid", False, False, False),
    ("openai", "http://provider.invalid", True, False, False),
    ("openai", "http://provider.invalid", False, True, False),
    ("openai", "http://provider.invalid", True, True, True),
    ("litellm", "", False, False, False),
])
def test_external_destinations_require_per_call_consent(monkeypatch, kind, host, upload, consent, allowed):
    raw = settings()
    raw["providers"][0].update(kind=kind, host=host, allow_image_upload=upload)
    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(1)
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=Store(raw))
        result = await router.classify_visual_state(**arguments(allow_remote_images=consent))
        assert bool(result.observation) is allowed
        assert len(calls) == int(allowed)
        if not allowed:
            assert result.reason == "image_upload_not_allowed"
        await router.shutdown()

    asyncio.run(scenario())


def test_invalid_response_or_exhausted_sensor_budget_cannot_become_a_class(monkeypatch):
    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(1)
            return observation().model_dump() | {"class_id": "invented"}

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=Store())
        result = await router.classify_visual_state(**arguments(max_attempts=3, sensor_limits={"requests_per_day": 1}))
        assert result.observation is None and result.reason == "sensor_day_limit"
        assert result.attempts[0].error == "invalid_visual_state_response"
        assert calls == [1]
        await router.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"deadline_monotonic": float("nan")}, {"deadline_monotonic": float("inf")},
    {"max_attempts": 0}, {"max_attempts": 4}, {"max_attempts": True},
    {"allow_remote_images": "true"}, {"sensor_id": "bad id"}, {"profile_id": ""},
    {"reference_images": {"unknown": b"fake"}}, {"fallback_profile_ids": [1]},
])
def test_invalid_call_policy_is_rejected_before_scheduling(changes):
    async def scenario():
        router = AiRouter(config_store=Store())
        with pytest.raises((ValueError, ValidationError)):
            await router.classify_visual_state(**(arguments() | changes))
        assert not router._visual_jobs
    asyncio.run(scenario())
