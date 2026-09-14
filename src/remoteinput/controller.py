import asyncio
import getpass
import random
import secrets
import time

from websockets.asyncio.client import connect

from .config import client_options
from .metrics import Metrics
from .platforms import controller_capture
from .protocol import ACK, ACK_CODE, Event, Op, control, parse_control
from .queueing import InputQueue, Overload
from .transport import Console, Outbox, run_tasks
from .vault import Vault


class Controller:
    def __init__(self, url, vault, identity, capture_factory=controller_capture):
        self.url, self.vault, self.identity = url, vault, identity
        self.loop = asyncio.get_running_loop()
        self.ready = asyncio.Event()
        self.queue = InputQueue(lambda: self.loop.call_soon_threadsafe(self.ready.set))
        self.capture = capture_factory(self.enqueue, self.toggle, self.stop_control)
        self.metrics = Metrics()
        self.connected = False
        self.running = True
        self.epoch = 0
        self.pending_epoch = 0
        self.seq = 0
        self.lease = 0
        self.lease_received = 0
        self.last_ack = 0
        self.activate_deadline = 0
        self.outbox = None

    def enqueue(self, event):
        if not self.connected or not self.epoch:
            return
        if not self.queue.put(event.with_fields(epoch=self.epoch)):
            self.stop_control("controller input queue overloaded")

    def _schedule(self, function):
        self.loop.call_soon_threadsafe(function)

    def toggle(self):
        if self.epoch or self.pending_epoch:
            self.stop_control("Ctrl+Alt+F9")
        else:
            self._schedule(self.activate)

    def stop_control(self, reason="control stopped locally"):
        # Stop local suppression immediately, including calls from the hook thread.
        self.capture.set_active(False)
        self.epoch = self.pending_epoch = 0
        self.queue.clear()
        self._schedule(lambda: self.send_reset(reason))

    def send_reset(self, reason="activation cancelled"):
        if self.outbox:
            self.outbox.put(self.frame(Event(Op.RESET)).pack())
        print(f"Paused: {reason}" if self.connected else "Disconnected", flush=True)

    def frame(self, event):
        self.seq += 1
        return event.with_fields(seq=self.seq, stamp=time.perf_counter_ns(), lease=self.lease)

    def activate(self):
        if self.epoch or self.pending_epoch:
            return
        if not self.connected or not self.lease or time.monotonic() - self.lease_received > .5:
            print("Paused: waiting for a fresh target connection. Press Ctrl+Alt+F9 again when connected.")
            return
        self.pending_epoch = secrets.randbits(64) or 1
        self.activate_deadline = time.monotonic() + 2
        self.queue.clear()
        self.outbox.put(self.frame(Event(Op.ACTIVATE, epoch=self.pending_epoch)).pack())

    async def sender(self, ws):
        while True:
            self.ready.clear()
            try:
                while True:
                    item = self.queue.pop()
                    if item is None:
                        break
                    event, age = item
                    if not self.epoch or event.epoch != self.epoch:
                        continue
                    self.metrics.add("controller_queue", age * 1000)
                    # Single outbox preserves order of inputs, heartbeats, probes, and resets.
                    self.outbox.put(self.frame(event).pack())
            except Overload:
                self.stop_control("controller input queue exceeded the safety limit")
            await self.ready.wait()

    async def reader(self, ws):
        async for raw in ws:
            if isinstance(raw, bytes):
                if len(raw) != ACK.size or raw[0] != ACK_CODE:
                    raise ValueError("Invalid acknowledgement")
                _, op, epoch, seq, stamp, queue_us, injection_us = ACK.unpack(raw)
                if epoch in (0, self.epoch):
                    elapsed = (time.perf_counter_ns() - stamp) / 1e6
                    if 0 <= elapsed < 10000 and 0 < seq <= self.seq:
                        self.last_ack = time.monotonic()
                        self.metrics.add("controller_target_controller_rtt", elapsed)
                        self.metrics.add("target_queue", queue_us / 1000)
                        if op in (Op.MOVE, Op.KEY, Op.BUTTON, Op.SCROLL):
                            self.metrics.add("target_injection", injection_us / 1000)
            else:
                message = parse_control(raw)
                kind = message["type"]
                if kind == "challenge":
                    self.lease = message["nonce"]
                    self.lease_received = time.monotonic()
                    epoch = self.epoch or self.pending_epoch
                    if epoch and self.capture.healthy():
                        # ACTIVATE is already ahead of this heartbeat in the
                        # same outbox. Don't spend a second network trip waiting
                        # for its reply before proving that capture is responsive.
                        self.outbox.put(self.frame(Event(Op.HEARTBEAT, epoch=epoch)).pack())
                elif kind == "activated":
                    if message["epoch"] != self.pending_epoch or not self.pending_epoch:
                        self.send_reset()
                        continue
                    self.epoch, self.pending_epoch = self.pending_epoch, 0
                    self.capture.set_active(True)
                    print("Controlling", flush=True)
                elif kind == "paused":
                    if message.get("epoch") in (self.epoch, self.pending_epoch):
                        self.stop_control("target stopped control; see the target console for the reason")
                else:
                    raise ValueError("Unexpected relay message")

    async def monitor(self, ws):
        last_probe = 0
        while True:
            now = time.monotonic()
            self.capture.network_tick()
            if self.epoch and not self.capture.healthy():
                self.stop_control("Windows input capture stopped responding")
            elif self.epoch and now - self.lease_received > .65:
                self.stop_control("target updates timed out; check the network and target console")
            elif self.pending_epoch and now > self.activate_deadline:
                self.stop_control("target activation timed out")
            if self.lease and now - last_probe >= .5:
                self.outbox.put(self.frame(Event(Op.PROBE, epoch=self.epoch)).pack())
                last_probe = now
            await asyncio.sleep(.05)

    async def link_measurements(self, ws):
        while True:
            start = time.perf_counter()
            pong = await ws.ping()
            await asyncio.wait_for(pong, 2)
            self.metrics.add("controller_relay_link_rtt", (time.perf_counter() - start) * 1000)
            await asyncio.sleep(10)

    async def run(self, ssl_context=None):
        console = Console()
        self.capture.start()
        print("Ctrl+Alt+F9: toggle control. Ctrl+Alt+F10: stop control.")
        print("While paused: M + Enter: measurements; F + Enter: Forget Target; Q + Enter: quit.")
        attempt = 0
        try:
            while self.running:
                retry = True
                try:
                    async with connect(self.url, **client_options(ssl_context)) as ws:
                        await ws.send(control("controller", **self.identity))
                        reply = parse_control(await asyncio.wait_for(ws.recv(), 10))
                        if reply["type"] != "connected":
                            reason = reply.get("reason", "connection_refused")
                            print(f"Disconnected: {reason}", flush=True)
                            if reason == "authentication_failed":
                                self.vault.delete()
                                print("Saved access was rejected and forgotten. Start Controller again to pair.")
                                retry = False
                                return
                            raise ConnectionError()
                        self.vault.save(self.identity)
                        self.connected = True
                        self.outbox = Outbox(self.loop, metrics=self.metrics)
                        attempt = 0
                        print("Connected / Paused", flush=True)

                        async def commands():
                            while self.running:
                                command = await console.commands.get()
                                if command in ("m", "measure"):
                                    self.metrics.display()
                                    print("RTT is a full return trip; it excludes your separate screen feed.")
                                elif command in ("f", "forget target"):
                                    self.stop_control()
                                    self.vault.delete()
                                    self.running = False
                                    print("Target forgotten. Start Controller again to pair.")
                                    return
                                elif command in ("q", "quit"):
                                    self.stop_control()
                                    self.running = False
                                    return

                        await run_tasks(self.reader(ws), self.sender(ws), self.outbox.writer(ws),
                                        self.monitor(ws), self.link_measurements(ws), commands())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    print("Disconnected. Reconnecting while paused.", flush=True)
                finally:
                    self.capture.set_active(False)
                    self.connected = False
                    self.epoch = self.pending_epoch = self.lease = 0
                    self.outbox = None
                    self.queue.clear()
                if self.running and retry:
                    delay = min(2 ** min(attempt, 5), 30) + random.random()
                    attempt += 1
                    try:
                        command = await asyncio.wait_for(console.commands.get(), delay)
                        if command in ("f", "forget target"):
                            self.vault.delete()
                            self.running = False
                        elif command in ("q", "quit"):
                            self.running = False
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.capture.stop()


async def run_controller(url):
    vault = Vault(url, "controller")
    identity = vault.load()
    if not identity:
        name = input("Target ID: ").strip().upper()
        password = getpass.getpass("Target password: ")
        identity = {"id": name, "password": password}
    await Controller(url, vault, identity).run()
