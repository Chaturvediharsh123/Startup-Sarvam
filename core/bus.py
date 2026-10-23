"""Thread-safe publish/subscribe event bus (PDF section "Event bus").

* Modules subscribe to the event types they care about.
* ``publish`` never waits for listeners: each subscriber has its own queue and
  its own dispatcher thread, so a slow subscriber cannot block the publisher.
* Any thread may publish (mic callbacks, the UI thread, the STT asyncio loop).
* A handler that raises is logged and never crashes the bus.

The UI polls instead of running a handler thread: :meth:`EventBus.queue` returns
a plain :class:`queue.Queue` that Tk drains with ``after()``.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.events import Event

logger = logging.getLogger(__name__)

Handler = Callable[[Any], None]
_STOP = object()


@dataclass
class _Subscriber:
    types: tuple[type[Event], ...]
    handler: Handler | None
    inbox: queue.Queue[Any]
    thread: threading.Thread | None = None

    def wants(self, event: Event) -> bool:
        return not self.types or isinstance(event, self.types)


class EventBus:
    """In-process pub/sub. Create one per app (tests create their own)."""

    def __init__(self) -> None:
        self._subs: list[_Subscriber] = []
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(
        self, types: type[Event] | Iterable[type[Event]], handler: Handler, name: str = ""
    ) -> Callable[[], None]:
        """Call ``handler(event)`` on a dedicated thread for each matching event.

        Args:
            types: one event class or several; an empty tuple means every event.
        Returns:
            A function that unsubscribes.
        """
        sub = _Subscriber(self._as_tuple(types), handler, queue.Queue())
        sub.thread = threading.Thread(
            target=self._dispatch, args=(sub,), name=f"bus-{name or 'sub'}", daemon=True
        )
        with self._lock:
            self._subs.append(sub)
        sub.thread.start()
        return lambda: self._remove(sub)

    def queue(self, types: type[Event] | Iterable[type[Event]] = ()) -> queue.Queue[Any]:
        """A queue that receives matching events, for consumers that poll (the UI)."""
        sub = _Subscriber(self._as_tuple(types), None, queue.Queue())
        with self._lock:
            self._subs.append(sub)
        return sub.inbox

    def publish(self, event: Event) -> None:
        """Hand ``event`` to every matching subscriber without waiting."""
        if self._closed:
            return
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            if sub.wants(event):
                sub.inbox.put(event)

    def close(self, timeout: float = 2.0) -> None:
        """Stop every dispatcher thread. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subs, self._subs = list(self._subs), []
        for sub in subs:
            if sub.thread is not None:
                sub.inbox.put(_STOP)
        for sub in subs:
            if sub.thread is not None:
                sub.thread.join(timeout)

    # -- internals ----------------------------------------------------------------
    @staticmethod
    def _as_tuple(types: type[Event] | Iterable[type[Event]]) -> tuple[type[Event], ...]:
        if isinstance(types, type):
            return (types,)
        return tuple(types)

    def _remove(self, sub: _Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        if sub.thread is not None:
            sub.inbox.put(_STOP)

    @staticmethod
    def _dispatch(sub: _Subscriber) -> None:
        while True:
            event = sub.inbox.get()
            if event is _STOP:
                return
            try:
                sub.handler(event)  # type: ignore[misc]
            except Exception:  # noqa: BLE001 - a bad handler must never crash the bus
                logger.exception("Bus handler failed for %s", type(event).__name__)
