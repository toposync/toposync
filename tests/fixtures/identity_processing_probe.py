"""Isolated processing process for the private transport contract test.

Synthetic vectors exercise transport and lifecycle only, never model accuracy.
"""

from contextlib import asynccontextmanager
import json
import os
import sys
import time

from pathlib import Path
from pydantic import BaseModel, Field
from starlette.requests import Request
import uvicorn

from toposync.processing_server import create_app
from toposync.runtime.pipelines import SourceOperatorRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.identity.contracts import IdentityEvidence
from toposync_ext_vision.identity.pipelines import occurrence_key, PRIVATE_EVIDENCE_ARTIFACT


class ProbeConfig(BaseModel):
    enabled: bool = True
    visits: int = Field(default=1, ge=1, le=20)


class ProbeSource(SourceOperatorRuntime):
    def __init__(self, config):
        self.sequence = 0
        self.visits = ProbeConfig.model_validate(config).visits

    async def produce(self, context):
        if self.sequence >= self.visits * 3:
            await context.sleep(0.05)
            return None
        index = self.sequence
        self.sequence += 1
        phase = index % 3
        visit = index // 3
        value = Packet.create(
            stream_id="camera:isolated-probe",
            lifecycle=(Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE)[phase],
            payload={
                "camera_id": "isolated-camera",
                "frame_ts": time.time(),
                "correlation_id": f"independent-process-session-{visit}",
                "subject": {"type": "event", "id": "tracked-pet", "category": "cat"},
                "world_position": {"x": 1.5, "z": 2.5},
                "processing_pid": os.getpid(),
                "probe_sequence": index,
            },
        )
        if phase == 2:
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
            runtime_factory=lambda config, _dependencies: ProbeSource(config),
        )
        yield


app.router.lifespan_context = lifespan

# Injeção somente no processo de teste: encerrar um SSE preservando a instância.
trace_path = os.environ.get("TOPOSYNC_TEST_STREAM_TRACE")
if trace_path:
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/processing/events/stream")
    original_stream = route.dependant.call
    stream_attempts = 0

    def trace(values):
        with Path(trace_path).open("a") as output:
            output.write(json.dumps(values) + "\n")

    async def interrupted_stream(request: Request):
        global stream_attempts
        stream_attempts += 1
        attempt = stream_attempts
        trace({"attempt": attempt, "cursor": request.headers.get("Last-Event-ID", "0")})
        response = await original_stream(request)
        if attempt == 1:
            iterator = response.body_iterator

            async def finite_stream():
                packets = 0
                try:
                    async for chunk in iterator:
                        yield chunk
                        payload = chunk.decode() if isinstance(chunk, bytes) else chunk
                        if payload.startswith("data: ") and json.loads(payload[6:]).get("event_type") == "packet.projected":
                            packets += 1
                            if packets == 9:
                                trace({"interrupted_after_packets": packets})
                                return
                finally:
                    await iterator.aclose()

            response.body_iterator = finite_stream()
        return response

    route.dependant.call = interrupted_stream
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning", access_log=False)
