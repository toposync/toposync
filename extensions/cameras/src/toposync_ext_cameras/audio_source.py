"""Audio from configured camera media; no implicit discovery or reconnection."""

import asyncio
from dataclasses import replace
import time
import uuid

from pydantic import BaseModel, ConfigDict

from toposync.runtime.pipelines.execution import SourceOperatorRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from .audio_capture import AudioCapture
from .capture_service import CameraCaptureRequest


class AudioSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    camera_id: str = ""
    source_id: str = ""


class AudioSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config, dependencies):
        self.config = AudioSourceConfig.model_validate(config)
        self.dependencies = dependencies

    async def run(self, context):
        from .pipelines.operators import get_global_camera_capture_service

        if not self.config.camera_id:
            raise ValueError("camera.audio_source requires a configured camera")
        capture_id = uuid.uuid4().hex
        resolved = await get_global_camera_capture_service().resolve(CameraCaptureRequest(
            owner_id=f"camera.audio_source:{capture_id}", camera_id=self.config.camera_id,
            source_id=self.config.source_id,
        ), self.dependencies)
        reader = AudioCapture(resolved.rtsp_url)
        packet = None
        try:
            while not context.is_cancelled():
                status, samples, offset = await context.run_blocking(reader.read)
                if context.is_cancelled():
                    break
                if status == "ended":
                    break
                duration = len(samples) / 16000 if samples is not None else 0
                metadata = {
                    "sample_rate": 16000, "source_id": resolved.source_id,
                    "capture_id": capture_id, "availability": status,
                    "clock": {"started_at": time.time() - duration, "basis": "receipt"},
                }
                packet = Packet.create(stream_id=capture_id, payload={
                    "camera_id": resolved.camera_id,
                    "camera_name": getattr(resolved, "camera_name", ""),
                    "source": {"source_id": resolved.source_id, "device_id": resolved.camera_id, "modality": "audio"},
                    "media": {"modality": "audio", "ts": offset, "duration_seconds": duration},
                    "audio_capture": metadata,
                })
                if samples is not None:
                    packet = packet.with_artifact(Artifact(name="audio", data=samples, metadata=metadata, private=True))
                await context.emit(packet, port="out")
                if status != "available":
                    messages = {
                        "no_audio": "The selected media source has no audio track. Choose an audio-capable source.",
                        "stream_lost": "Audio stream could not be read or ended. Check the source before restarting.",
                        "dependency_unavailable": "Local audio decoding is unavailable. Install the optional audio dependencies.",
                        "unsupported_channels": "Audio channel layout is unsupported. Select a mono or stereo source.",
                    }
                    raise RuntimeError(messages.get(status, "Audio source is unavailable."))
        finally:
            await asyncio.to_thread(reader.close)
            if packet is not None and not context.is_cancelled():
                await context.emit(replace(packet, lifecycle=Lifecycle.CLOSE, artifacts={}), port="out")


def register_audio_source(registry):
    if registry.get("camera.audio_source") is not None:
        return
    registry.register_operator(
        operator_id="camera.audio_source", description="PCM de fonte configurada; relógio de recepção, sem localização.",
        config_model=AudioSourceConfig, inputs=[], outputs=[{"name": "out"}],
        capabilities=["audio"], defaults={"camera_id": "", "source_id": ""},
        output_modalities=["audio"], execution_mode="thread_pool", max_concurrency=1,
        state_kind="runtime_resource", resource_kind="camera", owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: AudioSourceRuntime(config, deps),
    )
