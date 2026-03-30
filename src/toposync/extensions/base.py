from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, Awaitable, Callable

from fastapi import FastAPI

from toposync.extensions.manifest import ExtensionManifest
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry

logger = logging.getLogger("toposync.extensions")
_SHUTDOWN_CALLBACKS_STATE_KEY = "_toposync_extension_shutdown_callbacks"


ExtensionShutdownCallback = Callable[[], Any | Awaitable[Any]]


def register_extension_shutdown_callback(app: FastAPI, callback: ExtensionShutdownCallback) -> None:
    callbacks = getattr(app.state, _SHUTDOWN_CALLBACKS_STATE_KEY, None)
    if not isinstance(callbacks, list):
        callbacks = []
        setattr(app.state, _SHUTDOWN_CALLBACKS_STATE_KEY, callbacks)
    callbacks.append(callback)


async def run_extension_shutdown_callbacks(app: FastAPI) -> None:
    callbacks = getattr(app.state, _SHUTDOWN_CALLBACKS_STATE_KEY, None)
    if not isinstance(callbacks, list) or not callbacks:
        setattr(app.state, _SHUTDOWN_CALLBACKS_STATE_KEY, [])
        return

    setattr(app.state, _SHUTDOWN_CALLBACKS_STATE_KEY, [])
    for callback in reversed(callbacks):
        try:
            maybe = callback()
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            logger.warning("Extension shutdown callback failed.", exc_info=True)


@dataclass(slots=True)
class BaseExtension:
    package: str
    manifest_name: str = "extension.json"
    static_dir: str = "static"

    _manifest: ExtensionManifest | None = None

    def manifest(self) -> ExtensionManifest:
        if self._manifest is None:
            raw = resources.files(self.package).joinpath(self.manifest_name).read_text(encoding="utf-8")
            self._manifest = ExtensionManifest.model_validate(json.loads(raw))
        return self._manifest

    def static_root(self) -> Traversable | None:
        root = resources.files(self.package).joinpath(self.static_dir)
        if root.is_dir():
            return root
        return None

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        return None

    async def shutdown(self) -> None:
        return None

    def capabilities(self) -> dict[str, Any]:
        return {}
