"""Compara evidência acústica de duas entradas explícitas, sem afirmar identidade sonora."""

import asyncio
from dataclasses import replace
import time

import numpy as np
from pydantic import BaseModel, ConfigDict
from scipy.signal import spectrogram

from toposync.runtime.pipelines.runtime import Lifecycle

from .audio import AudioClock, decode_wav_pcm


class AudioCorrelationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


def audio_signature(samples, sample_rate):
    # Uma janela por pacote; taxas ainda não avaliadas permanecem indisponíveis.
    if (type(samples) is not np.ndarray or samples.dtype != np.int16
            or type(sample_rate) is not int or sample_rate not in (16000, 48000)
            or samples.shape not in ((sample_rate,), (sample_rate, 1))):
        return None
    values = samples.reshape(-1).astype(np.float64) / 32768
    if np.sqrt(np.mean(values * values)) < 1e-8:
        return None
    length, hop = int(sample_rate * .032), int(sample_rate * .01)
    frequencies, _, power = spectrogram(values, fs=sample_rate, window="hann",
                                        nperseg=length, noverlap=length - hop)
    edges = np.geomspace(300, 3400, 13)
    bands = np.stack([power[(frequencies >= low) & (frequencies < high)].sum(axis=0)
                      for low, high in zip(edges[:-1], edges[1:])])
    return 10 * np.log10(np.maximum(bands, 1e-20))


def compare_signatures(first, second):
    if first is None or second is None:
        return None
    best = None
    for lag in range(-10, 11):
        left = first[:, max(lag, 0):min(first.shape[1], second.shape[1] + lag)]
        right = second[:, max(-lag, 0):min(second.shape[1], first.shape[1] - lag)]
        if left.shape != right.shape or left.shape[1] < 50:
            continue
        keep = (left.std(axis=1) >= 1) & (right.std(axis=1) >= 1)
        if keep.sum() < 6:
            continue
        left = left[keep] - left[keep].mean(axis=1, keepdims=True)
        right = right[keep] - right[keep].mean(axis=1, keepdims=True)
        score = float(np.mean(np.sum(left * right, axis=1)
                              / (np.linalg.norm(left, axis=1) * np.linalg.norm(right, axis=1))))
        if best is None or score > best["score"]:
            best = {"score": score, "signal_lag_seconds": lag * .01}
    return best


class AudioCorrelationRuntime:
    def __init__(self, config, dependencies=None):
        AudioCorrelationConfig.model_validate(config)
        self._recent = {}
        self._bindings = {}
        self._closed = set()
        self._captures = {}
        self._last_started = {}

    async def shutdown(self):
        self._recent.clear()
        self._bindings.clear()
        self._closed.clear()
        self._captures.clear()
        self._last_started.clear()

    async def run(self, context):
        # Uma leitura pendente por porta; ordem/ciclo de vida de cada entrada preservados.
        pending = {}
        try:
            for port in context.inputs:
                pending[asyncio.create_task(context.read(port=port))] = port
            while pending and not context.is_cancelled():
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                self._expire()
                for task in done:
                    port = pending.pop(task)
                    packet = task.result()
                    if packet is not None:
                        output = await self.process_packet(packet, context, port)
                        await context.emit(output, port="out")
                    if not context.is_cancelled():
                        pending[asyncio.create_task(context.read(port=port))] = port
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.shutdown()

    def _expire(self):
        self._recent = {key: item for key, item in self._recent.items()
                        if time.monotonic() - item["received"] <= 5}

    async def process_packet(self, packet, context, port):
        now = time.monotonic()
        self._expire()
        result = {"status": "unavailable", "reason": "verified_capture_evidence_required",
                  "method": "log_power_envelope_v1", "same_sound_confirmed": False}
        try:
            if port not in {"first", "second"}:
                raise ValueError("unexpected_port")
            metadata = packet.payload.get("audio_capture", {})
            identity = (packet.payload.get("camera_id"), metadata.get("source_id"))
            capture = metadata.get("capture_id")
            if not all(isinstance(value, str) and 0 < len(value) <= 200 for value in (*identity, capture)):
                raise ValueError("invalid_source")
            if port in self._bindings and self._bindings[port] != identity:
                raise ValueError("source_changed")
            if identity in [value for key, value in self._bindings.items() if key != port]:
                raise ValueError("distinct_sources_required")
            self._bindings[port] = identity
            key = (*identity, capture)
            if packet.lifecycle == Lifecycle.CLOSE:
                if self._captures.get(port, key) != key:
                    raise ValueError("capture_changed")
                self._recent.pop(port, None)
                self._captures.pop(port, None)
                self._last_started.pop(port, None)
                if len(self._closed) < 64:
                    self._closed.add(key)
                result["reason"] = "source_closed"
            else:
                if len(self._closed) >= 64 or key in self._closed:
                    raise ValueError("closed_capture_or_session_capacity")
                if not 0 <= time.time() - packet.created_at <= 5:
                    raise ValueError("stale_packet")
                clock = AudioClock.model_validate(metadata.get("clock"))
                if (clock.basis != "capture" or not clock.synchronization_reference
                        or clock.uncertainty_seconds is None or clock.uncertainty_seconds > .05):
                    raise ValueError("verified_capture_clock_required")
                if (self._captures.get(port, key) != key
                        or clock.started_at <= self._last_started.get(port, float("-inf"))):
                    raise ValueError("capture_changed_or_out_of_order")
                artifact = packet.artifacts.get("audio")
                if (artifact is None or not artifact.private
                        or any(artifact.metadata.get(name) != metadata.get(name) for name in
                               ("source_id", "capture_id", "sample_rate", "clock", "availability"))):
                    raise ValueError("private_audio_contract_required")
                samples, rate = (decode_wav_pcm(artifact.data) if artifact.metadata.get("encoding") == "wav_pcm16"
                                 else (artifact.data, metadata.get("sample_rate")))
                if rate != metadata.get("sample_rate"):
                    raise ValueError("sample_rate_mismatch")
                signature = await context.run_blocking(audio_signature, samples, rate)
                if signature is None:
                    raise ValueError("unsupported_audio_window")
                other = self._recent.get("second" if port == "first" else "first")
                if other and (other["clock"].synchronization_reference == clock.synchronization_reference
                              and abs(other["clock"].started_at - clock.started_at) <= .1):
                    match = await context.run_blocking(compare_signatures, signature, other["signature"])
                    if match is not None:
                        result.update(match, status="similar" if match["score"] >= .7 else "different",
                                      reason="acoustic_similarity_is_not_sound_identity",
                                      candidate={"camera_id": other["key"][0], "source_id": other["key"][1],
                                                 "capture_id": other["key"][2]},
                                      threshold=.7, calibrated=False)
                    else:
                        result["reason"] = "insufficient_spectral_variation"
                self._captures[port] = key
                self._last_started[port] = clock.started_at
                self._recent[port] = {"key": key, "clock": clock, "signature": signature, "received": now}
        except (ValueError, TypeError, AttributeError) as error:
            self._recent.pop(port, None)
            result["reason"] = str(error) if type(error) is ValueError else "invalid_audio_contract"
        return replace(packet, payload={**packet.payload, "audio_correlation": result})


def register_audio_correlation(registry):
    if registry.get("camera.audio_correlation") is not None:
        return
    registry.register_operator(
        operator_id="camera.audio_correlation",
        description="Compara duas entradas privadas com relógio verificado; não confirma identidade sonora.",
        config_model=AudioCorrelationConfig,
        inputs=[{"name": "first", "required": True}, {"name": "second", "required": True}],
        outputs=[{"name": "out"}], defaults={},
        capabilities=["audio", "private_data", "origin_only"],
        input_modalities=["audio"], output_modalities=["audio"],
        produces_payload_keys=["audio_correlation"],
        execution_mode="thread_pool", max_concurrency=1, owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: AudioCorrelationRuntime(config, deps),
    )
