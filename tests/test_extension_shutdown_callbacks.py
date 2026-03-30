from __future__ import annotations

import asyncio

from fastapi import FastAPI

from toposync.extensions import register_extension_shutdown_callback, run_extension_shutdown_callbacks


def test_run_extension_shutdown_callbacks_runs_in_reverse_order_and_clears_state() -> None:
    app = FastAPI()
    events: list[str] = []

    def first() -> None:
        events.append("first")

    async def second() -> None:
        events.append("second")

    register_extension_shutdown_callback(app, first)
    register_extension_shutdown_callback(app, second)

    asyncio.run(run_extension_shutdown_callbacks(app))

    assert events == ["second", "first"]

    asyncio.run(run_extension_shutdown_callbacks(app))

    assert events == ["second", "first"]


def test_run_extension_shutdown_callbacks_continues_after_errors() -> None:
    app = FastAPI()
    events: list[str] = []

    def first() -> None:
        events.append("first")

    def failing() -> None:
        raise RuntimeError("boom")

    register_extension_shutdown_callback(app, first)
    register_extension_shutdown_callback(app, failing)

    asyncio.run(run_extension_shutdown_callbacks(app))

    assert events == ["first"]
