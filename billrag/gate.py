"""Admission control for the web server: at most `slots` questions run at once, a bounded FIFO queue waits behind them.

Most of a question's time is spent waiting on the hosted model, so several can overlap on one small container. More than
`slots` at once would only stretch every CPU-bound step (1/4 vCPU), so the rest queue in arrival order. A full queue, or a wait
longer than `max_wait` seconds, raises `Busy` and the server answers 503 (the page tells the user to retry) instead of letting
requests pile up until a proxy times out.
"""
import threading
import time
from collections import deque


class Busy(Exception):
    """The server cannot take this question now. `str(e)` is safe to show to the user."""


class Gate:
    def __init__(self, slots=4, max_queue=10, max_wait=90.0):
        self.slots, self.max_queue, self.max_wait = max(1, slots), max(0, max_queue), max_wait
        self.cond = threading.Condition()
        self.running, self.waiting, self.rejected = 0, deque(), 0

    def counts(self):
        with self.cond:
            return {"running": self.running, "queued": len(self.waiting), "max_concurrent": self.slots,
                    "max_queue": self.max_queue, "rejected": self.rejected}

    def acquire(self, on_wait=None):
        """Block until a slot is free -> (seconds waited, questions running now including this one).
        `on_wait(position)` is called (outside the lock) when this request starts waiting and each time its place in line
        changes; position 1 = next. Raises Busy when the queue is full or the wait exceeds `max_wait`."""
        start = time.time()
        with self.cond:
            if self.running < self.slots and not self.waiting:
                self.running += 1
                return 0.0, self.running
            if len(self.waiting) >= self.max_queue:
                self.rejected += 1
                raise Busy("The assistant is busy right now. Please try again in a minute.")
            me = object()
            self.waiting.append(me)
        last = None
        try:
            while True:
                with self.cond:
                    pos = self.waiting.index(me) + 1
                    if pos == 1 and self.running < self.slots:
                        self.waiting.popleft()
                        self.running += 1
                        self.cond.notify_all()  # the next in line may fit too if slots freed together
                        return time.time() - start, self.running
                    left = self.max_wait - (time.time() - start)
                    if left <= 0:
                        self.waiting.remove(me)
                        self.rejected += 1
                        self.cond.notify_all()
                        raise Busy("Waited too long for a free place. Please try again in a minute.")
                    if pos == last:
                        self.cond.wait(min(left, 5.0))
                        continue
                if on_wait:
                    try:
                        on_wait(pos)
                    except Exception:
                        pass  # a dead browser connection must not lose the question's place
                last = pos
        except BaseException:
            with self.cond:
                if me in self.waiting:
                    self.waiting.remove(me)
                    self.cond.notify_all()
            raise

    def release(self):
        with self.cond:
            self.running -= 1
            self.cond.notify_all()
