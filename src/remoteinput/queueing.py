import threading
import time
from collections import deque

from .config import MAX_QUEUE_AGE, QUEUE_LIMIT
from .protocol import Op


class Overload(Exception):
    pass


class InputQueue:
    """Callbacks perform bounded, in-memory work and wake at most once per empty queue.

    Only adjacent relative moves coalesce. The first arrival time is retained so a
    continuously growing move cannot hide an old backlog.
    """

    def __init__(self, wake=lambda: None, limit=QUEUE_LIMIT, max_age=MAX_QUEUE_AGE, clock=time.monotonic):
        self.items = deque()
        self.lock = threading.Lock()
        self.wake = wake
        self.limit = limit
        self.max_age = max_age
        self.clock = clock
        self.failed = False

    def put(self, event):
        now = self.clock()
        with self.lock:
            if self.failed:
                return False
            empty = not self.items
            if self.items and now - self.items[0][1] > self.max_age:
                self.items.clear()
                self.failed = True
            elif (self.items and event.op == Op.MOVE and self.items[-1][0].op == Op.MOVE
                  and self.items[-1][0].epoch == event.epoch):
                old, age = self.items[-1]
                dx, dy = old.a + event.a, old.b + event.b
                if max(abs(dx), abs(dy)) > 1_000_000:
                    self.items.clear()
                    self.failed = True
                else:
                    # New sequence/stamp acknowledges the last constituent; age remains the oldest.
                    self.items[-1] = (event.with_fields(a=dx, b=dy), age)
            elif len(self.items) >= self.limit:
                self.items.clear()
                self.failed = True
            else:
                self.items.append((event, now))
            ok = not self.failed
        if empty or not ok:
            self.wake()
        return ok

    def pop(self):
        with self.lock:
            if self.failed:
                raise Overload("Input queue overloaded; release all input and reactivate.")
            if not self.items:
                return None
            event, entered = self.items.popleft()
            age = self.clock() - entered
            if age > self.max_age:
                self.items.clear()
                self.failed = True
                raise Overload("Input queue exceeded its age limit.")
            return event, age

    def clear(self):
        with self.lock:
            self.items.clear()
            self.failed = False

