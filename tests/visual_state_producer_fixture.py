"""Real Toposync HTTP/runtime with generated pixels, for an isolated Docker consumer."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Literal
import uuid


def make_app(directory, *, resume=False, paused=False):
    if os.environ.get("TOPOSYNC_FULL_PRODUCER_TEST") != "1" or not Path("/.dockerenv").is_file():
        raise RuntimeError("This producer is restricted to the explicitly isolated Docker test")
    directory = directory.resolve()
    marker = directory / "synthetic-fixture.json"
    if directory.is_relative_to(Path(__file__).resolve().parents[1]):
        raise RuntimeError("Use a new external test data directory")
    if resume:
        if not marker.is_file() or json.loads(marker.read_text()) != {"fixture": "visual_state_producer", "version": 1}:
            raise RuntimeError("Only this synthetic fixture's persisted data can be resumed")
    elif directory.exists() and any(directory.iterdir()):
        raise RuntimeError("Use a new external test data directory")
    directory.mkdir(parents=True, exist_ok=True)
    os.environ.update(TOPOSYNC_DATA_DIR=str(directory), TOPOSYNC_AUTH_MODE="enforced",
                      TOPOSYNC_NO_FRONTEND="1", TOPOSYNC_EXTENSION_AUTO_INSTALL_ON_STARTUP="0")

    import numpy as np
    from fastapi import Request
    import toposync.extensions.manager as manager
    from toposync.app import create_app
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines.execution import BaseOperatorRuntime
    from toposync.runtime.pipelines.image_geometry import image_geometry
    from toposync.runtime.pipelines.preview import reference_input_pipeline, reference_preparation_revision
    from toposync.runtime.pipelines.runtime import Artifact, Packet
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.plugin import CamerasExtension
    from toposync_ext_streaming.plugin import StreamingExtension
    from toposync_ext_vision.plugin import VisionExtension
    from toposync_ext_vision.processing.runtime_backends.embedding import embedding_model_revision
    from toposync_ext_vision.registry.manifests import ModelManifest
    from toposync_ext_vision.visual_state.schemas import ReferenceEvidence, VisualStateConfig
    from test_visual_state_runtime import color_model
    from test_visual_state_store import image_bytes

    manifest_directory = directory / "vision-manifests"
    if resume:
        manifest = ModelManifest.model_validate_json((manifest_directory / "synthetic.json").read_text())
    else:
        manifest = color_model(directory)
        manifest_directory.mkdir()
        (manifest_directory / "synthetic.json").write_text(manifest.model_dump_json())
    state = {"color": "blue", "paused": paused, "frames": 0, "outbound_attempts": 0}
    runtimes = []

    def forbid_connect(*args, **kwargs):
        state["outbound_attempts"] += 1
        raise RuntimeError("Outbound connections are forbidden in this producer")
    socket.socket.connect = forbid_connect
    socket.socket.connect_ex = forbid_connect

    class Source(BaseOperatorRuntime):
        async def run(self, context):
            instance, sequence = uuid.uuid4().hex, 0
            while not context.is_cancelled():
                if not state["paused"]:
                    sequence += 1
                    state["frames"] += 1
                    evidence = {"capture_instance": instance, "generation": 0, "sequence": sequence,
                                "received_monotonic": time.monotonic(), "published_at": time.time()}
                    pixels = np.zeros((10, 12, 3), dtype=np.uint8)
                    pixels[:, :, {"blue": 0, "green": 1, "red": 2}[state["color"]]] = 255
                    await context.emit(Packet.create(stream_id="camera:synthetic:video",
                        payload={"capture_evidence": evidence}, artifacts={"main": Artifact(name="main", data=pixels,
                            metadata={"image_geometry": image_geometry(12, 10, evidence)})}))
                await context.sleep(.1)

    class FixtureCameras(CamerasExtension):
        async def setup(self, app, **kwargs):
            await super().setup(app, **kwargs)
            registry = app.state.pipeline_operator_registry
            registry._items["camera.source"] = replace(registry.get("camera.source"),
                runtime_factory=lambda *_: Source(), diagnostics_factory=lambda *_: [])

    class FixtureVision(VisionExtension):
        async def setup(self, app, **kwargs):
            await super().setup(app, **kwargs)
            registry = app.state.pipeline_operator_registry
            original = registry.get("vision.visual_state")
            def track(configuration, dependencies):
                runtime = original.runtime_factory(configuration, dependencies)
                runtimes.append(runtime)
                return runtime
            registry._items["vision.visual_state"] = replace(original, runtime_factory=track)
            if resume:
                return
            references = await kwargs["services"].call("vision.visual_references")
            nodes = [{"id": "source", "operator": "camera.source",
                      "config": {"camera_id": "synthetic", "source_id": "video", "fps": 10}}]
            configurations = []
            for identifier, kind in (("door", "enum"), ("gate", "binary")):
                configuration = VisualStateConfig(sensor_id=identifier, source_identity="camera:synthetic:video",
                    use_full_image=True, preparation_revision="pending", reference_revision="pending",
                    model_id=manifest.model_id, model_revision=embedding_model_revision(manifest),
                    classes=[{"class_id": color, "key": color, "label": color} for color in ("red", "blue", "green")],
                    comparison={"enter_threshold": .9, "enter_margin": .2, "hold_threshold": .8, "hold_margin": .1},
                    home_assistant={"enabled": True, "kind": kind, "name": identifier,
                                    "on_class_ids": ["red"] if kind == "binary" else [],
                                    "off_class_ids": ["blue"] if kind == "binary" else []})
                configurations.append(configuration)
                nodes.append({"id": identifier, "operator": "vision.visual_state", "config": configuration.model_dump(mode="json")})
            pipeline = Pipeline(name="full_visual_fixture", enabled=True, graph=build_pipeline_graph_v2(graph_uid="full-visual",
                nodes=nodes, edges=[{"from": {"node": "source", "port": "out"}, "to": {"node": key, "port": "in"},
                                    "traffic": {"modality": "video"}} for key in ("door", "gate")]))
            for configuration in configurations:
                revision = reference_preparation_revision(reference_input_pipeline(pipeline, configuration.sensor_id))
                draft = references.create_draft(configuration.model_copy(update={"preparation_revision": revision}))
                for color in ("red", "blue", "green"):
                    draft, _ = references.capture(draft.draft_id, color, image_bytes(color), ReferenceEvidence(
                        source_identity=configuration.source_identity, preparation_revision=revision,
                        sample={"capture_instance": "synthetic-reference", "generation": 0, "sequence": 1},
                        observed_at=time.time(), freshness_basis="receipt"),
                        operation_id=f"{configuration.sensor_id}-{color}", expected_revision=draft.revision)
                next(node for node in pipeline.graph["nodes"] if node["id"] == configuration.sensor_id)["config"] = draft.configuration.model_dump(mode="json")
            await app.state.config_store.create_pipeline(pipeline)
            marker.write_text(json.dumps({"fixture": "visual_state_producer", "version": 1}))

    class Entry:
        def __init__(self, name, implementation):
            self.name, self.implementation = name, implementation
            self.value = f"fixture:{name}"
        def load(self):
            return self.implementation
    manager._iter_entry_points = lambda _: [Entry("cameras", FixtureCameras), Entry("vision", FixtureVision), Entry("streaming", StreamingExtension)]
    app = create_app()

    def owner(request):
        auth = request.app.state.auth
        auth.authorize(context=request.state.auth_context, action="core:access:manage")

    @app.post("/fixture/control")
    async def control(request: Request, color: Literal["red", "blue", "green"], paused: bool = False):
        owner(request)
        state.update(color=color, paused=paused)
        return {"color": color, "paused": paused}

    @app.get("/fixture/stats")
    async def stats(request: Request):
        owner(request)
        return {**state, "pid": os.getpid(), "synthetic_pixels": True, "synthetic_model": True,
                "model_sha256": manifest.sha256,
                "runtime_providers": [list(runtime._backend.providers) for runtime in runtimes if runtime._backend is not None]}
    return app


def supervise(directory, control):
    """Kill/restart only the owned producer; file commands stay on the isolated test mount."""
    if os.environ.get("TOPOSYNC_FULL_PRODUCER_TEST") != "1" or not Path("/.dockerenv").is_file():
        raise RuntimeError("Explicit isolated Docker test only")
    control = control.resolve()
    if control.is_relative_to(Path(__file__).resolve().parents[1]):
        raise RuntimeError("Use an external control directory")
    control.mkdir(parents=True, exist_ok=False)
    process, sequence = None, 0
    deadline = time.monotonic() + 180
    (control / "ready").write_text(str(os.getpid()))
    try:
        while time.monotonic() < deadline:
            request = control / f"request-{sequence + 1}.json"
            if not request.is_file():
                time.sleep(.02)
                continue
            command = json.loads(request.read_text())
            action = command["action"]
            assert action in {"start", "kill", "stop", "withdraw_model", "restore_model"}
            if action in {"withdraw_model", "restore_model"}:
                assert process is None
                assert json.loads((directory / "synthetic-fixture.json").read_text()) == {
                    "fixture": "visual_state_producer", "version": 1}
                artifact, held = directory / "embedding.onnx", directory / "embedding.onnx.withdrawn"
                source, target = (artifact, held) if action == "withdraw_model" else (held, artifact)
                assert source.is_file() and not target.exists()
                source.rename(target)
                result = {"model_present": artifact.is_file()}
            elif action == "start":
                assert process is None
                arguments = [sys.executable, str(Path(__file__).resolve()), "--data-dir", str(directory)]
                if command.get("resume"):
                    arguments += ["--resume", "--paused"]
                process = subprocess.Popen(arguments)
                result = {"pid": process.pid, "started": True}
            else:
                assert process is not None
                process.kill() if action == "kill" else process.terminate()
                code = process.wait(timeout=10)
                result = {"pid": process.pid, "exitcode": code}
                process = None
            sequence += 1
            temporary = control / "response.tmp"
            temporary.write_text(json.dumps(result))
            temporary.replace(control / f"response-{sequence}.json")
            if action == "stop":
                return
        raise TimeoutError("Isolated producer supervisor exceeded 180 seconds")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--control-dir", type=Path)
    arguments = parser.parse_args()
    if arguments.control_dir is not None:
        supervise(arguments.data_dir, arguments.control_dir)
    else:
        uvicorn.run(make_app(arguments.data_dir, resume=arguments.resume, paused=arguments.paused),
                    host="0.0.0.0", port=8000, access_log=False)
