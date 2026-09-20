from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Subscription:
    notification_id: str | None = None
    include_ephemeral_image: bool = False
    expiry: asyncio.TimerHandle | None = None
    transient: dict[str, Any] | None = None


def _clear_ephemeral(subscription: _Subscription) -> None:
    if subscription.expiry:
        subscription.expiry.cancel()
        subscription.expiry = None
    if subscription.transient is not None:
        subscription.transient.pop("ephemeralImage", None)
        subscription.transient = None


class EventBroadcaster:
    def __init__(self, *, max_queue_size: int = 250) -> None:
        self._subscribers: dict[asyncio.Queue[dict[str, Any]], _Subscription] = {}
        self._max_queue_size = max(50, int(max_queue_size))

    @property
    def ephemeral_image_stream_count(self) -> int:
        return sum(item.include_ephemeral_image for item in self._subscribers.values())

    def subscribe(
        self, *, notification_id: str | None = None, include_ephemeral_image: bool = False
    ) -> asyncio.Queue[dict[str, Any]]:
        if include_ephemeral_image:
            if not notification_id:
                raise ValueError("Ephemeral images require a selected notification")
            if self.ephemeral_image_stream_count >= 16:
                raise ValueError("Too many ephemeral image streams")
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=1 if include_ephemeral_image else self._max_queue_size
        )
        self._subscribers[q] = _Subscription(notification_id, include_ephemeral_image)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        subscription = self._subscribers.pop(q, None)
        if subscription:
            _clear_ephemeral(subscription)
        if subscription and subscription.include_ephemeral_image:
            while not q.empty():
                q.get_nowait()

    def publish(
        self, event: dict[str, Any], *, ephemeral_image: dict[str, Any] | None = None,
        include_global: bool = True,
    ) -> None:
        notification = event.get("notification")
        notification_id = notification.get("id") if isinstance(notification, dict) else None
        # A separate argument prevents transient bytes from entering stored/public records.
        validated_monotonic = time.monotonic()
        validated_unix_ms = time.time() * 1000
        image = self._bounded_image(ephemeral_image, now_ms=validated_unix_ms)
        deadline = validated_monotonic + (image["expiresAt"] - validated_unix_ms) / 1000 if image else 0
        for q, subscription in list(self._subscribers.items()):
            if subscription.notification_id is not None:
                if subscription.notification_id != notification_id:
                    continue
            elif not include_global:
                continue
            outgoing = event
            if subscription.include_ephemeral_image:
                _clear_ephemeral(subscription)
                if image is not None and isinstance(notification, dict) and time.monotonic() < deadline:
                    transient = {**notification, "ephemeralImage": image}
                    subscription.transient = transient
                    outgoing = {**event, "notification": transient}
                    subscription.expiry = asyncio.get_running_loop().call_later(
                        max(0.0, deadline - time.monotonic()),
                        _clear_ephemeral, subscription,
                    )
            try:
                q.put_nowait(outgoing)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(outgoing)
                except asyncio.QueueFull:
                    pass

    @staticmethod
    def _bounded_image(image: dict[str, Any] | None, *, now_ms: float | None = None) -> dict[str, Any] | None:
        if not isinstance(image, dict):
            return None
        expiry = image.get("expiresAt")
        if isinstance(expiry, bool) or not isinstance(expiry, (float, int)):
            return None
        try:
            if not math.isfinite(expiry) or not 0 < expiry - (time.time() * 1000 if now_ms is None else now_ms) <= 750:
                return None
            encoded = json.dumps(image, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 384 * 1024:
                return None
            return json.loads(encoded)
        except (ValueError, TypeError, RecursionError, OverflowError):
            return None
