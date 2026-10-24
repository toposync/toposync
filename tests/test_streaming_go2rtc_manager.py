import asyncio
from pathlib import Path
from types import SimpleNamespace

from toposync_ext_streaming.api.models import StreamingMseSidecarSettings
from toposync_ext_streaming.streaming import go2rtc_manager


def test_sidecar_preserves_fallback_port_until_settings_change(monkeypatch, tmp_path: Path):
    async def scenario():
        processes = []
        occupied = {18764}

        class Process:
            returncode = None

            def __init__(self, port):
                self.pid = len(processes) + 1
                self.port = port
                occupied.add(port)

            def terminate(self):
                self.returncode = 0
                occupied.remove(self.port)

            async def wait(self):
                return self.returncode

        async def spawn(*args, **kwargs):
            config = Path(args[2]).read_text()
            port = int(config.split("listen: '127.0.0.1:")[1].split("'")[0])
            process = Process(port)
            processes.append(process)
            return process

        monkeypatch.setattr(go2rtc_manager, "_can_bind", lambda host, port: port not in occupied)
        monkeypatch.setattr(go2rtc_manager, "detect_go2rtc_platform", lambda: SimpleNamespace(key="test"))
        monkeypatch.setattr(go2rtc_manager, "extract_go2rtc_binary", lambda **kwargs: tmp_path / "go2rtc")
        monkeypatch.setattr(go2rtc_manager.asyncio, "create_subprocess_exec", spawn)
        manager = go2rtc_manager.Go2RtcSidecarManager(data_dir=tmp_path)
        settings = StreamingMseSidecarSettings(enabled=True, api_port=18764)
        streams = {"front": "rtsp://127.0.0.1:8554/front"}
        try:
            first = await manager.ensure_running(settings, streams=streams)
            assert first.api_port == 18765
            assert first.warnings
            for _ in range(3):
                clients = await asyncio.gather(
                    manager.ensure_running(settings, streams=streams),
                    manager.ensure_running(settings, streams=streams),
                )
                for current in clients:
                    assert current.pid == first.pid
                    assert current.restart_count == 1
                    assert current.warnings == first.warnings

            # Even if the preferred port becomes free, do not interrupt viewers.
            occupied.remove(18764)
            assert (await manager.ensure_running(settings, streams=streams)).pid == first.pid
            streams["zoom"] = "rtsp://127.0.0.1:8554/zoom"
            changed = await manager.ensure_running(settings, streams=streams)
            assert changed.api_port == first.api_port
            assert changed.restart_count == 2
            assert changed.stream_count == 2

            settings.api_port = changed.api_port
            for _ in range(2):
                current = await manager.ensure_running(settings, streams=streams)
                assert current.pid == changed.pid
                assert not current.warnings

            settings.api_port = 18770
            changed = await manager.ensure_running(settings, streams=streams)
            assert changed.api_port == 18770
            assert changed.restart_count == 3
            assert not changed.warnings

            await manager.stop()
            settings.api_port = 18764
            assert (await manager.ensure_running(settings, streams=streams)).api_port == 18764
        finally:
            await manager.stop()

    asyncio.run(scenario())
