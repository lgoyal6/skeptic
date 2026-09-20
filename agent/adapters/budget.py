"""
One request budget, shared by every adapter talking to the same tool process.

`min_interval` on an adapter paces that adapter alone. It is computed from the
tool's documented limit -- 3 requests per second for the lab -- under the
assumption that the adapter is the only client. Fan a settle run out across
threads and that assumption is false: four adapters each correctly paced at
2.5 req/s put 10 req/s on a process-wide 3 req/s budget, and the audit
measured 23 of 32 calls coming back 429 at exactly that setting.

Lowering the worker count reduces the overshoot; it does not remove it, because
the per-adapter pacer has no way to know how many siblings it has. Two workers
at 0.4s is still ~5 req/s against a budget of 3. The limit is global, so the
thing that enforces it has to be global too.

This is that thing: a sliding window of send times, shared by reference across
every adapter in a run. Callers block in `acquire()` until a slot is free, so
the fleet as a whole never exceeds `n` requests per `window_s` no matter how
many of them there are.

A 429 is not merely slow. Every probe template reads the response body, and an
error body is still a dict -- which is how a rate-limited create came to look
exactly like a field silently nulled by the server. Staying under the budget is
what keeps the experiments measuring their own variable.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager


class RateBudget:
    """A process-wide sliding-window request budget.

    `safety_s` widens the client's window past the server's. The server stamps
    arrival time and we stamp send time; scheduling and network delay mean
    those orderings are not identical, so a client pacing to exactly the
    server's window sits on the boundary and trips it on jitter alone.
    """

    def __init__(self, n: int = 3, window_s: float = 1.0, safety_s: float = 0.05) -> None:
        if n < 1:
            raise ValueError("a budget of fewer than one request cannot make progress")
        self.n = n
        self.window_s = window_s + safety_s
        self._lock = threading.Lock()
        self._sent: deque[float] = deque()
        # Observability: a run that spent most of its wall clock waiting on the
        # budget is a run that should have had fewer workers, and that should
        # be visible rather than inferred from a slow stopwatch.
        self.granted = 0
        self.waits = 0
        self.waited_s = 0.0
        # The moment each send was authorised, recorded inside the lock. A
        # caller timing itself after `acquire()` returns measures thread
        # scheduling as much as pacing: under load, four grants correctly
        # spaced can be observed within one second because the observations,
        # not the grants, bunched up. The budget's own log is the only record
        # of when it actually permitted a send.
        self.grant_log: list[float] = []

    def acquire(self) -> None:
        """Block until this caller may send, then record the send."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= self.window_s:
                    self._sent.popleft()
                if len(self._sent) < self.n:
                    self._sent.append(now)
                    self.granted += 1
                    self.grant_log.append(now)
                    return
                sleep_for = self.window_s - (now - self._sent[0])
                self.waits += 1
                self.waited_s += max(sleep_for, 0.0)
            time.sleep(max(sleep_for, 0.001))

    @contextmanager
    def exclusive(self):
        """Hold the whole budget so one experiment can burst alone.

        `header_burst` exists to trip the rate limiter and read what comes
        back, so it cannot run under a budget designed to prevent tripping it.
        Exempting it is not enough either: a burst fired while siblings are
        sending makes THEIR calls 429, and those 429s are the burst's doing
        rather than the tool's -- the sibling probe would be measuring this
        probe. So the bursting experiment takes the budget instead of
        bypassing it, and everyone else waits.

        On release the window is refilled rather than cleared, because the
        server's own window is saturated by a burst this budget never saw.
        The next caller waits it out instead of inheriting a limiter that is
        already full.
        """
        self._lock.acquire()
        try:
            yield
        finally:
            now = time.monotonic()
            self._sent.clear()
            self._sent.extend([now] * self.n)
            self._lock.release()

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "n_per_window": self.n,
                "window_s": round(self.window_s, 3),
                "granted": self.granted,
                "waits": self.waits,
                "waited_s": round(self.waited_s, 2),
                "worst_window": self.worst_window(),
            }

    def worst_window(self) -> int:
        """The most grants this budget ever authorised inside one window.

        The number the limit is about. If it exceeds `n`, the budget failed,
        and no amount of wall-clock timing elsewhere changes that.
        """
        g = sorted(self.grant_log)
        if not g:
            return 0
        return max(sum(1 for t in g if start <= t < start + self.window_s) for start in g)
