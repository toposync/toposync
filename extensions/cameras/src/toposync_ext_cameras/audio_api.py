"""Ensaio explícito com arquivo WAV; não acessa câmeras nem caminhos do servidor."""

import asyncio
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request

from .audio import AudioBaseline, AudioClock, MAX_WAV_BYTES, analyze_wav


def install_audio_routes(app: FastAPI) -> None:
    @app.post("/api/cameras/audio/analyze")
    async def analyze_audio_file(
        request: Request,
        started_at: float = Query(..., allow_inf_nan=False),
        clock_basis: Literal["capture", "receipt", "synthetic"] = Query(...),
        clock_uncertainty_seconds: float | None = Query(default=None, ge=0, allow_inf_nan=False),
        baseline_rms: float | None = Query(default=None, gt=0, le=1, allow_inf_nan=False),
        baseline_reference: str | None = Query(default=None, min_length=1, max_length=200),
    ):
        if (baseline_rms is None) != (baseline_reference is None):
            raise HTTPException(422, "Informe valor e referência do baseline juntos.")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_WAV_BYTES:
                raise HTTPException(413, "Arquivo excede o limite de áudio.")
            data.extend(chunk)
        clock = AudioClock(started_at=started_at, basis=clock_basis, uncertainty_seconds=clock_uncertainty_seconds)
        baseline = AudioBaseline(rms=baseline_rms, reference=baseline_reference) if baseline_rms is not None else None
        try:
            return await asyncio.to_thread(analyze_wav, bytes(data), clock=clock, baseline=baseline)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
