"""Campaign deadlines use simulated clocks and control, never camera hardware."""
import asyncio

import pytest

from toposync_ext_ptz_attention.controller import PtzAttentionController
from toposync_ext_ptz_attention.store import AttentionStore
from test_controller import Clock, LiveCamera, intent, live_services, profile


@pytest.mark.parametrize("end", [True, 0, -1, float("inf"), float("nan"), "invalid", 253402300800])
def test_campaign_end_rejects_invalid_or_unrepresentable_dates(end):
    with pytest.raises(ValueError):
        profile(campaign_ends_at=end)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "live_preset"])
async def test_campaign_expiry_pauses_and_rejects_new_work_after_restart(tmp_path, mode):
    clock, camera = Clock(), LiveCamera()
    path = tmp_path / "attention.sqlite"
    store = AttentionStore(path)
    store.create_profile(profile(mode=mode, campaign_ends_at=1010))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    await controller.submit_intent(intent("one", "low", "open", clock))
    if mode == "live_preset":
        assert camera.acquire_calls[0]["maximum_duration_s"] == 10
    clock.advance(10)
    await controller.tick_once()
    assert store.get_profile("front_attention").mode == "paused"
    assert not controller.status()[0].lease_id
    assert not controller._devices["front"].events
    if mode == "live_preset":
        assert camera.commands[-1]["command"]["kind"] == "stop"
        assert store.is_recovery_required("front")
    decisions, _ = store.list_decisions()
    assert any(item.action == "campaign_expired" for item in decisions)
    await controller.shutdown()
    store.close()
    count = camera.acquires
    store = AttentionStore(path)
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    await controller.submit_intent(intent("two", "high", "open", clock))
    with pytest.raises(RuntimeError, match="campaign_expired"):
        await controller.resume("front_attention")
    assert camera.acquires == count
    assert store.get_profile("front_attention").campaign_ends_at == 1010
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["returned", "timeout", "cancelled"])
async def test_expired_campaign_allows_only_explicit_bounded_recovery(outcome):
    clock, camera = Clock(), LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset", campaign_ends_at=1010))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    await controller.submit_intent(intent("one", "low", "open", clock))
    clock.advance(10)
    await controller.tick_once()
    assert await controller.return_home("front_attention") is True
    deadline = controller._devices["front"].manual_return_deadline_monotonic
    assert camera.acquire_calls[-1]["maximum_duration_s"] == 2
    assert camera.commands[-1]["command"]["preset_token"] == "preset-home"
    clock.advance(.5)
    assert await controller.return_home("front_attention") is True
    assert controller._devices["front"].manual_return_deadline_monotonic == deadline
    await controller.submit_intent(intent("two", "high", "open", clock))
    if outcome == "timeout":
        clock.advance(2)
    elif outcome == "cancelled":
        assert (await controller.pause("front_attention")).mode == "paused"
    await controller.tick_once()
    assert not controller.status()[0].lease_id
    assert store.is_recovery_required("front") is (outcome != "returned")
    assert store.get_profile("front_attention").mode == "paused"
    with pytest.raises(RuntimeError, match="campaign_expired"):
        await controller.resume("front_attention")
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_campaign_clock_jump_and_pause_do_not_restart_remaining_budget():
    wall, monotonic, camera = Clock(), Clock(), LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(campaign_ends_at=1010))
    controller = PtzAttentionController(store=store, services=live_services(camera),
        clock=wall, monotonic_clock=monotonic)
    await controller.submit_intent(intent("one", "low", "open", wall))
    await controller.pause("front_attention")
    wall.advance(-100)
    monotonic.advance(10)
    with pytest.raises(RuntimeError, match="campaign_expired"):
        await controller.resume("front_attention")
    await controller.tick_once()
    assert store.get_profile("front_attention").mode == "paused"
    assert not camera.acquires
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["resolve", "snapshot", "acquire", "lock"])
async def test_campaign_expiring_during_preparation_never_starts_movement(stage):
    clock, camera = Clock(), LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset", campaign_ends_at=1010))
    if stage != "lock":
        original = getattr(camera, stage)
        async def delayed(**kwargs):
            result = await original(**kwargs)
            clock.advance(11)
            return result
        setattr(camera, stage, delayed)
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    if stage == "lock":
        async with controller._lock_for("front"):
            pending = asyncio.create_task(controller.submit_intent(intent("one", "low", "open", clock)))
            await asyncio.sleep(0)
            clock.advance(11)
        await pending
    else:
        await controller.submit_intent(intent("one", "low", "open", clock))
    assert camera.acquires == (1 if stage == "acquire" else 0)
    assert all(item["command"]["kind"] == "stop" for item in camera.commands)
    assert not controller.status()[0].lease_id
    assert store.get_profile("front_attention").mode == "paused"
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_queued_intent_reads_campaign_changed_while_waiting():
    clock, camera = Clock(), LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    async with controller._lock_for("front"):
        pending = asyncio.create_task(controller.submit_intent(intent("one", "low", "open", clock)))
        await asyncio.sleep(0)
        store.replace_profile(profile(mode="live_preset", campaign_ends_at=clock() - 1))
    await pending
    assert not camera.acquires and not camera.commands
    assert store.get_profile("front_attention").mode == "paused"
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_campaign_expiring_after_verified_return_needs_no_extra_recovery():
    clock, camera = Clock(), LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset", campaign_ends_at=1010))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    await controller.submit_intent(intent("one", "low", "close", clock, target=False))
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    count = len(camera.commands)
    clock.advance(10)
    await controller.tick_once()
    assert store.get_profile("front_attention").mode == "paused"
    assert not store.is_recovery_required("front")
    assert controller.status()[0].campaign_expired
    assert len(camera.commands) == count
    await controller.shutdown()
    store.close()
