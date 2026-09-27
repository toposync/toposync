"""Optional local transcription; one bounded CPU inference, no implicit downloads."""

from __future__ import annotations

import math
from pathlib import Path
from threading import Lock

import numpy as np
from scipy.signal import resample_poly

from .audio import AudioClock, analyze_pcm


_inference_lock = Lock()
MODEL_DIRECTORY = "audio-models/faster-whisper-tiny"


def transcribe_pcm(samples: np.ndarray, sample_rate: int, *, data_dir: Path | None, language: str | None = None) -> dict:
    # Reuse the input contract, including the 30-second bound and PCM16 channels.
    analysis = analyze_pcm(samples, sample_rate, clock=AudioClock(started_at=0, basis="synthetic"))
    if language is not None and (not isinstance(language, str) or not language.isascii() or not language.isalpha() or not 2 <= len(language) <= 3):
        raise ValueError("invalid_transcription_language")
    if analysis["status"] not in {"audible", "silent"}:
        raise ValueError("transcription_requires_audio_samples")
    if analysis["status"] == "silent":
        return {"status": "no_speech", "reason": "silent_audio", "segments": [], "text": ""}
    model_dir = data_dir / MODEL_DIRECTORY if data_dir is not None else None
    if model_dir is None or not all((model_dir / name).is_file() for name in (
        "model.bin", "config.json", "tokenizer.json", "vocabulary.txt",
    )):
        return {"status": "unavailable", "reason": "transcription_model_not_installed", "segments": []}
    if not _inference_lock.acquire(blocking=False):
        return {"status": "busy", "reason": "local_transcription_in_progress", "segments": []}
    model = segments = None
    try:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return {"status": "unavailable", "reason": "transcription_dependency_not_installed", "segments": []}
        values = samples.astype(np.float32) / 32768
        channel = 0
        if values.ndim == 2:
            # Select a real channel; averaging can cancel opposite-phase stereo.
            channel = int(np.argmax(np.mean(values ** 2, axis=0)))
            values = values[:, channel]
        if sample_rate != 16000:
            divisor = math.gcd(sample_rate, 16000)
            values = resample_poly(values, 16000 // divisor, sample_rate // divisor).astype(np.float32)
        model = WhisperModel(str(model_dir), device="cpu", compute_type="int8", cpu_threads=2,
                             num_workers=1, local_files_only=True, use_auth_token=False)
        segments, info = model.transcribe(
            values, language=language.lower() if language else None, beam_size=3, best_of=1,
            temperature=0, condition_on_previous_text=False, vad_filter=True, max_new_tokens=128,
        )
        rows = []
        token_count = 0
        text_length = 0
        for segment in segments:
            start, end = float(segment.start), float(segment.end)
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= len(values) / 16000 + 0.1):
                raise ValueError("invalid_transcription_interval")
            text = " ".join(segment.text.split())
            text_length += len(text) + 1
            if text_length > 4096 or len(rows) >= 64:
                raise ValueError("transcription_output_exceeds_budget")
            token_count += len(segment.tokens)
            rows.append({"offset_seconds": start, "duration_seconds": end - start, "text": text})
        return {
            "status": "partial" if token_count >= 128 else "transcribed" if rows else "no_speech",
            "segments": rows, "text": " ".join(row["text"] for row in rows),
            "language": info.language, "model": "faster-whisper-tiny", "selected_channel": channel,
            "automatic_text_requires_review": True,
        }
    except Exception:
        return {"status": "unavailable", "reason": "local_transcription_failed", "segments": []}
    finally:
        # No resident model or background inference survives this call.
        segments = model = None
        _inference_lock.release()
