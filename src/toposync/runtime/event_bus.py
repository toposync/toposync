from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


UNSET = object()


@dataclass(slots=True)
class EventOutcome:
    stop_propagation: bool = False
    prevent_default: bool = False
    payload: Any = UNSET
    result: Any = UNSET
    exception: Exception | None = None


@dataclass(slots=True)
class EmitResult:
    payload: Any
    result: Any
    prevented_default: bool
    stopped: bool
    outcome: EventOutcome | None = None


Handler = Callable[[Any, dict[str, Any]], Awaitable[EventOutcome | None] | EventOutcome | None]
DefaultHandler = Callable[[Any], Awaitable[Any] | Any]


def _maybe_await(value: Any) -> Awaitable[Any]:
    if inspect.isawaitable(value):
        return value  # type: ignore[return-value]

    async def _wrap() -> Any:
        return value

    return _wrap()


@dataclass(slots=True)
class _RegisteredHandler:
    priority: int
    handler: Handler


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[_RegisteredHandler]] = {}
        self._default_handlers: dict[str, DefaultHandler] = {}

    def on(self, event_name: str, handler: Handler, *, priority: int = 0) -> None:
        items = self._handlers.setdefault(event_name, [])
        items.append(_RegisteredHandler(priority=priority, handler=handler))
        items.sort(key=lambda h: h.priority, reverse=True)

    def set_default_handler(self, event_name: str, handler: DefaultHandler) -> None:
        self._default_handlers[event_name] = handler

    async def emit(
        self,
        event_name: str,
        payload: Any,
        *,
        context: dict[str, Any] | None = None,
        default: DefaultHandler | None = None,
    ) -> EmitResult:
        ctx = context or {}
        current_payload = payload
        current_result: Any = UNSET
        prevented_default = False
        stopped = False
        outcome: EventOutcome | None = None

        for item in self._handlers.get(event_name, []):
            try:
                maybe = item.handler(current_payload, ctx)
                outcome = await _maybe_await(maybe)
            except Exception as exc:  # noqa: BLE001
                outcome = EventOutcome(exception=exc, stop_propagation=True, prevent_default=True)

            if outcome is None:
                continue
            if outcome.payload is not UNSET:
                current_payload = outcome.payload
            if outcome.result is not UNSET:
                current_result = outcome.result
            if outcome.prevent_default:
                prevented_default = True
            if outcome.stop_propagation:
                stopped = True
                break

        if default is None:
            default = self._default_handlers.get(event_name)

        if not prevented_default and default is not None and current_result is UNSET:
            current_result = await _maybe_await(default(current_payload))

        if current_result is UNSET:
            current_result = None

        return EmitResult(
            payload=current_payload,
            result=current_result,
            prevented_default=prevented_default,
            stopped=stopped,
            outcome=outcome,
        )
