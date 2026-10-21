from __future__ import annotations

import asyncio
from datetime import datetime
import json

import pytest

from toposync_ext_ai.router import AiRouter
from toposync_ext_ai.settings import AiLimitSettings
from test_ai_visual_state_router import Store, settings


def test_profile_options_exclude_credentials_hosts_other_sensors_and_never_infer(tmp_path, monkeypatch):
    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: pytest.fail("options must not query a provider"))

    async def run():
        raw = settings()
        raw["providers"][0].update(api_key="private-credential", host="http://192.0.2.1:11434")
        raw["profiles"][0].update(budget_group_id="account", max_images_per_request=7)
        raw["budget_groups"] = [{"id": "account", "name": "Shared", "limits": {"requests_per_minute": 5}}]
        router = AiRouter(config_store=Store(raw), data_dir=tmp_path)
        config = await router.settings()
        for sensor in ("door", "another-private-sensor"):
            allowed, _ = await router._limiter.check_and_increment(profile_id="first", limits=config.limits,
                sensor_id=sensor, sensor_limits=AiLimitSettings(), budget_group=config.budget_groups[0])
            assert allowed
        options = await router.visual_state_options(sensor_id="door")
        text = json.dumps(options)
        assert all(secret not in text for secret in ("private-credential", "192.0.2.1", "another-private-sensor", "raw_counters", "api_key"))
        profile = next(item for item in options["profiles"] if item["id"] == "first")
        assert profile["local"] is False and profile["images_allowed"] is True
        assert profile["max_images_per_request"] == 7 and profile["chain"] == ["first", "second", "third"]
        assert options["usage"]["sensors"] == {"door": dict.fromkeys(("minute", "hour", "day", "month"), 1)}
        assert options["usage"]["profiles"]["first"]["minute"] == 2
        assert options["usage"]["budget_groups"]["account"]["minute"] == 2
        assert (await router.usage_snapshot())["profiles"]["first"]["minute"] == 2
        await router.shutdown()
    asyncio.run(run())


def test_usage_reset_times_follow_server_calendar_and_missing_storage_is_unknown(tmp_path, monkeypatch):
    from toposync_ext_ai import usage
    now = datetime(2026, 12, 31, 23, 59, 30).timestamp()
    monkeypatch.setattr(usage.time, "time", lambda: now)

    async def run():
        router = AiRouter(config_store=Store(), data_dir=tmp_path)
        options = await router.visual_state_options(sensor_id="door")
        assert options["usage"]["available"]
        assert set(options["usage"]["resets_at"].values()) == {datetime(2027, 1, 1).timestamp()}
        await router.shutdown()
        (tmp_path / "ai_usage.json").write_text("invalid")
        broken = AiRouter(config_store=Store(), data_dir=tmp_path)
        options = await broken.visual_state_options(sensor_id="door")
        assert not options["usage"]["available"]
        assert options["usage"]["sensors"] == options["usage"]["resets_at"] == {}
        await broken.shutdown()
    asyncio.run(run())
