"""Real tracker-envelope -> core.notify -> TypeScript reader, without video/models."""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync_ext_vision.pipelines.schemas import VisionTrackConfig
from toposync_ext_vision.processing.tasks.event_assembler import TrackEventAssembler, _EventState


def test_tracker_parent_frame_survives_notify_projection_and_uncalibrated_reader():
    root = Path(__file__).resolve().parents[1]
    if not shutil.which("node") or not (root / "node_modules/typescript").is_dir():
        pytest.skip("Cross-language contract requires the existing frontend dependencies")
    source = Packet.create(
        stream_id="camera-source", lifecycle=Lifecycle.UPDATE,
        payload={
            "camera_id": "camera-a", "source_stream_id": "camera-source",
            "capture_evidence": {"capture_instance": "capture", "generation": 1,
                                 "sequence": 1, "published_at": 1000},
            "vision": {"pose_media_ts": 42, "poses": [{"tracking_id": "track-a",
                "camera_id": "camera-a", "source_stream_id": "camera-source",
                "landmarks": [{"name": "left_wrist", "position": [0.4, 0.2], "provenance": "image_estimate"}]}]},
            "spatial": {"camera": {"status": "unavailable", "reason": "ground_calibration_required"}},
        },
    )
    source.payload["vision"]["pose_frame_packet_id"] = source.packet_id
    assembler = TrackEventAssembler(VisionTrackConfig())
    state = _EventState(event_id="actor-a", event_code="1", stream_id="event-stream",
                        source_stream_id=source.stream_id, correlation_id="correlation", label="person")
    child = assembler._build_event_packet(source, lifecycle=Lifecycle.UPDATE,
        object_data={"tracking_id": "track-a", "camera_id": "camera-a", "label": "person",
                     "score": 0.9, "bbox01": [0.1, 0.1, 0.6, 0.9]}, state=state)
    assert child.packet_id != source.packet_id
    assert child.parent_packet_id == source.packet_id
    assert child.payload["vision"]["poses"][0]["actor_subject_id"] == "actor-a"
    emitted = []

    async def upsert(**kwargs):
        emitted.append(kwargs)

    notify = NotifyRuntime({"notification_type": "com.toposync.cameras.human_observation",
        "update_interval_seconds": 0,
        "include_payload_paths": ["subject", "camera_id", "source_stream_id", "capture_evidence",
            "vision.poses", "vision.pose_media_ts", "vision.pose_frame_packet_id", "spatial.camera.status"]},
        PipelineRuntimeDependencies(notifications_upsert=upsert))
    asyncio.run(notify.process_packet(child, SimpleNamespace(pipeline_name="fixture", node_id="notify")))
    notification = {"id": "notification", "type": emitted[0]["type"], "title": "Fixture", "payload": emitted[0]["payload"]}
    script = """
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const source = fs.readFileSync('extensions/cameras/ui/src/notifications/humanObservation.ts', 'utf8');
const context = {exports:{}};
vm.runInNewContext(ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText, context);
const notification = JSON.parse(fs.readFileSync(0,'utf8'));
const good = context.exports.readHumanObservation(notification, 1000100);
notification.payload.parent_packet_id = 'unrelated-frame';
const bad = context.exports.readHumanObservation(notification, 1000100);
process.stdout.write(JSON.stringify({good,bad}));
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps(notification), text=True,
                            capture_output=True, cwd=root, check=True)
    models = json.loads(result.stdout)
    assert models["good"]["state"] == "current"
    assert models["good"]["actor"] == "actor-a"
    assert models["good"]["pose2D"][0]["name"] == "left_wrist"
    assert models["good"]["body"]["position"] is None
    assert models["bad"]["state"] == "unavailable"
