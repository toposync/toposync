"""Ensaio explícito com arquivo WAV; não acessa câmeras nem caminhos do servidor."""

import asyncio
import logging
import sqlite3
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from .audio import AudioBaseline, AudioClock, MAX_WAV_BYTES, analyze_pcm, decode_wav_pcm
from .audio_transcription import transcribe_pcm


def install_audio_clip_routes(app: FastAPI) -> None:
    from toposync.extensions import register_extension_shutdown_callback
    from toposync.runtime.auth import AuthContext, AuthRuntime
    from .audio_clips import AudioClipStore

    store = AudioClipStore(app.state.config_store.paths.data_dir)

    def authorize(request, camera_id, *, delete=False):
        auth = getattr(request.app.state, "auth", None)
        context = getattr(request.state, "auth_context", None)
        if not isinstance(auth, AuthRuntime) or not isinstance(context, AuthContext):
            raise HTTPException(401, "Authentication required")
        auth.authorize(context=context, action="core:extension:use", resource_type="core:extension",
                       resource_selector="com.toposync.cameras")
        auth.authorize(context=context, action="core:camera:configure" if delete else "core:camera:read",
                       resource_type="core:camera", resource_selector=camera_id)

    @app.get("/api/cameras/audio/clips/{clip_id}")
    async def read_audio_clip(request: Request, clip_id: str):
        result = await asyncio.to_thread(store.read, clip_id, authorize=lambda camera: authorize(request, camera))
        if result is None:
            raise HTTPException(404, "Audio excerpt unavailable or expired")
        return Response(result[1], media_type="audio/wav", headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        })

    @app.delete("/api/cameras/audio/clips/{clip_id}", status_code=204)
    async def delete_audio_clip(request: Request, clip_id: str):
        await asyncio.to_thread(store.delete, clip_id, authorize=lambda camera: authorize(request, camera, delete=True))
        return Response(status_code=204)

    async def cleanup():
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(store.cleanup)
            except (OSError, sqlite3.Error):
                logging.getLogger(__name__).error("Private audio cleanup failed; expired excerpts remain inaccessible")

    task = asyncio.create_task(cleanup())

    async def shutdown():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    register_extension_shutdown_callback(app, shutdown)


def install_audio_routes(app: FastAPI) -> None:
    @app.post("/api/cameras/audio/analyze")
    async def analyze_audio_file(
        request: Request,
        started_at: float = Query(..., allow_inf_nan=False),
        clock_basis: Literal["capture", "receipt", "synthetic"] = Query(...),
        clock_uncertainty_seconds: float | None = Query(default=None, ge=0, allow_inf_nan=False),
        baseline_rms: float | None = Query(default=None, gt=0, le=1, allow_inf_nan=False),
        baseline_reference: str | None = Query(default=None, min_length=1, max_length=200),
        transcribe: bool = False,
        language: str | None = Query(default=None, pattern="^[a-z]{2,3}$"),
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
            samples, rate = await asyncio.to_thread(decode_wav_pcm, bytes(data))
            result = await asyncio.to_thread(analyze_pcm, samples, rate, clock=clock, baseline=baseline)
            if transcribe:
                store = getattr(request.app.state, "config_store", None)
                directory = store.paths.data_dir if store is not None else None
                result["transcription"] = await asyncio.to_thread(
                    transcribe_pcm, samples, rate, data_dir=directory, language=language,
                )
            return result
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
