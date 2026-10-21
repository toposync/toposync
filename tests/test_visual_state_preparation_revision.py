from __future__ import annotations

import asyncio
from dataclasses import replace
import io

import numpy as np
from PIL import Image
import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.preview import PipelinePreviewError, reference_input_pipeline
from toposync.runtime.pipelines.runtime import Artifact
from toposync_ext_cameras.visual_region_models import CanonicalRegionConfig
from toposync_ext_vision.visual_state.preparation import prepare_visual_frame, visual_preparation_revision, VisualRegionUnavailable
from toposync_ext_vision.visual_state.preview import VisualStatePreviewRunner
from toposync_ext_vision.visual_state.runtime import VisualStateRuntime
from toposync_ext_vision.visual_state.schemas import ReferenceEvidence, VisualStateConfig

from test_camera_visual_region_service import setup
from test_visual_state_preview import RealClock, preview_registry
from test_visual_state_runtime import frame, harness, until
from test_visual_state_store import image_bytes


def canonical_configuration(configuration, prefix, region):
    prefix.graph['nodes'][0]['config'] = {'camera_id': region.camera_id, 'source_id': region.source_id}
    value = {**configuration.model_dump(), 'canonical_region': region.model_dump(), 'use_full_image': False,
             'source_identity': f'camera:{region.camera_id}:{region.source_id}', 'region_id': 'region',
             'classes': [{**category.model_dump(), 'reference_ids': []} for category in configuration.classes]}
    candidate = VisualStateConfig.model_validate(value)
    return candidate.model_copy(update={'preparation_revision': visual_preparation_revision(candidate, prefix)})


def region_configuration():
    return CanonicalRegionConfig(camera_id='one', source_id='video', name='Door',
        reference={'artifact_id': 'reference', 'revision': 1, 'geometry_digest': 'digest'},
        strokes=[{'diameter01': .2, 'points01': [[.5, .5]]}])


def test_capture_runner_and_runtime_receive_identical_canonical_pixels_with_real_localization(tmp_path):
    async def scenario():
        (tmp_path / "sensor").mkdir()
        original, context, _, store, saved = await harness(tmp_path / 'sensor')
        service, region, source, _, counts = setup(tmp_path / 'panorama')
        region = CanonicalRegionConfig.model_validate({**region.model_dump(), 'maximum_side': 128,
                                                        'evidence_timeout_seconds': 3})
        prefix = reference_input_pipeline(saved, 'state')
        configuration = canonical_configuration(original.config, prefix, region)
        configuration = configuration.model_copy(update={'temporal': configuration.temporal.model_copy(update={
            'maximum_frame_age_ms': 3000, 'validity_ms': 3000})})
        original.dependencies.services.register('cameras.visual_region.prepare', service.prepare)
        packet = frame(RealClock(), 1)
        packet = replace(packet, stream_id=configuration.source_identity, artifacts={'main': Artifact(name='main',
            data=source, metadata={'image_geometry': image_geometry(960, 540, packet.payload['capture_evidence'])})})
        from toposync.runtime.pipelines.execution import BaseOperatorRuntime

        class Source(BaseOperatorRuntime):
            async def run(self, context):
                await context.emit(packet)
                while not context.is_cancelled():
                    await context.sleep(.1)

        registry = preview_registry()
        registry._items['camera.source'] = replace(registry.get('camera.source'), runtime_factory=lambda *_: Source())
        captured_inputs = []

        class Backend:
            def embed(self, image):
                captured_inputs.append(image.copy())
                return np.array([1., 0., 0.])
            def close(self):
                pass

        runtime = VisualStateRuntime(configuration.model_dump(), original.dependencies)
        runtime._prefix, runtime._backend = prefix, Backend()
        runner = VisualStatePreviewRunner()
        try:
            snapshot = await runner.run(owner='owner', configuration=configuration, pipeline=prefix, registry=registry,
                                        dependencies=original.dependencies, capture=True)
            _, observed = await runtime._analyze(context, packet)
            with Image.open(io.BytesIO(snapshot['image'])) as image:
                np.testing.assert_array_equal(np.asarray(image)[:, :, ::-1], captured_inputs[0])
            assert snapshot['evidence']['sample'] == observed['sample']
            assert snapshot['evidence']['preparation_revision'] == observed['preparation_revision'] == configuration.preparation_revision
            assert observed['prepared_region'] is True
            assert max(captured_inputs[0].shape[:2]) == 128
            assert packet.artifacts['main'].data is source
            np.testing.assert_array_equal(packet.artifacts['main'].metadata['image_geometry']['to_source'], np.eye(3))
            assert counts == {'capture': 0, 'motion': 0}
            assert not runner._owners and not runner._tasks
        finally:
            await runner.shutdown()
            await runtime.shutdown()
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['missing', 'revision', 'sample', 'reference', 'pixels', 'stale'])
def test_prepared_service_response_must_match_revision_sample_and_freshness(tmp_path, fault):
    async def scenario():
        runtime, context, clock, store, saved = await harness(tmp_path)
        prefix = reference_input_pipeline(saved, 'state')
        region = region_configuration()
        configuration = canonical_configuration(runtime.config, prefix, region)
        packet = replace(frame(clock, 1), stream_id=configuration.source_identity)

        async def prepare(**arguments):
            result = {'status': 'ready', 'preparation_revision': region.geometry_key(),
                'capture_evidence': arguments['evidence'], 'reference': region.reference.model_dump(),
                '_image': arguments['image']}
            if fault == 'missing':
                return {'status': 'unavailable', 'reason': 'visual_region_outside'}
            if fault == 'revision':
                result['preparation_revision'] = 'changed'
            if fault == 'sample':
                result['capture_evidence'] = {**arguments['evidence'], 'sequence': 9}
            if fault == 'reference':
                result['reference'] = {**result['reference'], 'revision': 2}
            if fault == 'pixels':
                result['_image'] = np.zeros((513, 513, 3), dtype=np.uint8)
            if fault == 'stale':
                clock.now += 1
            return result

        runtime.dependencies.services.register('cameras.visual_region.prepare', prepare)
        try:
            with pytest.raises(PipelinePreviewError if fault == 'stale' else VisualRegionUnavailable):
                await prepare_visual_frame(configuration, prefix, packet, runtime.dependencies.services,
                                            clock=clock, unix_clock=clock.unix)
        finally:
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(scenario())


def test_region_loss_reconfirms_on_return_and_slow_preparation_does_not_delay_expiry(tmp_path):
    async def scenario():
        original, context, clock, store, saved = await harness(tmp_path)
        prefix = reference_input_pipeline(saved, 'state')
        region = region_configuration()
        configuration = canonical_configuration(original.config, prefix, region)
        draft = store.create_draft(configuration)
        for color in ('red', 'blue'):
            draft, _ = store.capture(draft.draft_id, color, image_bytes(color), ReferenceEvidence(
                source_identity=configuration.source_identity, preparation_revision=configuration.preparation_revision,
                sample={'capture_instance': 'capture', 'generation': 0, 'sequence': 1}, observed_at=clock.unix(),
                freshness_basis='receipt', prepared_region=True), operation_id=color, expected_revision=draft.revision)
        mode = 'ready'
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(**arguments):
            if mode == 'slow':
                entered.set()
                await release.wait()
            if mode == 'missing':
                return {'status': 'unavailable'}
            return {'status': 'ready', 'preparation_revision': region.geometry_key(), 'capture_evidence': arguments['evidence'],
                    'reference': region.reference.model_dump(), '_image': arguments['image']}

        original.dependencies.services.register('cameras.visual_region.prepare', prepare)
        runtime = VisualStateRuntime(draft.configuration.model_dump(), original.dependencies,
            preview_input_pipeline=prefix, clock=clock, unix_clock=clock.unix)
        await runtime.prepare(context)
        task = asyncio.create_task(runtime.run(context))

        async def emit(sequence):
            clock.now += .26
            await context.inputs['in'].put(replace(frame(clock, sequence), stream_id=configuration.source_identity))

        async def observed(sequence):
            await until(lambda: runtime._reducer._last_seen is not None and runtime._reducer._last_seen.sample.sequence == sequence)

        try:
            await emit(1)
            await observed(1)
            await emit(2)
            await observed(2)
            assert runtime._reducer.snapshot().class_id == 'red'
            mode = 'missing'
            await emit(3)
            await until(lambda: runtime._reducer.snapshot().reason == 'region_unavailable')
            state = runtime._reducer.snapshot()
            assert state.status == 'unavailable' and state.class_id is None and state.valid_for_ms == 0
            mode = 'ready'
            await emit(4)
            await observed(4)
            assert runtime._reducer.snapshot().status == 'pending'
            await emit(5)
            await observed(5)
            assert runtime._reducer.snapshot().class_id == 'red'
            mode = 'slow'
            await emit(6)
            await entered.wait()
            clock.now += 2.1
            await until(lambda: context.packets[-1].payload['visual_state']['reason'] == 'observation_expired')
            assert context.packets[-1].payload['visual_state']['class_id'] is None
            release.set()
            await until(lambda: runtime._reducer.snapshot().reason == 'input_unobservable')
            assert runtime._reducer.snapshot().class_id is None
        finally:
            release.set()
            context.cancel_event.set()
            await task
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(scenario())


def test_geometry_revision_and_source_bindings_reject_old_examples_before_model_loading(tmp_path):
    async def scenario():
        original, context, _, store, saved = await harness(tmp_path)
        prefix = reference_input_pipeline(saved, 'state')
        region = region_configuration()
        configuration = canonical_configuration(original.config, prefix, region)
        revision = configuration.preparation_revision
        assert visual_preparation_revision(configuration.model_copy(update={'canonical_region': region.model_copy(update={'name': 'New name'})}), prefix) == revision
        for change in ({'maximum_side': 64}, {'alignment_margin_pixels': 12}, {'strokes': []}):
            changed = configuration.model_copy(update={'canonical_region': region.model_copy(update=change)})
            assert visual_preparation_revision(changed, prefix) != revision
        incompatible = configuration.model_copy(update={
            'canonical_region': region.model_copy(update={'maximum_side': 64}), 'classes': original.config.classes})
        runtime = VisualStateRuntime(incompatible.model_dump(), original.dependencies, preview_input_pipeline=prefix,
            backend_factory=lambda *_: pytest.fail('incompatible references must fail before model loading'))
        with pytest.raises(ValueError, match='preparation changed'):
            await runtime.prepare(context)
        wrong_source = prefix.model_copy(deep=True)
        wrong_source.graph['nodes'][0]['config']['source_id'] = 'other'
        with pytest.raises(ValueError, match='bound camera source'):
            visual_preparation_revision(configuration, wrong_source)
        wrong_packet = frame(RealClock(), 1)
        with pytest.raises(ValueError, match='source changed'):
            await prepare_visual_frame(configuration, prefix, wrong_packet, original.dependencies.services)
        for extra in ({'use_full_image': True}, {'canonical_region': {**region.model_dump(), 'framing': 'maintain'}}):
            with pytest.raises(ValidationError):
                VisualStateConfig.model_validate({**configuration.model_dump(), **extra})
        dynamic = prefix.model_copy(deep=True)
        dynamic.graph['nodes'].append({'id': 'crop', 'uid': 'crop', 'operator': 'camera.image_crop', 'config': {'region_mode': 'motion'}})
        dynamic.graph['edges'].append({'uid': 'crop', 'from': {'node': 'source', 'port': 'out'}, 'to': {'node': 'crop', 'port': 'in'}, 'traffic': {'modality': 'video'}})
        with pytest.raises(PipelinePreviewError, match='stable prepared region'):
            visual_preparation_revision(configuration, dynamic)
        await context.scheduler.shutdown()
        store.close()
    asyncio.run(scenario())


def test_cancelled_capture_keeps_admission_until_native_encoding_finishes(tmp_path, monkeypatch):
    import threading
    from toposync_ext_vision.visual_state import preview as preview_module

    async def scenario():
        original, context, _, store, saved = await harness(tmp_path)
        prefix = reference_input_pipeline(saved, 'state')
        entered, release = threading.Event(), threading.Event()
        encode = preview_module._encode_capture

        def slow_encode(image):
            entered.set()
            assert release.wait(5)
            return encode(image)

        monkeypatch.setattr(preview_module, '_encode_capture', slow_encode)
        runner = VisualStatePreviewRunner()
        task = asyncio.create_task(runner.run(owner='owner', configuration=original.config, pipeline=prefix,
            registry=preview_registry(), dependencies=original.dependencies, capture=True))
        try:
            await until(entered.is_set)
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done() and runner._owners == {'owner'}
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not runner._owners and not runner._tasks
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runner.shutdown()
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(scenario())
