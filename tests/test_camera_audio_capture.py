import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from threading import Event, Thread
import time
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync_ext_cameras.audio_capture import AudioCapture
from toposync_ext_cameras.audio_events import AudioOccurrenceRuntime
from toposync_ext_cameras.audio_pipeline import AudioFeaturesRuntime
from toposync_ext_cameras.audio_source import AudioSourceRuntime


def test_audio_source_compiles_with_analysis():
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
    from toposync.runtime.pipelines.operator_registry import OperatorRegistry
    from toposync_ext_cameras.audio_pipeline import register_audio_operator
    registry = OperatorRegistry()
    register_audio_operator(registry)
    pipeline = Pipeline(name="audio", graph={"schema_version": 2, "uid": "audio",
        "nodes": [
            {"id": "source", "uid": "source", "operator": "camera.audio_source", "config": {"camera_id": "fixture"}},
            {"id": "analysis", "uid": "analysis", "operator": "camera.audio_features", "config": {}},
            {"id": "event", "uid": "event", "operator": "camera.audio_occurrence", "config": {}},
        ], "edges": [{"uid": "audio_analysis", "from": {"node": "source", "port": "out"},
                       "to": {"node": "analysis", "port": "in"},
                       "traffic": {"modality": "audio", "semantic_class": "frame", "continuous": True}},
                      {"uid": "analysis_event", "from": {"node": "analysis", "port": "out"},
                       "to": {"node": "event", "port": "in"},
                       "traffic": {"modality": "audio", "semantic_class": "frame", "continuous": True}}]})
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    assert compiled.nodes[-1].operator_id == "camera.audio_occurrence"


def test_real_decoder_and_source_lifecycle(tmp_path, monkeypatch):
    pytest.importorskip("av")
    path = tmp_path / "stereo.wav"
    signal = (8000 * np.sin(2 * np.pi * 1000 * np.arange(36000) / 16000)).astype(np.int16)
    samples = np.column_stack([signal, -signal])
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples.astype("<i2").tobytes())
    requests = []
    async def resolve(request, dependencies):
        requests.append(request)
        return SimpleNamespace(rtsp_url=str(path), source_id="main", camera_id="fixture")
    monkeypatch.setattr("toposync_ext_cameras.pipelines.operators.get_global_camera_capture_service",
                        lambda: SimpleNamespace(resolve=resolve))
    packets = []
    records = []
    store = NotificationStore(tmp_path / "notifications.sqlite3")
    async def upsert(**values):
        records.append(store.upsert(**values))
    analysis = AudioFeaturesRuntime({"baseline": {"reference": "synthetic-quiet", "rms": 0.01}}, None)
    events = AudioOccurrenceRuntime({})
    sink = NotifyRuntime({"dedupe_by_occurrence": True, "update_interval_seconds": 0,
                          "historical_evidence_paths": ["audio_analysis"]},
                         PipelineRuntimeDependencies(notifications_upsert=upsert))
    class Context:
        pipeline_name = "audio-capture-replay"
        node_id = "notify"
        def is_cancelled(self):
            return False
        async def run_blocking(self, function, *args, **kwargs):
            return function(*args, **kwargs)
        async def emit(self, packet, **kwargs):
            packets.append(packet)
            analyzed = (await analysis.process_packet(packet, self))[0]
            for occurrence in await events.process_packet(analyzed, self):
                await sink.process_packet(occurrence, self)
    async def scenario():
        await AudioSourceRuntime({"camera_id": "fixture", "source_id": "main"}, None).run(Context())
        assert len(requests) == 1 and requests[0].camera_id == "fixture"
        assert [len(packet.artifacts["audio"].data) for packet in packets[:-1]] == [16000, 16000, 4000]
        assert [packet.payload["media"]["ts"] for packet in packets[:-1]] == [0, 1, 2]
        assert np.array_equal(np.concatenate([p.artifacts["audio"].data for p in packets[:-1]]), samples)
        for packet in packets[:-1]:
            assert packet.artifacts["audio"].private
            analyzed = (await AudioFeaturesRuntime({}, None).process_packet(packet, Context()))[0]
            assert analyzed.payload["audio_analysis"]["status"] == "audible"
            assert analyzed.payload["audio_analysis"]["clock"]["basis"] == "receipt"
            assert analyzed.payload["audio_analysis"]["correlation"]["status"] == "unavailable"
        assert packets[-1].lifecycle == Lifecycle.CLOSE and packets[-1].artifacts == {}
        assert sum(created for _, created in records) == 1
        assert records[-1][0].payload["status"] == "closed"
        saved = records[-1][0].payload["data"]["historical_evidence"]["audio_analysis"]
        assert saved["duration_seconds"] == 0.25
        assert saved["windows"][0]["anomaly"]["status"] == "above_baseline"
        readers = []
        class TrackedCapture(AudioCapture):
            def __init__(self, location):
                super().__init__(location)
                readers.append(self)
        monkeypatch.setattr("toposync_ext_cameras.audio_source.AudioCapture", TrackedCapture)
        class CancelContext(Context):
            cancelled = False
            def is_cancelled(self):
                return self.cancelled
            async def emit(self, packet, **kwargs):
                self.cancelled = True
        await AudioSourceRuntime({"camera_id": "fixture"}, None).run(CancelContext())
        assert len(readers) == 1 and readers[0]._container is None
        with pytest.raises(ValueError, match="configured camera"):
            await AudioSourceRuntime({}, None).run(Context())
        assert len(requests) == 2  # Missing configuration never resolves/opens a source.
    asyncio.run(scenario())


def test_no_audio_loss_and_closed_reader(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "video.avi"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=1)
        stream.width = stream.height = 16
        stream.pix_fmt = "yuv420p"
        frame = av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    reader = AudioCapture(str(path))
    assert reader.read()[0] == "no_audio"
    reader.close()
    assert reader._container is None
    assert reader.read()[0] == "ended"
    missing = AudioCapture(str(tmp_path / "missing.wav"))
    assert missing.read()[0] == "stream_lost"
    missing.close()


def test_loopback_audio_eof_and_stalled_transport_release_resources():
    pytest.importorskip("av")
    data = BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(np.zeros(20000, dtype="<i2").tobytes())
    release = Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path == "/stall":
                release.wait(10)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data.getvalue())))
            self.end_headers()
            self.wfile.write(data.getvalue())
    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    reader = stalled = None
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        reader = AudioCapture(url + "/audio")
        first, second, terminal = reader.read(), reader.read(), reader.read()
        assert first[0] == second[0] == "available"
        assert len(first[1]) == 16000 and len(second[1]) == 4000
        assert np.count_nonzero(first[1]) == 0  # A silent track is still audio.
        assert terminal[0] == "stream_lost"
        stalled = AudioCapture(url + "/stall")
        started = time.monotonic()
        assert stalled.read()[0] == "stream_lost"
        assert time.monotonic() - started < 8  # Five-second timeout, scheduling allowance.
    finally:
        release.set()
        if reader is not None:
            reader.close()
        if stalled is not None:
            stalled.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not worker.is_alive()
