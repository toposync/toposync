"""Finite synthetic PCM source in a separate processing process; no device I/O."""
from contextlib import asynccontextmanager
import os
import sys
import time

import numpy as np
from pydantic import BaseModel
import uvicorn

from toposync.processing_server import create_app
from toposync.runtime.pipelines import SourceOperatorRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.audio_pipeline import register_audio_operator


class Config(BaseModel):
    pass


class Source(SourceOperatorRuntime):
    def __init__(self):
        self.sequence = 0

    async def produce(self, context):
        index = self.sequence
        if index >= 3:
            await context.sleep(0.05)
            return None
        self.sequence += 1
        metadata = {"sample_rate": 16000, "capture_id": "audio-http-probe", "source_id": "synthetic",
                    "clock": {"started_at": time.time(), "basis": "synthetic"}}
        packet = Packet.create(stream_id="audio-http-probe", lifecycle=Lifecycle.CLOSE if index == 2 else Lifecycle.UPDATE,
                               payload={"camera_id": "synthetic-camera", "source": {"source_id": "synthetic"},
                                        "audio_capture": metadata, "media": {"ts": index}, "processing_pid": os.getpid()})
        if index == 2:
            return packet
        samples = (8000 * np.sin(2 * np.pi * 1000 * np.arange(16000) / 16000)).astype(np.int16)
        return packet.with_artifact(Artifact(name="audio", data=samples, metadata=metadata, private=True))


app = create_app()
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        registry = application.state.pipeline_operator_registry
        register_audio_operator(registry)
        registry.register_operator(operator_id="test.audio_probe", config_model=Config,
                                   inputs=[], outputs=[{"name": "out"}], capabilities=["source"],
                                   defaults={}, share_strategy="never", owner="test",
                                   runtime_factory=lambda config, dependencies: Source())
        yield


app.router.lifespan_context = lifespan
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning", access_log=False)
