from __future__ import annotations

import asyncio
import threading

from toposync_ext_cameras.processing.camera_hub import CameraHub


class Reader:
    def __init__(self, _url: str, *, target_fps: float, backend: str) -> None:
        self.target_fps = target_fps
        self.backend = backend
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True

    def metrics_snapshot(self):
        return {"target_fps": self.target_fps, "opened": not self.stopped}


async def acquire(hub, fps):
    return await hub.acquire(
        key="camera:front", rtsp_url="rtsp://example", target_fps=fps, backend="ffmpeg"
    )


def test_last_high_frequency_consumer_leaving_restores_analysis_and_release_is_idempotent():
    async def scenario():
        readers = []

        def factory(*args, **kwargs):
            reader = Reader(*args, **kwargs)
            readers.append(reader)
            return reader

        hub = CameraHub(frame_grabber_factory=factory)
        analysis = await acquire(hub, 5)
        first = await acquire(hub, 25)
        second = await acquire(hub, 25)
        await hub.release(key="camera:front", grabber=first)
        assert analysis.target_fps == 25
        await hub.release(key="camera:front", grabber=first)
        assert (await hub.snapshot())[0]["refcount"] == 2
        await hub.release(key="camera:front", grabber=second)
        assert analysis.target_fps == 5
        assert analysis.metrics_snapshot() == {"target_fps": 5, "opened": True}
        assert len(readers) == 3
        assert all(reader.stopped for reader in readers[:-1])
        assert (await hub.snapshot())[0]["refcount"] == 1
        await hub.release(key="camera:front", grabber=analysis)
        assert await hub.snapshot() == []
        assert all(reader.stopped for reader in readers)

    asyncio.run(scenario())


def test_failed_reduction_preserves_active_reader_without_repeated_attempts():
    async def scenario():
        readers = []

        class FailingReader(Reader):
            def start(self):
                if len(readers) == 3:
                    raise RuntimeError("replacement failed")
                return self

        def factory(*args, **kwargs):
            reader = FailingReader(*args, **kwargs)
            readers.append(reader)
            return reader

        hub = CameraHub(frame_grabber_factory=factory)
        analysis = await acquire(hub, 5)
        player = await acquire(hub, 25)
        await hub.release(key="camera:front", grabber=player)
        assert analysis.metrics_snapshot() == {"target_fps": 25, "opened": True}
        assert readers[-1].stopped
        await hub.release(key="camera:front", grabber=player)
        assert len(readers) == 3
        await hub.release(key="camera:front", grabber=analysis)
        assert all(reader.stopped for reader in readers)
        assert await hub.snapshot() == []

    asyncio.run(scenario())


def test_new_high_demand_during_reduction_keeps_sufficient_capture():
    async def scenario():
        readers = []
        started, finish = threading.Event(), threading.Event()

        class BlockingReader(Reader):
            def start(self):
                if len(readers) == 3:
                    started.set()
                    assert finish.wait(2)
                return self

        def factory(*args, **kwargs):
            reader = BlockingReader(*args, **kwargs)
            readers.append(reader)
            return reader

        hub = CameraHub(frame_grabber_factory=factory)
        analysis = await acquire(hub, 5)
        player = await acquire(hub, 25)
        releasing = asyncio.create_task(hub.release(key="camera:front", grabber=player))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            new_player = await acquire(hub, 25)
        finally:
            finish.set()
        await releasing
        assert analysis.target_fps == new_player.target_fps == 25
        assert readers[2].stopped
        assert not readers[1].stopped
        await hub.release(key="camera:front", grabber=new_player)
        assert analysis.target_fps == 5
        await hub.release(key="camera:front", grabber=analysis)
        assert all(reader.stopped for reader in readers)

    asyncio.run(scenario())


def test_final_consumer_leaving_during_reduction_releases_both_readers():
    async def scenario():
        readers = []
        started, finish = threading.Event(), threading.Event()

        class BlockingReader(Reader):
            def start(self):
                if len(readers) == 3:
                    started.set()
                    assert finish.wait(2)
                return self

        def factory(*args, **kwargs):
            reader = BlockingReader(*args, **kwargs)
            readers.append(reader)
            return reader

        hub = CameraHub(frame_grabber_factory=factory)
        analysis = await acquire(hub, 5)
        player = await acquire(hub, 25)
        releasing = asyncio.create_task(hub.release(key="camera:front", grabber=player))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await hub.release(key="camera:front", grabber=analysis)
        finally:
            finish.set()
        await releasing
        assert await hub.snapshot() == []
        assert all(reader.stopped for reader in readers)

    asyncio.run(scenario())


def test_canceled_reduction_releases_candidate_and_allows_future_acquisition():
    async def scenario():
        readers = []
        started, finish = threading.Event(), threading.Event()

        class BlockingReader(Reader):
            def start(self):
                if len(readers) == 3:
                    started.set()
                    assert finish.wait(2)
                return self

        def factory(*args, **kwargs):
            reader = BlockingReader(*args, **kwargs)
            readers.append(reader)
            return reader

        hub = CameraHub(frame_grabber_factory=factory)
        analysis = await acquire(hub, 5)
        player = await acquire(hub, 25)
        releasing = asyncio.create_task(hub.release(key="camera:front", grabber=player))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            releasing.cancel()
            try:
                await releasing
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Release cancellation was swallowed")
        finally:
            finish.set()
        assert readers[2].stopped
        assert analysis.metrics_snapshot()["opened"]
        higher = await asyncio.wait_for(acquire(hub, 30), 1)
        assert higher.target_fps == analysis.target_fps == 30
        await hub.release(key="camera:front", grabber=analysis)
        await hub.release(key="camera:front", grabber=higher)
        assert await hub.snapshot() == []
        assert all(reader.stopped for reader in readers)

    asyncio.run(scenario())


def test_reader_finishing_native_open_after_timeout_is_closed():
    async def scenario():
        finish, stopped_after_finish = threading.Event(), threading.Event()

        class LateReader(Reader):
            opened = False

            def start(self):
                assert finish.wait(2)
                self.opened = True
                return self

            def stop(self):
                if self.opened:
                    self.opened = False
                    stopped_after_finish.set()

        hub = CameraHub(frame_grabber_factory=LateReader, start_timeout_s=0.02)
        try:
            await acquire(hub, 5)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Slow native open did not time out")
        finally:
            finish.set()
        assert await asyncio.to_thread(stopped_after_finish.wait, 1)
        assert await hub.snapshot() == []

    asyncio.run(scenario())


def test_upgrade_canceled_during_old_reader_cleanup_does_not_leak_new_consumer():
    async def scenario():
        readers = []
        stopping, finish = threading.Event(), threading.Event()

        class BlockingStopReader(Reader):
            def stop(self):
                if self is readers[0]:
                    stopping.set()
                    assert finish.wait(2)
                super().stop()

        def factory(*args, **kwargs):
            reader = BlockingStopReader(*args, **kwargs)
            readers.append(reader)
            return reader

        hub = CameraHub(frame_grabber_factory=factory)
        analysis = await acquire(hub, 5)
        acquiring = asyncio.create_task(acquire(hub, 25))
        try:
            assert await asyncio.to_thread(stopping.wait, 2)
            acquiring.cancel()
            try:
                await acquiring
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Acquisition cancellation was swallowed")
            assert analysis.target_fps == 5
            assert (await hub.snapshot())[0]["refcount"] == 1
        finally:
            finish.set()
        await hub.release(key="camera:front", grabber=analysis)
        assert await hub.snapshot() == []
        assert all(reader.stopped for reader in readers[1:])

    asyncio.run(scenario())
