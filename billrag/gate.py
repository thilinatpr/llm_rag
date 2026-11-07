"""Admission control for the web server: at most `slots` questions run at once, a bounded FIFO queue waits behind them. Load test on 1/4 vCPU: AGENTS.md "Load test".

Most of a question's time is spent waiting on the hosted model, so several can overlap on one small container. More than
`slots` at once would only stretch every CPU-bound step (1/4 vCPU), so the rest queue in arrival order. A full queue, or a wait
longer than `max_wait` seconds, raises `Busy` and the server answers 503 (the page tells the user to retry) instead of letting
requests pile up until a proxy times out.

Two lanes share the slots. "member" (signed-in users) may use all of them. "public" (anonymous visitors, marked by the Worker's
X-Lane header) may use at most `public_slots` and has its own short queue, so a flood of public questions can never take the
last slots or the queue places away from members. Members waiting always go before public requests.
"""
import threading
import time
from collections import deque


class Busy(Exception):
    """The server cannot take this question now. `str(e)` is safe to show to the user."""


class Gate:
    def __init__(self, slots=4, max_queue=10, max_wait=90.0, public_slots=1, public_queue=3):
        self.slots, self.max_queue, self.max_wait = max(1, slots), max(0, max_queue), max_wait
        self.public_slots, self.public_queue = max(0, min(public_slots, self.slots)), max(0, public_queue)
        self.cond = threading.Condition()
        self.running = self.running_public = 0
        self.waiting, self.waiting_public = deque(), deque()
        self.rejected = 0

    def counts(self):
        with self.cond:
            return {"running": self.running, "queued": len(self.waiting) + len(self.waiting_public),
                    "max_concurrent": self.slots, "max_queue": self.max_queue, "rejected": self.rejected,
                    "public_running": self.running_public, "public_queued": len(self.waiting_public),
                    "public_slots": self.public_slots, "public_queue": self.public_queue}

    def _may_start(self, public):
        if self.running >= self.slots:
            return False
        return not public or (self.running_public < self.public_slots and not self.waiting)

    def _take(self, public):
        self.running += 1
        if public:
            self.running_public += 1

    def acquire(self, on_wait=None, lane="member"):
        """Block until a slot is free -> (seconds waited, questions running now including this one).
        `on_wait(position)` is called (outside the lock) when this request starts waiting and each time its place in its lane's
        line changes; position 1 = next. Raises Busy when that lane's queue is full or the wait exceeds `max_wait`."""
        public = lane == "public"
        line, limit = (self.waiting_public, self.public_queue) if public else (self.waiting, self.max_queue)
        start = time.time()
        with self.cond:
            if not line and self._may_start(public):
                self._take(public)
                return 0.0, self.running
            if len(line) >= limit:
                self.rejected += 1
                raise Busy("The assistant is busy right now. Please try again in a minute.")
            me = object()
            line.append(me)
        last = None
        try:
            while True:
                with self.cond:
                    pos = line.index(me) + 1
                    if pos == 1 and self._may_start(public):
                        line.popleft()
                        self._take(public)
                        self.cond.notify_all()  # the next in line may fit too if slots freed together
                        return time.time() - start, self.running
                    left = self.max_wait - (time.time() - start)
                    if left <= 0:
                        line.remove(me)
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
                if me in line:
                    line.remove(me)
                    self.cond.notify_all()
            raise

    def release(self, lane="member"):
        with self.cond:
            self.running -= 1
            if lane == "public":
                self.running_public -= 1
            self.cond.notify_all()
