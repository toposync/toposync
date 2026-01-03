from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable


Service = Callable[..., Awaitable[Any] | Any]


async def _call(service: Service, **kwargs: Any) -> Any:
    value = service(**kwargs)
    if inspect.isawaitable(value):
        return await value
    return value


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, Service] = {}

    def register(self, service_id: str, service: Service) -> None:
        self._services[service_id] = service

    async def call(self, service_id: str, **kwargs: Any) -> Any:
        service = self._services.get(service_id)
        if service is None:
            raise KeyError(f"Unknown service: {service_id}")
        return await _call(service, **kwargs)
