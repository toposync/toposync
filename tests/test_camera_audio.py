import asyncio
from io import BytesIO
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pytest

from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.audio import AudioBaseline, AudioClock, analyze_pcm, analyze_wav
from toposync_ext_cameras.audio_api import install_audio_routes
from toposync_ext_cameras.audio_pipeline import AudioFeaturesRuntime, register_audio_operator


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
        for index in range(40):
            await capture("d", str(index), 30 + index * 2)
        assert len(runtime._recent) == 32

    asyncio.run(scenario())
