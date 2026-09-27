import asyncio
from dataclasses import replace
from io import BytesIO
import sys
from types import SimpleNamespace
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pytest

from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.audio import AudioBaseline, AudioClock, analyze_pcm, analyze_wav
from toposync_ext_cameras.audio_api import install_audio_routes
from toposync_ext_cameras.audio_pipeline import AudioFeaturesRuntime, register_audio_operator
from toposync_ext_cameras.audio_transcription import MODEL_DIRECTORY, _inference_lock, transcribe_pcm


def wav_bytes(samples, rate=16000):
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1 if samples.ndim == 1 else samples.shape[1])
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.astype("<i2").tobytes())
    return output.getvalue()


def tone():
    return (8000 * np.sin(2 * np.pi * 1000 * np.arange(16000) / 16000)).astype(np.int16)


def test_wav_route_reports_frequency_clock_and_baseline_without_location():
    app = FastAPI()
    install_audio_routes(app)
    with TestClient(app) as client:
        response = client.post(
            "/api/cameras/audio/analyze?started_at=123&clock_basis=synthetic"
            "&baseline_rms=0.01&baseline_reference=quiet-test",
            content=wav_bytes(np.concatenate([np.zeros(16000, dtype=np.int16), tone()])),
        )
    assert response.status_code == 200
    result = response.json()
    assert result["sample_rate"] == 16000 and result["clock"]["basis"] == "synthetic"
    assert result["clock"]["uncertainty_seconds"] is None
    silent, audible = result["windows"]
    assert silent["status"] == "silent" and silent["dominant_frequency_hz"] is None
    assert audible["started_at"] == 124 and audible["offset_seconds"] == 1
    assert audible["dominant_frequency_hz"] == pytest.approx(1000)
    assert audible["anomaly"]["status"] == "above_baseline"
    assert audible["anomaly"]["confidence"] is None
    assert result["localization"]["status"] == "unavailable"
    assert result["transcription"]["status"] == "unavailable"


def test_audio_states_are_distinct_and_stereo_does_not_cancel():
    clock = AudioClock(started_at=10, basis="receipt")
    statuses = {
        analyze_pcm(None, None, clock=clock, availability="no_audio")["status"],
        analyze_pcm(None, None, clock=clock, availability="stream_lost")["status"],
        analyze_pcm(None, None, clock=clock)["status"],
        analyze_pcm(np.zeros(16000, dtype=np.int16), 16000, clock=clock)["status"],
    }
    assert statuses == {"no_audio", "stream_lost", "audio_missing", "silent"}
    stereo = np.column_stack([tone(), -tone()])
    result = analyze_wav(wav_bytes(stereo), clock=clock)
    assert result["status"] == "audible"
    assert result["windows"][0]["dominant_frequency_hz"] == pytest.approx(1000)
    assert result["windows"][0]["anomaly"]["status"] == "unavailable"


def test_transcription_is_optional_bounded_and_keeps_analysis_when_model_is_missing(tmp_path):
    app = FastAPI()
    install_audio_routes(app)
    with TestClient(app) as client:
        path = "/api/cameras/audio/analyze?started_at=0&clock_basis=synthetic&transcribe=true"
        result = client.post(path, content=wav_bytes(tone())).json()
        assert result["status"] == "audible"
        assert result["transcription"]["reason"] == "transcription_model_not_installed"
        result = client.post(path, content=wav_bytes(np.zeros(16000, dtype=np.int16))).json()
        assert result["transcription"]["status"] == "no_speech"
        assert result["transcription"]["text"] == ""
        assert client.post(path + "&language=../bad", content=wav_bytes(tone())).status_code == 422
        assert client.post(path, content=wav_bytes(np.zeros(31 * 16000, dtype=np.int16))).status_code == 422
    # Busy admission is tested without loading an inference model.
    directory = tmp_path / MODEL_DIRECTORY
    directory.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (directory / name).touch()
    assert _inference_lock.acquire(blocking=False)
    try:
        assert transcribe_pcm(tone(), 16000, data_dir=tmp_path)["status"] == "busy"
    finally:
        _inference_lock.release()


def test_noise_short_impulse_clipping_and_explicit_baseline():
    clock = AudioClock(started_at=0, basis="synthetic")
    noise = np.random.default_rng(1).integers(-100, 100, 16000, dtype=np.int16)
    baseline = AudioBaseline(reference="synthetic-normal-noise", rms=0.01)
    normal = analyze_pcm(noise, 16000, clock=clock, baseline=baseline)
    assert normal["windows"][0]["anomaly"]["status"] == "within_baseline"
    noise[400:560] = 32767
    short = analyze_pcm(noise, 16000, clock=clock, baseline=baseline)["windows"][0]
    assert short["peak"] > 0.99 and short["clipped_fraction"] == pytest.approx(0.01)
    assert short["anomaly"]["status"] == "above_baseline"


def test_transcription_preserves_uncalibrated_scores_and_rejects_invalid_values(monkeypatch, tmp_path):
    directory = tmp_path / MODEL_DIRECTORY
    directory.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (directory / name).touch()
    scores = [(-0.4, 0.02), (float("nan"), 1.1), (True, float("inf")), (None, None)]
    segments = [SimpleNamespace(start=0, end=1, text="Texto de fixture", tokens=[1],
                               avg_logprob=log, no_speech_prob=no_speech) for log, no_speech in scores]
    class Model:
        def __init__(self, *args, **kwargs):
            pass
        def transcribe(self, *args, **kwargs):
            return iter(segments), SimpleNamespace(language="pt")
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    result = transcribe_pcm(tone(), 16000, data_dir=tmp_path, language="pt")
    assert result["status"] == "transcribed"
    for index, row in enumerate(result["segments"]):
        assert row["confidence"] is None
        assert row["decoder_scores"] == {
            "average_log_probability": -0.4 if index == 0 else None,
            "no_speech_probability": 0.02 if index == 0 else None,
            "calibrated": False,
        }
        assert row["text"] == "Texto de fixture" and row["duration_seconds"] == 1


def test_wav_input_validation_and_explicit_clock():
    app = FastAPI()
    install_audio_routes(app)
    with TestClient(app) as client:
        assert client.post("/api/cameras/audio/analyze", content=wav_bytes(tone())).status_code == 422
        path = "/api/cameras/audio/analyze?started_at=0&clock_basis=capture"
        assert client.post(path, content=b"not audio").status_code == 422
        assert client.post(path, content=wav_bytes(tone())[:-10]).status_code == 422
        assert client.post(path + "&baseline_rms=0.1", content=wav_bytes(tone())).status_code == 422
        assert client.post(path.replace("started_at=0", "started_at=nan"), content=b"x").status_code == 422


def test_audio_operator_preserves_occurrence_and_exposes_features():
    class Context:
        async def run_blocking(self, function, *args, **kwargs):
            return function(*args, **kwargs)

    async def scenario():
        registry = OperatorRegistry()
        register_audio_operator(registry)
        assert registry.get("camera.audio_features") is not None
        runtime = AudioFeaturesRuntime({}, None)
        packet = Packet.create(stream_id="test", lifecycle=Lifecycle.OPEN, payload={"source_id": "synthetic"})
        packet = packet.with_artifact(Artifact(name="audio", data=tone(), metadata={
            "sample_rate": 16000, "clock": {"started_at": 5, "basis": "capture", "uncertainty_seconds": 0.02},
        }))
        result = (await runtime.process_packet(packet, Context()))[0]
        assert result.packet_id == packet.packet_id and result.lifecycle == packet.lifecycle
        assert result.payload["source_id"] == "synthetic"
        assert result.payload["audio_analysis"]["windows"][0]["dominant_frequency_hz"] == 1000
        assert runtime.config.transcribe is False
        enabled = AudioFeaturesRuntime({"transcribe": True, "language": "pt"}, None)
        transcribed = (await enabled.process_packet(packet, Context()))[0]
        assert transcribed.packet_id == packet.packet_id and transcribed.lifecycle == packet.lifecycle
        assert transcribed.payload["audio_analysis"]["transcription"]["reason"] == "transcription_model_not_installed"
        invalid = Packet.create(stream_id="test", payload={})
        assert (await runtime.process_packet(invalid, Context()))[0].payload["audio_analysis"]["status"] == "unavailable"

    asyncio.run(scenario())


def test_temporal_candidates_require_explicit_shared_clock_and_never_claim_same_sound():
    class Context:
        async def run_blocking(self, function, *args, **kwargs):
            return function(*args, **kwargs)

    async def scenario():
        runtime = AudioFeaturesRuntime({"baseline": {"reference": "quiet", "rms": 0.01}}, None)

        async def capture(source, capture_id, started_at, *, basis="capture", reference="calibration-1", uncertainty=0.1):
            packet = Packet.create(stream_id=source).with_artifact(Artifact(name="audio", data=tone(), metadata={
                "sample_rate": 16000, "source_id": source, "capture_id": capture_id,
                "clock": {"started_at": started_at, "basis": basis,
                          "synchronization_reference": reference, "uncertainty_seconds": uncertainty},
            }))
            return (await runtime.process_packet(packet, Context()))[0].payload["audio_analysis"]["correlation"]

        assert (await capture("a", "first", 10))["candidates"] == []
        result = await capture("b", "second", 11.1)
        assert result["status"] == "possible_overlap"
        assert result["candidates"] == [{"source_id": "a", "capture_id": "first"}]
        assert result["reason"] == "temporal_overlap_does_not_confirm_same_sound"
        assert (await capture("c", "third", 10, basis="receipt"))["status"] == "unavailable"
        assert (await capture("c", "third", 10, uncertainty=None))["status"] == "unavailable"
        assert (await capture("c", "third", 10, reference="another-clock"))["candidates"] == []
        assert (await capture("d", "fourth", 20))["candidates"] == []
        await capture("a", "first", 100)
        await capture("a", "first", 100.1)
        later = await capture("b", "second", 100.2)
        assert later["candidates"] == [{"source_id": "a", "capture_id": "first"}]
        assert later["status"] == "possible_overlap"
        assert (await capture("c", "third", 100, reference="another-clock"))["candidates"] == []
        for index in range(40):
            await capture("d", str(index), 30 + index * 2)
        assert len(runtime._recent) == 32

    asyncio.run(scenario())


def test_audio_history_survives_close_restart_and_replay_without_merging_sources(tmp_path):
    class Context:
        pipeline_name = "audio-history"
        node_id = "notify"

        async def run_blocking(self, function, *args, **kwargs):
            return function(*args, **kwargs)

    path = tmp_path / "notifications.sqlite3"
    store = NotificationStore(path)
    records = []

    async def upsert(**values):
        records.append(store.upsert(**values))

    def notifier():
        return NotifyRuntime({
            "title": "Análise de áudio sintético",
            "dedupe_by_occurrence": True,
            "update_interval_seconds": 0,
            "historical_evidence_paths": ["audio_analysis"],
        }, PipelineRuntimeDependencies(notifications_upsert=upsert))

    async def scenario():
        audio = AudioFeaturesRuntime({"baseline": {"reference": "synthetic-quiet", "rms": 0.01}}, None)
        sink = notifier()
        packet = Packet.create(stream_id="capture", lifecycle=Lifecycle.OPEN, payload={
            "source": {"source_id": "microphone-a"}, "correlation_id": "capture-1",
            "subject": {"type": "event", "id": "audio-1", "started_ts": 0},
            "media": {"ts": 0},
        }).with_artifact(Artifact(name="audio", data=tone(), metadata={
            "sample_rate": 16000, "clock": {"started_at": 0, "basis": "synthetic"},
        }))
        analyzed = (await audio.process_packet(packet, Context()))[0]
        await sink.process_packet(analyzed, Context())
        terminal = replace(packet, lifecycle=Lifecycle.CLOSE, artifacts={},
                           payload={**packet.payload, "media": {"ts": 1}})
        closed = (await audio.process_packet(terminal, Context()))[0]
        assert "audio_analysis" not in closed.payload
        await sink.process_packet(closed, Context())
        record = records[-1][0]
        assert record.payload["status"] == "closed"
        history = record.payload["data"]["historical_evidence"]["audio_analysis"]
        assert history == analyzed.payload["audio_analysis"]
        assert history["windows"][0]["anomaly"]["status"] == "above_baseline"
        assert history["clock"]["basis"] == "synthetic"
        assert history["localization"]["status"] == "unavailable"
        assert history["correlation"]["status"] == "unavailable"
        assert NotificationStore(path).get(record.id).payload == record.payload
        # A new runtime replaying the same upstream occurrence cannot reopen it.
        await notifier().process_packet(analyzed, Context())
        assert records[-1][0].id == record.id
        assert records[-1][0].payload == record.payload
        assert sum(created for _, created in records) == 1
        other = replace(analyzed, payload={**analyzed.payload, "source": {"source_id": "microphone-b"}})
        await notifier().process_packet(other, Context())
        assert records[-1][0].id != record.id
        assert sum(created for _, created in records) == 2

    asyncio.run(scenario())
