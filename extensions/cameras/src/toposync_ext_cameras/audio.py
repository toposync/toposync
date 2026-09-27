"""Análise local de janelas PCM; frequência não determina posição ou intenção."""

from __future__ import annotations

from io import BytesIO
from typing import Literal
import wave

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

MAX_WAV_BYTES = 12_000_000


class AudioClock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    started_at: float = Field(allow_inf_nan=False)
    basis: Literal["capture", "receipt", "synthetic"]
    uncertainty_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    synchronization_reference: str | None = Field(default=None, min_length=1, max_length=200)


class AudioBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=200)
    rms: float = Field(gt=0, le=1, allow_inf_nan=False)
    multiplier: float = Field(default=3, gt=1, le=100, allow_inf_nan=False)


def analyze_pcm(
    samples: np.ndarray | None, sample_rate: int | None, *, clock: AudioClock,
    baseline: AudioBaseline | None = None,
    availability: Literal["available", "no_audio", "stream_lost"] = "available",
) -> dict:
    result = {
        "clock": clock.model_dump(), "baseline": baseline.model_dump() if baseline else None,
        "localization": {"status": "unavailable", "reason": "microphone_geometry_and_clock_calibration_required"},
        "transcription": {"status": "unavailable", "reason": "transcription_model_not_configured"},
        "windows": [],
    }
    if availability not in {"available", "no_audio", "stream_lost"}:
        raise ValueError("invalid_audio_availability")
    if availability != "available":
        if samples is not None:
            raise ValueError("audio_samples_conflict_with_availability")
        return {**result, "status": availability}
    if samples is None:
        return {**result, "status": "audio_missing"}
    if (
        type(sample_rate) is not int or not 8000 <= sample_rate <= 96000
        or type(samples) is not np.ndarray or samples.dtype != np.int16
        or samples.ndim not in (1, 2) or (samples.ndim == 2 and samples.shape[1] not in (1, 2))
        or not 0 < len(samples) <= sample_rate * 30
    ):
        raise ValueError("expected_up_to_30_seconds_of_mono_or_stereo_pcm16")
    values = samples.astype(np.float64) / 32768
    if values.ndim == 1:
        values = values[:, None]
    windows = []
    # Cada janela é processada uma vez; canais não são somados para evitar cancelamento de fase.
    for start in range(0, len(values), sample_rate):
        segment = values[start:start + sample_rate]
        rms = float(np.sqrt(np.mean(segment ** 2)))
        peak = float(np.max(np.abs(segment)))
        clipped = float(np.mean(np.abs(segment) >= 32767 / 32768))
        silent = peak < 0.0001
        dominant = None
        if not silent and len(segment) > 1:
            centered = segment - np.mean(segment, axis=0)
            spectrum = np.fft.rfft(centered * np.hanning(len(segment))[:, None], axis=0)
            power = np.sum(np.abs(spectrum) ** 2, axis=1)
            power[0] = 0
            if np.max(power) > 0:
                dominant = float(np.fft.rfftfreq(len(segment), 1 / sample_rate)[np.argmax(power)])
        windows.append({
            "started_at": clock.started_at + start / sample_rate,
            "offset_seconds": start / sample_rate, "duration_seconds": len(segment) / sample_rate,
            "status": "silent" if silent else "audible", "rms": rms, "peak": peak,
            "clipped_fraction": clipped, "dominant_frequency_hz": dominant,
            "anomaly": {
                "status": "above_baseline" if baseline and rms > baseline.rms * baseline.multiplier
                else "within_baseline" if baseline else "unavailable",
                "reason": "rms_threshold" if baseline else "explicit_baseline_required",
                "confidence": None,
            },
        })
    return {
        **result, "status": "silent" if all(item["status"] == "silent" for item in windows) else "audible",
        "sample_rate": sample_rate, "channels": values.shape[1], "window_seconds": 1,
        "duration_seconds": len(values) / sample_rate, "windows": windows,
    }


def analyze_wav(data: bytes, *, clock: AudioClock, baseline: AudioBaseline | None = None) -> dict:
    if not 0 < len(data) <= MAX_WAV_BYTES:
        raise ValueError("wav_exceeds_budget")
    try:
        with wave.open(BytesIO(data), "rb") as stream:
            rate, channels = stream.getframerate(), stream.getnchannels()
            if (
                stream.getsampwidth() != 2 or stream.getcomptype() != "NONE"
                or channels not in (1, 2) or not 8000 <= rate <= 96000
                or not 0 < stream.getnframes() <= rate * 30
            ):
                raise ValueError("expected_up_to_30_seconds_of_mono_or_stereo_pcm16")
            count = stream.getnframes()
            pcm = stream.readframes(count)
            if len(pcm) != count * channels * 2:
                raise ValueError("truncated_wav")
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid_wav") from exc
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int16).reshape(-1, channels)
    return analyze_pcm(samples, rate, clock=clock, baseline=baseline)
