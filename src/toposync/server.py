"""Notify long-lived responses before Uvicorn waits for HTTP connections."""

import asyncio

import uvicorn


class Server(uvicorn.Server):
    async def startup(self, sockets=None):
        self.lifespan.state["toposync_shutdown"] = asyncio.Event()
        await super().startup(sockets)

    async def shutdown(self, sockets=None):
        self.lifespan.state["toposync_shutdown"].set()
        await super().shutdown(sockets)


def run(app, **options):
    Server(uvicorn.Config(app, **options)).run()
