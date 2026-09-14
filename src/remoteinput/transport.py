import asyncio
import threading
import time
from collections import deque

from .config import MAX_QUEUE_AGE, SEND_TIMEOUT
from .queueing import Overload


class Outbox:
    """Bounded thread-to-async bridge; at most one pending wakeup for a nonempty queue."""

    def __init__(self, loop, capacity=256, metrics=None):
        self.loop = loop
        self.ready = asyncio.Event()
        self.lock = threading.Lock()
        self.items = deque()
        self.capacity = capacity
        self.failed = False
        self.metrics = metrics

    def put(self, raw):
        with self.lock:
            empty = not self.items
            if len(self.items) >= self.capacity:
                self.items.clear()
                self.failed = True
            elif not self.failed:
                self.items.append((raw, time.monotonic()))
        if empty or self.failed:
            try:
                self.loop.call_soon_threadsafe(self.ready.set)
            except RuntimeError:
                pass

    async def writer(self, ws):
        while True:
            self.ready.clear()
            while True:
                with self.lock:
                    if self.failed:
                        raise Overload("Output overload")
                    item = self.items.popleft() if self.items else None
                if item is None:
                    break
                raw, entered = item
                age = time.monotonic() - entered
                if age > MAX_QUEUE_AGE:
                    raise Overload("Output expired")
                if self.metrics:
                    self.metrics.add("controller_send_queue", age * 1000)
                await asyncio.wait_for(ws.send(raw), SEND_TIMEOUT)
            await self.ready.wait()


async def run_tasks(*coroutines):
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Console:
    def __init__(self):
        self.commands = asyncio.Queue(8)
        self.loop = asyncio.get_running_loop()
        self.thread = threading.Thread(target=self._read, name="console-commands", daemon=True)
        self.thread.start()

    def _push(self, command):
        if not self.commands.full():
            self.commands.put_nowait(command)

    def _read(self):
        while True:
            try:
                command = input().strip().lower()
            except (EOFError, OSError):
                return
            try:
                self.loop.call_soon_threadsafe(self._push, command)
            except RuntimeError:
                return
