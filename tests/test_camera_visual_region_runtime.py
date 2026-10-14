import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines.visual_region import VisualRegionRuntime
from toposync_ext_cameras.pipelines.region_occurrence import RegionOccurrenceRuntime

CONFIG = {"camera_id":"one", "source_id":"video", "name":"Nicho", "reference":{"artifact_id":"reference", "revision":1,"geometry_digest":"digest"}, "strokes":[{"diameter01":.1,"points01":[[.5,.5]]}]}


def observation(sequence, classification="inside"):
    evidence = {"capture_instance":"decoder", "generation":0,"sequence":sequence,"published_at":time.time()}
    return Packet.create(stream_id="cat", payload={
        "source":{"device_id":"one","source_id":"video"}, "frame_ts":float(sequence),
        "subject":{"id":"cat","category":"cat","bbox01":[.4,.4,.6,.6]},
        "capture_evidence":evidence,
        "region_observation":{"classification":classification,"geometry_key":"one"},
    }, artifacts={"main":Artifact(name="main",data=np.zeros((100,200,3),np.uint8),metadata={"image_geometry":image_geometry(200,100,evidence)})})


def test_filter_keeps_outside_and_unknown_on_observations_port_and_closes_primary():
    async def scenario():
        services = ServiceRegistry()
        classification = "inside"
        async def evaluate(**kwargs):
            return {"classification":"observed", "observations":[{"classification":classification,"reason":"test"}]}
        services.register("cameras.visual_region.evaluate",evaluate)
        emitted=[]
        async def emit(packet,port): emitted.append((port,packet))
        runtime = VisualRegionRuntime(CONFIG,PipelineRuntimeDependencies(services=services))
        context=SimpleNamespace(emit=emit)
        assert (await runtime.process_packet(observation(1),context))[0].lifecycle == Lifecycle.OPEN
        classification="outside"
        assert (await runtime.process_packet(observation(2),context))[0].lifecycle == Lifecycle.CLOSE
        classification="indeterminate"
        assert await runtime.process_packet(observation(3),context) == []
        assert [p.payload["region_observation"]["classification"] for _,p in emitted] == ["inside","outside","indeterminate"]
        assert all(port=="observations" for port,_ in emitted)
        assert await runtime.process_packet(observation(2),context) == []
    asyncio.run(scenario())


def test_observed_entry_requires_outside_and_unknown_resets_duration():
    async def scenario():
        runtime=RegionOccurrenceRuntime({"mode":"entry","minimum_duration_seconds":1})
        assert await runtime.process_packet(observation(1),None)==[]
        assert await runtime.process_packet(observation(2,"outside"),None)==[]
        assert await runtime.process_packet(observation(3),None)==[]
        event=(await runtime.process_packet(observation(4),None))[0]
        assert event.lifecycle==Lifecycle.OPEN and "region_occurrence" in event.payload
        event=(await runtime.process_packet(observation(5,"indeterminate"),None))[0]
        assert event.lifecycle==Lifecycle.CLOSE and event.payload["region_occurrence"]["status"]=="unconfirmed"
        assert await runtime.process_packet(observation(6),None)==[]
        assert await runtime.process_packet(observation(7),None)==[]
    asyncio.run(scenario())


def test_missing_runtime_and_wrong_source_are_unknown_without_output():
    async def scenario():
        emitted=[]
        async def emit(packet,port): emitted.append(packet)
        runtime=VisualRegionRuntime(CONFIG,PipelineRuntimeDependencies(services=ServiceRegistry()))
        packet=observation(1)
        packet.payload["source"]["source_id"]="other"
        assert await runtime.process_packet(packet,SimpleNamespace(emit=emit))==[]
        assert emitted[0].payload["region_observation"]["classification"]=="indeterminate"
    asyncio.run(scenario())


def test_reentry_has_new_scope_and_old_capture_close_cannot_end_it():
    async def scenario():
        services = ServiceRegistry()
        async def evaluate(**kwargs):
            return {'classification': 'observed', 'observations': [{'classification': 'inside'}]}
        services.register('cameras.visual_region.evaluate', evaluate)
        emitted = []
        async def emit(packet, port): emitted.append((port, packet))
        runtime = VisualRegionRuntime(CONFIG, PipelineRuntimeDependencies(services=services))
        context = SimpleNamespace(emit=emit)
        first = (await runtime.process_packet(observation(1), context))[0]
        key = next(iter(runtime.forwarded))
        runtime.forwarded[key] = (first, time.monotonic() - 100)
        closed = (await runtime.on_idle(context))[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload['region_observation']['classification'] == 'indeterminate'
        second = (await runtime.process_packet(observation(2), context))[0]
        assert second.lifecycle == Lifecycle.OPEN and second.stream_id != first.stream_id
        stale = replace(observation(3), lifecycle=Lifecycle.CLOSE)
        stale.payload['capture_evidence']['generation'] = -1
        assert await runtime.process_packet(stale, context) == []
        assert runtime.forwarded
        final = (await runtime.process_packet(replace(observation(4), lifecycle=Lifecycle.CLOSE), context))[0]
        assert final.stream_id == second.stream_id and not runtime.forwarded
    asyncio.run(scenario())


def test_occurrence_rejects_filtered_connection_and_remote_execution_is_explicit():
    from toposync_ext_cameras.pipelines.region_occurrence import region_occurrence_diagnostics
    from toposync_ext_cameras.pipelines.visual_region import visual_region_diagnostics
    context = {'upstream_nodes': [{'node_id': 'region', 'operator_id': 'camera.panorama_region'}],
               'incoming_edges': [{'from': {'node': 'region', 'port': 'out'}}]}
    assert region_occurrence_diagnostics({}, context)[0].code == 'visual_region_observations_required'
    context['incoming_edges'][0]['from']['port'] = 'observations'
    assert region_occurrence_diagnostics({}, context) == []
    assert visual_region_diagnostics(CONFIG, {'check_runtime_prerequisites': True, 'processing_server_id': 'remote'})[0].code == 'visual_region_remote_reference_unavailable'


def test_application_catalog_registers_visual_region_operators(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from toposync.app import create_app
    monkeypatch.setenv('TOPOSYNC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TOPOSYNC_AUTH_MODE', 'bypass')
    monkeypatch.setenv('TOPOSYNC_NO_FRONTEND', '1')
    with TestClient(create_app()) as client:
        response = client.get('/api/pipelines/operators')
        assert response.status_code == 200
        operators = {item['id']: item for item in response.json()['operators']}
        assert 'camera.region_occurrence' in operators
        assert {port['name'] for port in operators['camera.panorama_region']['outputs']} == {'out', 'observations'}
