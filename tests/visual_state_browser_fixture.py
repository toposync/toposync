"""Dedicated visual-sensor browser fixture. Generated images; no hardware or outbound connections."""
import argparse
import os
from pathlib import Path

import uvicorn


def make_app(directory: Path):
    directory = directory.resolve()
    repository = Path(__file__).resolve().parents[1]
    if directory.is_relative_to(repository):
        raise RuntimeError("Browser fixture data must be outside the repository")
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("Browser fixture requires a new empty data directory")
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["TOPOSYNC_DATA_DIR"] = str(directory)
    os.environ["TOPOSYNC_AUTH_MODE"] = "bypass"
    import asyncio
    from dataclasses import replace
    import time
    import uuid

    import httpx
    import numpy as np

    import toposync.extensions.manager as manager
    from toposync.app import create_app
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines.execution import BaseOperatorRuntime
    from toposync.runtime.pipelines.image_geometry import image_geometry
    from toposync.runtime.pipelines.runtime import Artifact, Packet
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.plugin import CamerasExtension
    from toposync_ext_vision.plugin import VisionExtension
    from toposync_ext_ai.plugin import AiExtension
    from toposync_ext_home_assistant.plugin import HomeAssistantExtension
    from toposync_ext_ai.visual_state import VisualStateResponse
    import toposync_ext_ai.router as ai_module

    stats = {"generated_frames": 0, "simulated_inferences": 0, "outbound_attempts": 0, "synthetic": True}

    async def forbid_http(*args, **kwargs):
        stats["outbound_attempts"] += 1
        raise RuntimeError("external HTTP is disabled in this fixture")

    httpx.AsyncClient.send = forbid_http

    def forbid_sync_http(*args, **kwargs):
        stats["outbound_attempts"] += 1
        raise RuntimeError("outbound HTTP is disabled in this fixture")

    httpx.Client.send = forbid_sync_http

    import socket

    def forbid_connect(*args, **kwargs):
        stats["outbound_attempts"] += 1
        raise RuntimeError("outbound sockets are disabled in this fixture")

    socket.socket.connect = forbid_connect
    socket.socket.connect_ex = forbid_connect

    class Provider:
        async def classify_prepared_visual_state(self, *, request, encoded):
            stats["simulated_inferences"] += 1
            await asyncio.sleep(.015)
            return VisualStateResponse(observable=True, class_id=request.classes[0].class_id,
                reason="Synthetic fixture response", declared_confidence=None, reference_ids=[])

    ai_module.build_provider = lambda *_: Provider()

    async def inventory(self, host=None):
        return [{"name": "synthetic-visual:fixture"}]

    ai_module.AiRouter.list_ollama_models = inventory

    class Source(BaseOperatorRuntime):
        async def run(self, context):
            instance = uuid.uuid4().hex
            sequence = 0
            while not context.is_cancelled():
                sequence += 1
                stats["generated_frames"] += 1
                evidence = {"capture_instance": instance, "generation": 0, "sequence": sequence,
                            "received_monotonic": time.monotonic(), "published_at": time.time()}
                pixels = np.zeros((128, 256, 3), dtype=np.uint8)
                pixels[:, :, 2] = 220
                pixels[20:110, 80:180, 0] = 100
                await context.emit(Packet.create(stream_id="camera:synthetic:video", payload={"capture_evidence": evidence},
                    artifacts={"main": Artifact(name="main", data=pixels,
                        metadata={"image_geometry": image_geometry(256, 128, evidence)})}))
                await context.sleep(.1)

    class FixtureCameras(CamerasExtension):
        async def setup(self, app, **kwargs):
            await super().setup(app, **kwargs)
            registry = app.state.pipeline_operator_registry
            registry._items["camera.source"] = replace(registry.get("camera.source"), runtime_factory=lambda *_: Source())

    class FixtureAi(AiExtension):
        async def setup(self, app, **kwargs):
            await super().setup(app, **kwargs)
            store = app.state.config_store
            if await store.get_pipeline("visual_fixture") is None:
                await kwargs["services"].call("ai.settings.patch", patch={
                    "default_profile_id": "visual_fixture", "providers": [
                        {"id": "fixture_local", "name": "Provedor simulado", "kind": "ollama", "host": "http://127.0.0.1:1"},
                        {"id": "fixture_remote", "name": "Destino simulado externo", "kind": "openai", "allow_image_upload": True}],
                    "profiles": [
                        {"id": "visual_fixture", "name": "Visual local simulado", "provider_id": "fixture_local", "model": "synthetic-visual:fixture",
                         "capabilities": ["vision", "structured_json", "multiple_images"], "max_images_per_request": 65, "budget_group_id": "fixture_account"},
                        {"id": "visual_remote", "name": "Visual externo simulado", "provider_id": "fixture_remote", "model": "synthetic-remote",
                         "capabilities": ["vision", "structured_json", "multiple_images"], "max_images_per_request": 65},
                        {"id": "incompatible", "name": "Perfil incompatível", "provider_id": "fixture_local", "model": "text-only", "capabilities": []}],
                    "limits": {"requests_per_minute": 1000, "requests_per_hour": 10000, "requests_per_day": 100000},
                    "budget_groups": [{"id": "fixture_account", "name": "Conta do laboratório", "limits": {"requests_per_minute": 1000}}],
                })
                graph = build_pipeline_graph_v2(graph_uid="visual_fixture", nodes=[
                    {"id": "source", "operator": "camera.source", "config": {"camera_id": "synthetic", "source_id": "video", "fps": 10}},
                    {"id": "sensor", "operator": "vision.visual_state", "config": {
                        "sensor_id": "lab_sensor", "strategy": "direct", "ai_profile_id": "visual_fixture", "source_identity": "camera:synthetic:video", "preparation_revision": "synthetic-ui-preparation", "reference_revision": "draft", "model_revision": "unconfigured", "use_full_image": True,
                        "classes": [{"class_id": key, "key": key, "label": label, "description": description}
                            for key, label, description in [("open", "Aberta", "Abertura visível"), ("closed", "Fechada", "Folha cobre a abertura")]]}},
                ], edges=[{"from": {"node": "source", "port": "out"}, "to": {"node": "sensor", "port": "in"}, "traffic": {"modality": "video"}}])
                await store.create_pipeline(Pipeline(name="visual_fixture", display_name="Laboratório visual isolado", enabled=False, editor_mode="json", graph=graph))

    class FixtureVision(VisionExtension):
        async def setup(self, app, **kwargs):
            await super().setup(app, **kwargs)
            from toposync_ext_vision.visual_state.schemas import ReferenceEvidence, SuggestionProvenance, VisualStateConfig
            from fastapi import Request
            import io
            from PIL import Image

            @app.post("/fixture/seed-suggestions")
            async def seed_suggestions(request: Request):
                body = await request.json()
                pipeline = await app.state.config_store.get_pipeline("visual_fixture")
                configuration = VisualStateConfig.model_validate(next(node for node in pipeline.graph["nodes"] if node["id"] == "sensor")["config"])
                store = await kwargs["services"].call("vision.visual_references")
                results = []
                for index, color in enumerate(body.get("colors", ["red", "blue", "green"])):
                    evidence = ReferenceEvidence(source_identity=configuration.source_identity, preparation_revision=configuration.preparation_revision,
                        sample={"capture_instance": "synthetic-ui", "generation": 0, "sequence": index + 1},
                        observed_at=time.time(), freshness_basis="receipt", prepared_region=False)
                    provenance = SuggestionProvenance(runtime_epoch="fixture-original-epoch", proposed_class_id="open",
                        configuration_digest=store.configuration_digest(configuration), reference_revision=configuration.reference_revision,
                        model_revision=configuration.model_revision, ai_configuration_revision="a" * 64,
                        profile_id="visual_fixture", provider_id="fixture_local", model="synthetic-visual:fixture",
                        reason="Imagem sintética para validar revisão, não uma observação física", declared_confidence=.99)
                    output = io.BytesIO()
                    Image.new("RGB", (128, 96), color).save(output, format="PNG")
                    suggestion = store.offer_suggestion(configuration, "open", output.getvalue(), evidence, provenance)
                    results.append(suggestion.suggestion_id if suggestion else None)
                return {"suggestions": results}

    class Entry:
        def __init__(self, name, implementation):
            self.name, self.implementation = name, implementation
            self.value = f"fixture:{name}"
        def load(self):
            return self.implementation

    manager._iter_entry_points = lambda _: [Entry("cameras", FixtureCameras), Entry("vision", FixtureVision), Entry("ai", FixtureAi), Entry("home_assistant", HomeAssistantExtension)]
    app = create_app()

    @app.get("/fixture/stats")
    def fixture_stats():
        return stats

    class IngressFixture:
        async def __call__(self, scope, receive, send):
            prefix = "/fixture-ingress"
            if scope.get("type") in {"http", "websocket"} and scope.get("path", "").startswith(prefix + "/"):
                scope = dict(scope)
                scope["path"] = scope["path"][len(prefix):]
                scope["raw_path"] = scope["path"].encode()
                scope["headers"] = [*scope.get("headers", []), (b"x-ingress-path", prefix.encode())]
            await app(scope, receive, send)


    return IngressFixture()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    uvicorn.run(make_app(arguments.data_dir), host="127.0.0.1", port=arguments.port, log_level="warning")
