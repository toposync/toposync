from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_core import DebugStdoutRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet


def test_core_debug_prints_to_stdout_and_dumps_images(tmp_path: Path, capsys) -> None:
    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = DebugStdoutRuntime(
            {
                "enabled": True,
                "save_images": True,
                "max_images_per_packet": 4,
                "output_dir": str(tmp_path),
                "print_payload": True,
                "print_metadata": True,
                "print_artifacts": True,
            },
            deps,
        )

        frame = np.zeros((8, 10, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "frame_ts": 1.23,
                "camera_id": "camera-main",
                "tracking_id": "trk-1",
            },
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        class _Ctx:
            pipeline_name = "test_pipeline"
            node_id = "debug"

        out = await runtime.process_packet(packet, _Ctx())
        assert out == [packet]

    asyncio.run(scenario())

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines

    payload = json.loads("\n".join(lines))
    assert payload.get("operator") == "core.debug"
    saved = payload.get("saved_images")
    assert isinstance(saved, list)
    assert saved

    png_files = list(tmp_path.rglob("*.png"))
    assert png_files
