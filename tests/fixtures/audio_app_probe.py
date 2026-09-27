"""Actual app with a synthetic audio source; no hardware or model access."""
from contextlib import asynccontextmanager
import sys

import uvicorn

from audio_processing_probe import Config, Source
from toposync.app import create_app
import toposync.extensions.manager as manager
from toposync.runtime.config_store import Pipeline
from toposync_ext_cameras.plugin import CamerasExtension
from toposync_ext_cameras.audio_events import build_audio_recipe


class EntryPoint:
    name = "cameras"
    value = "toposync_ext_cameras.plugin:CamerasExtension"

    def load(self):
        return CamerasExtension


manager._iter_entry_points = lambda _: [EntryPoint()]
app = create_app()
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        registry = application.state.pipeline_operator_registry
        registry.register_operator(
            operator_id="test.audio_probe", config_model=Config,
            inputs=[], outputs=[{"name": "out"}], capabilities=["source"],
            defaults={}, share_strategy="never", owner="test",
            runtime_factory=lambda config, dependencies: Source(config),
        )
        store = application.state.config_store
        if not await store.list_pipelines():
            graph = build_audio_recipe(
                graph_uid="restart_audio", camera_id="synthetic-camera", source_id="synthetic",
                baseline={"reference": "synthetic quiet", "rms": 0.01}, retain_audio=True,
            )
            graph["nodes"][0].update(operator="test.audio_probe", config={"hold_open": True})
            await store.create_pipeline(Pipeline(name="restart_audio", graph=graph))
        application.state.pipelines_orchestrator.trigger_reload()
        yield


app.router.lifespan_context = lifespan
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning", access_log=False,
                timeout_graceful_shutdown=2)
