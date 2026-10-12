"""Controlled coordinates exercise real operators, not detector or physical recall."""
import asyncio
from dataclasses import replace

import pytest

from toposync.runtime.pipelines.operators_core import StationaryEventRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync_ext_cameras.pipelines.postprocess import AreaRestrictionRuntime, VelocityEstimationRuntime


async def replay(monkeypatch, positions, *, area=False, stationary_config=None, velocity_config=None, mutate=None):
    clock = [1000.0]
    monkeypatch.setattr('time.time', lambda: clock[0])
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    velocity = VelocityEstimationRuntime(velocity_config or {'stopped_speed_threshold': 1 / 3.6})
    stationary = StationaryEventRuntime({'require_arrival': True, 'arrival_evidence_field': 'payload.velocity.arrival_evidence', 'observation_started_field': 'payload.area_observation_started_at', **(stationary_config or {})})
    region = AreaRestrictionRuntime({'areas': [{'name': 'front', 'points': [
        {'x': -1.5, 'z': 0}, {'x': 5, 'z': 0}, {'x': 5, 'z': 30}, {'x': -1.5, 'z': 30}]}],
        'include_area_names': ['front']})
    outputs, packets = [], []
    for index, x in enumerate(positions):
        clock[0] = 1000.0 + index
        packet = Packet.create(stream_id='camera', payload={
            'source_stream_id': 'camera', 'camera_id': 'camera', 'event_id': 'vehicle',
            'subject': {'id': 'vehicle', 'category': 'car'}, 'frame_ts': clock[0],
            'world': {'x': x, 'z': 21.0},
            'capture_evidence': {'capture_instance': 'one', 'generation': 0, 'sequence': index + 1,
                                 'published_at': clock[0], 'received_monotonic': clock[0]},
        })
        if mutate:
            mutate(index, packet)
        [packet] = await velocity.process_packet(packet, None)
        packets.append(packet)
        for item in (await region.process_packet(packet, None) if area else [packet]):
            outputs.extend(await stationary.process_packet(item, None))
    return outputs, packets


def test_deceleration_preserves_arrival(monkeypatch):
    outputs, packets = asyncio.run(replay(monkeypatch, [0, 1, 1.25, 1.25, 1.25, 1.25, 1.25]))
    assert packets[2].payload['velocity']['stopped'] is False
    opened = [p for p in outputs if p.lifecycle == Lifecycle.OPEN]
    assert len(opened) == 1
    assert opened[0].payload['frame_ts'] == 1005


def test_arrival_before_area_survives_slow_entry(monkeypatch):
    outputs, _ = asyncio.run(replay(monkeypatch, [-3, -1.55, -1.4, -1.4, -1.4, -1.4, -1.4], area=True))
    opened = [p for p in outputs if p.lifecycle == Lifecycle.OPEN]
    assert len(opened) == 1
    assert opened[0].payload['frame_ts'] == 1004


@pytest.mark.parametrize('failure', ['parked', 'outside', 'generation', 'invalid_world', 'gap', 'missing_capture'])
def test_arrival_does_not_survive_invalid_continuity(monkeypatch, failure):
    positions = [-3, -1.55, -1.4, -1.4, -1.4, -1.4, -1.4]
    if failure == 'parked':
        positions = [-1.4] * 7
    if failure == 'outside':
        positions = [-5, -4, -4, -4, -4, -4, -4]
    def mutate(index, packet):
        if failure == 'generation' and index >= 2:
            packet.payload['capture_evidence']['generation'] = 1
        if failure == 'invalid_world' and index == 2:
            packet.payload['world']['x'] = float('nan')
        if failure == 'missing_capture' and index == 2:
            packet.payload.pop('capture_evidence')
        if failure == 'gap' and index >= 2:
            packet.payload['frame_ts'] += 20
    outputs, _ = asyncio.run(replay(monkeypatch, positions, area=True, mutate=mutate))
    assert not any(p.lifecycle == Lifecycle.OPEN for p in outputs)


def test_region_reentry_starts_new_stationary_duration(monkeypatch):
    outputs, _ = asyncio.run(replay(monkeypatch, [-3, -1.55, -1.4, -3, -1.55, -1.4, -1.4, -1.4], area=True))
    opened = [p for p in outputs if p.lifecycle == Lifecycle.OPEN]
    assert len(opened) == 1
    assert opened[0].payload['stationary_event']['candidate_since'] == 1005


@pytest.mark.parametrize('threshold', [0, .1, 1 / 3.6])
def test_zero_speed_stays_stopped(monkeypatch, threshold):
    async def scenario():
        clock = [1000.0]
        monkeypatch.setattr('time.time', lambda: clock[0])
        monkeypatch.setattr('time.monotonic', lambda: clock[0])
        velocity = VelocityEstimationRuntime({'stopped_speed_threshold': threshold})
        for index in range(5):
            clock[0] += 1
            packet = Packet.create(stream_id='camera', payload={'event_id': 'one', 'frame_ts': clock[0], 'world': {'x': 1, 'z': 1}})
            [out] = await velocity.process_packet(packet, None)
            if index:
                assert out.payload['velocity']['valid'] and out.payload['velocity']['stopped']
    asyncio.run(scenario())


def test_old_arrival_cannot_be_reimported_after_invalid_mapping(monkeypatch):
    async def scenario():
        from test_stationary_evidence import observed
        clock = [1000.0]
        monkeypatch.setattr('time.time', lambda: clock[0])
        monkeypatch.setattr('time.monotonic', lambda: clock[0])
        runtime = StationaryEventRuntime({'require_arrival': True, 'arrival_evidence_field': 'payload.velocity.arrival_evidence'})
        output = []
        for index in range(5):
            clock[0] = 1000 + index
            packet = observed(clock[0])
            packet.payload['velocity']['arrival_evidence'] = {'observed_at': 999, 'capture_instance': 'camera', 'generation': 0,
                'subject_id': 'one', 'source_stream_id': 'camera'}
            if index == 1:
                packet.payload.pop('world')
            output.extend(await runtime.process_packet(packet, None))
        assert not any(p.lifecycle == Lifecycle.OPEN for p in output)
    asyncio.run(scenario())


@pytest.mark.parametrize('speed,stopped,expected', [(0.25, False, False), (0.25, True, True), (0.25, None, True), (2, True, False)])
def test_motion_boolean_and_numeric_fallback(speed, stopped, expected):
    async def scenario():
        from test_stationary_evidence import observed
        runtime = StationaryEventRuntime({'min_stationary_seconds': 0, 'min_valid_samples': 1})
        packet = observed(1)
        packet.payload['velocity'] = {'valid': True, 'speed_mps': speed}
        if stopped is not None:
            packet.payload['velocity']['stopped'] = stopped
        output = await runtime.process_packet(packet, None)
        assert bool(output) is expected
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['old', 'future', 'capture', 'subject', 'source'])
def test_unbound_arrival_evidence_cannot_confirm(failure):
    async def scenario():
        from test_stationary_evidence import observed
        runtime = StationaryEventRuntime({'require_arrival': True, 'min_stationary_seconds': 0,
            'min_valid_samples': 1, 'arrival_evidence_field': 'payload.velocity.arrival_evidence'})
        packet = observed(1000)
        evidence = {'observed_at': 999, 'capture_instance': 'camera', 'generation': 0,
                    'subject_id': 'one', 'source_stream_id': 'camera'}
        if failure == 'old':
            evidence['observed_at'] = 990
        if failure == 'future':
            evidence['observed_at'] = 1001
        if failure == 'capture':
            evidence['generation'] = 2
        if failure == 'subject':
            evidence['subject_id'] = 'other'
        if failure == 'source':
            evidence['source_stream_id'] = 'other'
        packet.payload['velocity']['arrival_evidence'] = evidence
        assert await runtime.process_packet(packet, None) == []
    asyncio.run(scenario())


def test_duplicate_capture_cannot_extend_velocity_history(monkeypatch):
    async def scenario():
        from test_stationary_evidence import observed
        velocity = VelocityEstimationRuntime({})
        packet = observed(1000)
        packet.payload['event_id'] = 'one'
        await velocity.process_packet(packet, None)
        duplicate = replace(packet, payload={**packet.payload, 'frame_ts': 1001, 'world': {'x': 2, 'z': 1}})
        assert await velocity.process_packet(duplicate, None) == []
        assert len(next(iter(velocity._state_by_key.values())).samples) == 1
    asyncio.run(scenario())


def test_stationary_decision_telemetry_is_bounded():
    async def scenario():
        from types import SimpleNamespace
        from test_stationary_evidence import observed
        samples = []
        context = SimpleNamespace(observe_telemetry_numeric=lambda *args, **kwargs: samples.append(args))
        runtime = StationaryEventRuntime({'require_arrival': True})
        for i in range(10):
            await runtime.process_packet(observed(i), context)
        assert samples == [('stationary.decision.awaiting_arrival', 1.0)]
    asyncio.run(scenario())
