"""Global, resizable concurrency gate for LLM-bound work (text-parse jobs).

The task system (``core/tasks.py``) spawns one daemon thread per task with no throttle,
so an N-file parse batch would open N simultaneous LLM requests. This gate bounds how
many parse jobs run LLM work at once (sized from ``config.generation.max_concurrency``
at the start of a batch) so a large batch can't saturate the LLM service / CPU.

It is a ``threading.Condition``-based gate rather than a ``threading.Semaphore`` so the
limit can grow *or* shrink between batches. The limit is always clamped to ``>= 1`` —
there is deliberately no "unlimited" mode (the whole point is to cap in-flight LLM
calls) — so ``acquire``/``release`` stay perfectly balanced: every ``acquire`` that
takes a slot is matched by exactly one ``release``.
"""
from __future__ import annotations

import threading


class ConcurrencyGate:
    """A resizable permit gate.

    ``set_limit(n)`` clamps to ``>= 1`` and wakes any waiters (so a raised limit lets
    queued jobs in, and a lowered one simply stops admitting new ones until the active
    count drops below it). ``acquire`` blocks until a slot is free; ``release`` frees a
    slot and wakes one waiter. ``active`` reports how many slots are currently held.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._limit = 1  # always >= 1 (no "unlimited" mode)
        self._active = 0

    def set_limit(self, n: int) -> None:
        """Set the max number of concurrent holders (clamped to ``>= 1``)."""
        with self._cond:
            self._limit = max(1, int(n or 1))
            self._cond.notify_all()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def active(self) -> int:
        """How many slots are currently held (useful for tests / UI)."""
        with self._cond:
            return self._active

    def acquire(self) -> None:
        """Block until a slot is free, then take it."""
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait()
            self._active += 1

    def release(self) -> None:
        """Free the slot this thread took (guarded against underflow)."""
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()


# Module-level singleton shared by every parse worker in this process.
_gate = ConcurrencyGate()


def set_concurrency(n: int) -> None:
    """Size the process-wide gate (call at the start of a parse batch)."""
    _gate.set_limit(n)


def gate() -> ConcurrencyGate:
    """The process-wide gate."""
    return _gate
