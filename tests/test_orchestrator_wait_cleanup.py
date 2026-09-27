import asyncio
from types import SimpleNamespace

from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator


def test_idle_polling_and_stop_do_not_accumulate_wait_tasks():
    async def scenario():
        baseline = asyncio.all_tasks()
        cycles = 0

        async def reconcile():
            nonlocal cycles
            cycles += 1

        state = SimpleNamespace(
            _stop=asyncio.Event(),
            _reload=asyncio.Event(),
            _poll_interval_s=0.002,
            _reconcile=reconcile,
        )
        task = asyncio.create_task(PipelinesOrchestrator._run(state))
        try:
            async with asyncio.timeout(2):
                while cycles < 30:
                    await asyncio.sleep(0.005)
            pending = asyncio.all_tasks() - baseline - {task}
            assert len(pending) <= 1, f"idle polling accumulated {len(pending)} pending tasks"
            before_reload = cycles
            state._reload.set()
            async with asyncio.timeout(1):
                while cycles == before_reload:
                    await asyncio.sleep(0)
        finally:
            state._stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.sleep(0)
        assert not asyncio.all_tasks() - baseline

    asyncio.run(scenario())
