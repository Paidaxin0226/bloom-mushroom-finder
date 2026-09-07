from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any


class EventHub:
    """Thread-safe fan-out from Python workers to WebSocket subscribers."""

    def __init__(self, queue_size: int = 64) -> None:
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._queue_size)
        with self._lock:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._queues.discard(queue)

    def publish(self, event: Mapping[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        payload = dict(event)
        loop.call_soon_threadsafe(self._fan_out, payload)

    def _fan_out(self, payload: dict[str, Any]) -> None:
        with self._lock:
            queues = tuple(self._queues)
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass
