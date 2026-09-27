import asyncio
from dataclasses import replace

import numpy as np

from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.audio_correlation import AudioCorrelationRuntime, audio_signature, compare_signatures
from toposync_ext_cameras.audio_pipeline import AudioFeaturesRuntime


class Context:
    async def run_blocking(self, function, *args, **kwargs):
        return function(*args, **kwargs)


def signal(seed=1, rate=16000):
    # Ruído modulado para testar o contrato; não substitui replay de fala multicaptura.
    noise = np.random.default_rng(seed).normal(size=rate)
    modulation = np.interp(np.arange(rate), np.linspace(0, rate - 1, 20),
                            np.random.default_rng(seed + 100).uniform(.02, .6, 20))
    return (np.clip(noise * modulation, -.9, .9) * 32767).astype(np.int16)


def packet(source, *, samples=None, started=10, capture="capture", basis="capture", reference="clock"):
    metadata = {"source_id": source, "capture_id": capture, "sample_rate": 16000,
                "availability": "available",
                "clock": {"started_at": started, "basis": basis, "uncertainty_seconds": .01,
                          "synchronization_reference": reference}}
    return Packet.create(stream_id=source, payload={"camera_id": source, "audio_capture": metadata}).with_artifact(
        Artifact(name="audio", data=signal() if samples is None else samples, metadata=metadata, private=True))


def test_signature_is_bounded_abstains_and_preserves_gain():
    original = audio_signature(signal(), 16000)
    assert original.shape == (12, 97)
    assert compare_signatures(original, audio_signature(signal() // 2, 16000))["score"] > .99
    assert audio_signature(np.zeros(16000, dtype=np.int16), 16000) is None
    assert audio_signature(signal(), 8000) is None
    assert audio_signature(np.zeros((16000, 2), dtype=np.int16), 16000) is None
    tone = (np.sin(2 * np.pi * 1000 * np.arange(16000) / 16000) * 8000).astype(np.int16)
    assert compare_signatures(audio_signature(tone, 16000), audio_signature(tone, 16000)) is None


def test_real_feature_transport_source_clock_close_and_order():
    async def scenario():
        context = Context()
        runtime = AudioCorrelationRuntime({})
        features = AudioFeaturesRuntime({"baseline": {"reference": "quiet", "rms": .01}}, None)
        first = (await features.process_packet(packet("a"), context))[0]
        second = (await features.process_packet(packet("b"), context))[0]
        assert first.artifacts["audio"].metadata["encoding"] == "wav_pcm16"
        assert (await runtime.process_packet(first, context, "first")).payload["audio_correlation"]["status"] == "unavailable"
        output = await runtime.process_packet(second, context, "second")
        match = output.payload["audio_correlation"]
        assert match["status"] == "similar" and match["score"] > .99
        assert match["same_sound_confirmed"] is False and match["calibrated"] is False
        assert output.packet_id == second.packet_id and output.lifecycle == second.lifecycle
        assert output.artifacts == second.artifacts
        assert match["candidate"] == {"camera_id": "a", "source_id": "a", "capture_id": "capture"}
        # Uma regressão não apaga o watermark e não pode reintroduzir dados antigos.
        for _ in range(2):
            result = await runtime.process_packet(first, context, "first")
            assert result.payload["audio_correlation"]["reason"] == "capture_changed_or_out_of_order"
        await runtime.process_packet(replace(first, lifecycle=Lifecycle.CLOSE, artifacts={}), context, "first")
        assert "first" not in runtime._recent
        assert (await runtime.process_packet(first, context, "first")).payload["audio_correlation"]["reason"] == "closed_capture_or_session_capacity"
        await runtime.shutdown()
        assert not runtime._recent and not runtime._closed

        for changed in ({"basis": "receipt"}, {"reference": "other"}):
            runtime = AudioCorrelationRuntime({})
            await runtime.process_packet(packet("a"), context, "first")
            result = await runtime.process_packet(packet("b", **changed), context, "second")
            assert result.payload["audio_correlation"]["status"] == "unavailable"
        runtime = AudioCorrelationRuntime({})
        await runtime.process_packet(packet("a"), context, "first")
        duplicate = await runtime.process_packet(packet("a"), context, "second")
        assert duplicate.payload["audio_correlation"]["reason"] == "distinct_sources_required"
        swapped = await runtime.process_packet(packet("c"), context, "first")
        assert swapped.payload["audio_correlation"]["reason"] == "source_changed"
        isolated = AudioCorrelationRuntime({})
        assert (await isolated.process_packet(packet("b"), context, "second")).payload["audio_correlation"]["status"] == "unavailable"
    asyncio.run(scenario())


def test_two_input_run_preserves_all_packets_and_releases_reads():
    async def scenario():
        class Inputs(Context):
            inputs = {"first": object(), "second": object()}
            def __init__(self):
                self.queues = {port: asyncio.Queue() for port in self.inputs}
                self.outputs = []
                self.stopped = False
            def is_cancelled(self):
                return self.stopped
            async def read(self, *, port):
                return await self.queues[port].get()
            async def emit(self, value, *, port):
                assert port == "out"
                self.outputs.append(value)
                if len(self.outputs) == 4:
                    self.stopped = True
        context = Inputs()
        original = [packet("a"), packet("b")]
        for port, item in zip(context.inputs, original):
            context.queues[port].put_nowait(item)
            context.queues[port].put_nowait(replace(item, lifecycle=Lifecycle.CLOSE, artifacts={}))
        runtime = AudioCorrelationRuntime({})
        await asyncio.wait_for(runtime.run(context), 1)
        assert len(context.outputs) == 4
        for item in original:
            assert [row.lifecycle for row in context.outputs if row.stream_id == item.stream_id] == [Lifecycle.UPDATE, Lifecycle.CLOSE]
        assert not runtime._recent
    asyncio.run(scenario())


def test_compiler_accepts_two_ports_and_private_origin_boundary():
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines import OperatorRegistry, register_builtin_operators
    from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
    from toposync.runtime.pipelines.distributed.plan import build_distributed_graphs, required_transport_capabilities
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.audio_pipeline import register_audio_operator

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_audio_operator(registry)
    nodes, edges = [], []
    for port in ("first", "second"):
        nodes.extend([
            {"id": port, "operator": "camera.audio_source", "config": {"camera_id": port, "source_id": port}},
            {"id": port + "_analysis", "operator": "camera.audio_features", "config": {}},
        ])
        for source, target, target_port in [(port, port + "_analysis", "in"),
                                             (port + "_analysis", "correlate", port)]:
            edges.append({"from": {"node": source, "port": "out"}, "to": {"node": target, "port": target_port},
                          "traffic": {"modality": "audio", "semantic_class": "frame", "continuous": True},
                          "queue": {"max_items": 4, "drop_policy": "block"},
                          "backpressure": {"mode": "pause_upstream"}})
    nodes.append({"id": "correlate", "operator": "camera.audio_correlation", "config": {}})
    pipeline = Pipeline(name="two_microphones", graph=build_pipeline_graph_v2(
        graph_uid="two_microphones", nodes=nodes, edges=edges))
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    assert len(compiled.edges) == 4
    from toposync.runtime.pipelines.execution import PipelineRuntime
    runtime = PipelineRuntime(compiled=compiled, registry=registry)
    assert set(runtime._runtime_by_node) == {"first", "second", "first_analysis", "second_analysis", "correlate"}
    asyncio.run(runtime.stop())
    graphs = build_distributed_graphs(pipeline, registry)
    assert "correlate" in {node["id"] for node in graphs.origin_graph["nodes"]}
    assert required_transport_capabilities([pipeline], registry) == {"private_artifacts_v1"}
