from __future__ import annotations

from types import SimpleNamespace

from toposync_ext_cinematic.director.camera_pool import CameraPoolFrame
from toposync_ext_cinematic.director.runtime import CinematicDirectorRuntime
from toposync_ext_cinematic.director.state import ShotDecision
from toposync_ext_cinematic.status import get_cinematic_status_store


def test_status_keeps_last_published_frame_during_frame_wait() -> None:
    store = get_cinematic_status_store()
    store.clear()
    runtime = CinematicDirectorRuntime(
        config=SimpleNamespace(fps=5),
        dependencies=SimpleNamespace(services=None),
    )
    context = SimpleNamespace(pipeline_name="cinematic_test", node_id="director")
    runtime._stream_open = True

    try:
        runtime._publish_status(
            context,
            now=100.0,
            decision=ShotDecision(camera_id="front", source_id="main", mode="idle", reason="idle_round"),
            frame=CameraPoolFrame(
                camera_id="front",
                source_id="main",
                frame=object(),
                frame_ts=99.5,
                width=640,
                height=360,
                fresh=True,
            ),
        )
        runtime._publish_status(
            context,
            now=101.0,
            decision=ShotDecision(camera_id="front", source_id="main", mode="idle", reason="idle_hold"),
            reason="waiting_frame",
        )

        item = store.snapshot()["items"][0]
        assert item["active_camera_id"] == "front"
        assert item["active_source_id"] == "main"
        assert item["cut_reason"] == "idle_hold"
        assert item["frame_width"] == 640
        assert item["frame_height"] == 360
        assert item["frame_age_seconds"] == 1.5
    finally:
        store.clear()
