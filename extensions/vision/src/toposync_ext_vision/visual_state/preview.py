from __future__ import annotations

import asyncio
import io
from collections import deque
from dataclasses import replace
import time
import uuid

from PIL import Image

from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
from toposync.runtime.pipelines.execution import PipelineRuntime, PipelineRuntimeDependencies
from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
from toposync.runtime.pipelines.preview import (
    PipelinePreviewError, build_preview_registry, freeze_reference_packet, prepare_preview_pipeline, reference_capture_evidence,
)

from .preparation import prepare_visual_frame, visual_preparation_revision
from .runtime import VisualStateRuntime


def _encode_capture(image):
    output = io.BytesIO()
    Image.fromarray(image[:, :, ::-1]).save(output, format="PNG")
    data = output.getvalue()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("reference_capture_image_limit")
    return data


class VisualPreviewBusy(RuntimeError):
    pass


class VisualStatePreviewRunner:
    """Bounded disposable pipelines; canceled native work keeps its admission slot."""

    def __init__(self):
        self._owners = set()
        self._tasks = set()
        self._retirements = set()
        self._closed = False

    async def run(self, *, owner, configuration, pipeline, registry, dependencies, duration_ms=3000, capture=False):
        if self._closed or owner in self._owners or len(self._owners) >= 2:
            raise VisualPreviewBusy("visual_preview_busy")
        self._owners.add(owner)
        task = asyncio.current_task()
        self._tasks.add(task)
        runtime = sensor = encoding = None
        scheduler = ExecutionScheduler(thread_pool_max_workers=2)
        try:
            if not capture:
                configuration.validate_activation()
            prefix = pipeline.model_copy(deep=True)
            revision = visual_preparation_revision(configuration, prefix)
            if configuration.preparation_revision is not None and configuration.preparation_revision != revision:
                raise ValueError("visual sensor preparation changed; capture compatible references")
            preview = prepare_preview_pipeline(pipeline=prefix, registry=registry, reference_capture=True)
            preview.name = f"visual_test_{uuid.uuid4().hex}"
            preview_registry = build_preview_registry(registry)
            if not capture:
                sensor = VisualStateRuntime(configuration.model_dump(), dependencies, preview_input_pipeline=prefix)
                preview_registry._items["vision.visual_state"] = replace(
                    registry.get("vision.visual_state"), runtime_factory=lambda *_: sensor,
                )
                graph = preview.graph
                sink = next(node for node in graph["nodes"] if node["operator"] == "core.sink")
                incoming = next(edge for edge in graph["edges"] if edge["to"]["node"] == sink["id"])
                node_id = f"visual_test_{uuid.uuid4().hex}"
                incoming["to"] = {"node": node_id, "port": "in"}
                incoming["traffic"] = {"modality": "video", "semantic_class": "frame", "continuous": True}
                graph["nodes"].append({"id": node_id, "uid": node_id, "operator": "vision.visual_state",
                                       "config": configuration.model_dump(mode="json")})
                graph["edges"].append({"uid": node_id, "from": {"node": node_id, "port": "out"},
                                       "to": {"node": sink["id"], "port": "in"},
                                       "traffic": {"modality": "data"},
                                       "queue": {"max_items": 1, "drop_policy": "latest_only"}})
            states = deque(maxlen=32)
            captured = asyncio.get_running_loop().create_future()

            async def collect(packet, *_):
                if not capture:
                    states.append(packet.payload["visual_state"])
                elif not captured.done():
                    try:
                        reference_capture_evidence(packet, prefix,
                            maximum_age_ms=configuration.temporal.maximum_frame_age_ms,
                            input_artifact_name=configuration.input_artifact_name)
                        captured.set_result(freeze_reference_packet(packet, input_artifact_name=configuration.input_artifact_name))
                    except PipelinePreviewError:
                        pass

            # Construct dependencies explicitly: no live destinations, histories, snapshots or inspection stores.
            isolated = PipelineRuntimeDependencies(config_store=dependencies.config_store, services=dependencies.services,
                vision_model_registry=dependencies.vision_model_registry, execution_scheduler=scheduler,
                preview_packet_collector=collect)
            if sensor is not None:
                sensor.dependencies = isolated
            runtime = PipelineRuntime(PipelineGraphCompiler(preview_registry).compile_pipeline(preview), preview_registry, isolated)
            async with asyncio.timeout(15):
                await runtime.start()
                if capture:
                    packet = await captured
                    image, evidence = await prepare_visual_frame(configuration, prefix, packet, dependencies.services)
                    encoding = asyncio.create_task(asyncio.to_thread(_encode_capture, image))
                    data = await asyncio.shield(encoding)
                    # Encoding must not turn an expired frame into a fresh reference.
                    reference_capture_evidence(packet, prefix,
                        maximum_age_ms=configuration.temporal.maximum_frame_age_ms,
                        input_artifact_name=configuration.input_artifact_name)
                    return {"image": data, "evidence": evidence}
            started = time.monotonic()
            await asyncio.sleep(duration_ms / 1000)
            return {"test_only": True, "state": sensor._reducer.tick().model_dump(mode="json"),
                    "states": list(states), "completed_at": time.time(), "duration_ms": round((time.monotonic() - started) * 1000)}
        finally:
            cleanup = asyncio.create_task(self._dispose(owner, runtime, sensor, scheduler, task, encoding))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    async def _dispose(self, owner, runtime, sensor, scheduler, task, encoding=None):
        try:
            if runtime is not None:
                await runtime.stop()
        finally:
            if encoding is not None:
                await asyncio.gather(encoding, return_exceptions=True)
            if sensor is not None:
                await sensor.shutdown()
            await scheduler.shutdown()
            self._tasks.discard(task)
            if sensor is None or sensor.resources_released:
                self._owners.discard(owner)
            else:
                retirement = asyncio.create_task(self._retire(owner, sensor))
                self._retirements.add(retirement)
                retirement.add_done_callback(self._retirements.discard)

    async def _retire(self, owner, sensor):
        while not sensor.resources_released:
            await asyncio.sleep(.05)
        self._owners.discard(owner)

    async def shutdown(self):
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        retirements = list(self._retirements)
        for task in retirements:
            task.cancel()
        await asyncio.gather(*retirements, return_exceptions=True)
