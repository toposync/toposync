"""Anexa medidas revisáveis a pacotes de áudio sem criar identidade ou posição."""

from dataclasses import replace
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.runtime import Lifecycle

from .audio import AudioBaseline, AudioClock, analyze_pcm, decode_wav_pcm, encode_wav_pcm
from .audio_transcription import transcribe_pcm


class AudioFeaturesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: AudioBaseline | None = None
    transcribe: bool = False
    language: str | None = Field(default=None, pattern="^[a-z]{2,3}$")


class AudioFeaturesRuntime(TransformOperatorRuntime):
    def __init__(self, config, deps):
        self.config = AudioFeaturesConfig.model_validate(config)
        self._recent = deque(maxlen=32)
        store = getattr(deps, "config_store", None)
        self._data_dir = store.paths.data_dir if store is not None else None

    async def process_packet(self, packet, context):
        if packet.lifecycle == Lifecycle.CLOSE:
            return [packet]
        artifact = packet.artifacts.get("audio")
        try:
            metadata = artifact.metadata if artifact else packet.payload.get("audio_capture", {})
            samples = artifact.data if artifact else None
            if artifact and metadata.get("encoding") == "wav_pcm16":
                samples, decoded_rate = decode_wav_pcm(samples)
                if decoded_rate != metadata.get("sample_rate"):
                    raise ValueError("audio sample rate mismatch")
            clock = AudioClock.model_validate(metadata.get("clock"))
            result = await context.run_blocking(
                analyze_pcm, samples, metadata.get("sample_rate"),
                clock=clock, baseline=self.config.baseline,
                availability=metadata.get("availability", "available"),
            )
            if self.config.transcribe and result["status"] in {"audible", "silent"}:
                result["transcription"] = await context.run_blocking(
                    transcribe_pcm, samples, metadata.get("sample_rate"),
                    data_dir=self._data_dir, language=self.config.language,
                )
            source_id, capture_id = metadata.get("source_id"), metadata.get("capture_id")
            correlation = {"status": "unavailable", "reason": "baseline_and_verified_capture_clock_required", "candidates": []}
            if (
                self.config.baseline is not None
                and clock.basis == "capture" and clock.uncertainty_seconds is not None
                and clock.synchronization_reference
                and isinstance(source_id, str) and 0 < len(source_id) <= 200
                and isinstance(capture_id, str) and 0 < len(capture_id) <= 200
            ):
                intervals = [
                    (window["started_at"] - clock.uncertainty_seconds,
                     window["started_at"] + window["duration_seconds"] + clock.uncertainty_seconds)
                    for window in result["windows"]
                    if window["anomaly"]["status"] == "above_baseline"
                ]
                candidates = [
                    {"source_id": source, "capture_id": capture}
                    for source, capture in dict.fromkeys(
                        (source, capture) for source, capture, reference, previous in self._recent
                        if source != source_id and reference == clock.synchronization_reference
                        and any(max(start, other_start) <= min(end, other_end)
                                for start, end in intervals for other_start, other_end in previous)
                    )
                ]
                correlation = {
                    "status": "possible_overlap" if candidates else "no_overlap_in_recent_window",
                    "reason": "temporal_overlap_does_not_confirm_same_sound", "candidates": candidates,
                    "retained_blocks_limit": 32,
                }
                block = (source_id, capture_id, clock.synchronization_reference, intervals)
                if intervals and block not in self._recent:
                    self._recent.append(block)
            result["correlation"] = correlation
        except (ValueError, TypeError, AttributeError):
            result = {"status": "unavailable", "reason": "invalid_audio_capture_contract", "windows": []}
        artifacts = dict(packet.artifacts)
        if artifact is not None:
            if result["status"] in {"audible", "silent"}:
                # Private transport accepts bounded bytes, never ndarray/pickle.
                wav = await context.run_blocking(encode_wav_pcm, samples, metadata.get("sample_rate"))
                artifacts["audio"] = replace(artifact, data=wav, mime_type="audio/wav", private=True,
                                             metadata={**metadata, "encoding": "wav_pcm16"})
            else:
                artifacts.pop("audio", None)
        return [replace(packet, payload={**packet.payload, "audio_analysis": result}, artifacts=artifacts)]


def register_audio_operator(registry) -> None:
    from .audio_source import register_audio_source
    from .audio_events import register_audio_occurrence
    register_audio_source(registry)
    register_audio_occurrence(registry)
    if registry.get("camera.audio_features") is not None:
        return
    registry.register_operator(
        operator_id="camera.audio_features",
        description="Características de PCM com relógio declarado e baseline explícito; não localiza som.",
        config_model=AudioFeaturesConfig,
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        capabilities=["audio", "private_data"], defaults=AudioFeaturesConfig().model_dump(),
        produces_payload_keys=["audio_analysis"],
        input_modalities=["audio"], output_modalities=["audio"],
        execution_mode="thread_pool", max_concurrency=1,
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: AudioFeaturesRuntime(config, deps),
    )
