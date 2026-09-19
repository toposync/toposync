"""Isolated processing process for the private transport contract test.

Synthetic vectors exercise transport and lifecycle only, never model accuracy.
"""

from contextlib import asynccontextmanager
import json
import os
import sys
import time

from pydantic import BaseModel
import uvicorn

from toposync.processing_server import create_app
from toposync.runtime.pipelines import SourceOperatorRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.identity.contracts import IdentityEvidence
from toposync_ext_vision.identity.pipelines import occurrence_key, PRIVATE_EVIDENCE_ARTIFACT


class ProbeConfig(BaseModel):
    enabled: bool = True


class ProbeSource(SourceOperatorRuntime):
    def __init__(self):
        self.sequence = 0

    async def produce(self, context):
        if self.sequence >= 3:
            await context.sleep(0.05)
            return None
        index = self.sequence
        self.sequence += 1
        value = Packet.create(
            stream_id="camera:isolated-probe",
            lifecycle=(Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE)[index],
            payload={
                "camera_id": "isolated-camera",
                "frame_ts": time.time(),
                "correlation_id": "independent-process-session",
                "subject": {"type": "event", "id": "tracked-pet", "category": "cat"},
                "world_position": {"x": 1.5, "z": 2.5},
                "processing_pid": os.getpid(),
            },
        )
        if index == 2:
            return value
        evidence = IdentityEvidence(
            species="cat",
            occurrence_id=occurrence_key(value),
            source_id=value.stream_id,
            camera_id="isolated-camera",
            capture_id=value.packet_id,
            observed_at=value.payload["frame_ts"],
            embedding_space="synthetic:transport-contract",
            vector=(1.0,) + (0.0,) * 15,
            quality=0.9,
            reference_eligible=True,
            region=(0, 0, 1, 1),
        )
        return value.with_artifact(
            Artifact(
                name=PRIVATE_EVIDENCE_ARTIFACT,
                private=True,
                data=json.dumps({"evidence": evidence.model_dump(), "crop": None}).encode(),
            )
        )


app = create_app()
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        application.state.pipeline_operator_registry.register_operator(
            operator_id="test.private_probe",
            config_model=ProbeConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            capabilities=["source", "private_data"],
            defaults=ProbeConfig().model_dump(),
            share_strategy="never",
            owner="test",
            runtime_factory=lambda _config, _dependencies: ProbeSource(),
        )
        yield


app.router.lifespan_context = lifespan
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning", access_log=False)
