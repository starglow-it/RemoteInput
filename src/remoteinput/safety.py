"""The target's lease clock and injector are independent of websocket keepalives."""
import secrets
import threading
import time

from .config import CHALLENGE_INTERVAL, MAX_QUEUE_AGE, NETWORK_TIMEOUT_MAX
from .protocol import Op, ack, control
from .queueing import InputQueue
from .timing import NetworkTiming


class TargetEngine:
    def __init__(self, injector, emit, status=lambda _: None, clock=time.monotonic):
        self.injector, self.emit, self.status, self.clock = injector, emit, status, clock
        self.lock = threading.RLock()
        self.signal = threading.Event()
        self.queue = InputQueue(self.signal.set, clock=clock)
        self.leases = {}
        self.last_challenge = -1e9
        self.last_heartbeat = 0
        self.last_echo_issued = -1e9
        self.network = NetworkTiming()
        self.epoch = 0
        self.last_epoch = 0
        self.seq = 0
        self.keys, self.buttons = set(), set()
        self.blocked = False
        self.connected = False
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="target-input", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.signal.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.reset("Disconnected")

    def reset(self, reason="Paused"):
        with self.lock:
            old = self.epoch
            self.epoch = 0
            self.queue.clear()
            # Attempt every release even if one OS call fails. Retry failures on subsequent ticks.
            for key in list(self.keys):
                try:
                    self.injector.key(key, False)
                    self.keys.discard(key)
                except Exception:
                    self.blocked = True
            for button in list(self.buttons):
                try:
                    self.injector.button(button, False)
                    self.buttons.discard(button)
                except Exception:
                    self.blocked = True
            if old:
                self.emit(control("paused", epoch=old))
                self.status(reason)

    def set_connected(self, value):
        with self.lock:
            self.connected = value
            self.leases.clear()
            self.last_challenge = -1e9
            self.last_echo_issued = -1e9
            self.network = NetworkTiming()
            self.reset("Paused" if value else "Disconnected")

    def peer_changed(self):
        with self.lock:
            self.reset()
            self.network = NetworkTiming()
            self.last_echo_issued = -1e9
            self.last_challenge = -1e9
            # Keep still-valid issued challenges: one may already be in flight
            # when the relay admits a controller. Their deadlines still apply.

    def receive(self, event):
        if event.op == Op.RESET:
            self.reset()
            return
        if not self.queue.put(event):
            self.reset("Paused: input overload")

    def valid_lease(self, nonce, now, limit):
        return nonce in self.leases and 0 <= now - self.leases[nonce] < limit

    def heartbeat_expired(self, now):
        gap, limit = now - self.last_heartbeat, self.network.timeout
        if self.epoch and gap >= limit:
            self.reset(f"Paused: controller heartbeat timed out (gap {gap * 1000:.0f} ms; "
                       f"limit {limit * 1000:.0f} ms)")
            return True
        return False

    def observe_heartbeat(self, event, now):
        issued = self.leases[event.lease]
        # Repeating a nonce with a new sequence must not extend a hold. RTT
        # estimates also use each challenge only once, in increasing order.
        if issued <= self.last_echo_issued:
            return
        self.last_echo_issued = issued
        self.network.observe(now - issued)
        if self.epoch:
            self.last_heartbeat = now

    def reject_stale(self, event, now, limit):
        issued = self.leases.get(event.lease)
        age = "expired or unknown" if issued is None else f"age {(now - issued) * 1000:.0f} ms"
        kind = "heartbeat" if event.op == Op.HEARTBEAT else "input"
        reason = f"Paused: stale {kind} (token {age}; limit {limit * 1000:.0f} ms)"
        active = self.epoch
        self.reset(reason)
        if event.op == Op.ACTIVATE:
            self.emit(control("paused", epoch=event.epoch))
            if not active:
                self.status(reason)

    def tick(self):
        with self.lock:
            now = self.clock()
            # Retain a small bounded history for numeric failure diagnostics;
            # valid_lease still enforces the shorter per-operation deadline.
            self.leases = {n: t for n, t in self.leases.items()
                           if now - t < NETWORK_TIMEOUT_MAX + CHALLENGE_INTERVAL + MAX_QUEUE_AGE + 1}
            if self.connected and now - self.last_challenge >= CHALLENGE_INTERVAL:
                nonce = secrets.randbits(64) or 1
                self.leases[nonce] = now
                self.last_challenge = now
                self.emit(control("challenge", nonce=nonce))
            self.heartbeat_expired(now)
            if self.epoch and not self.injector.permitted():
                self.reset("Paused: input permission lost")
            if not self.epoch and (self.keys or self.buttons):
                self.reset("Paused: retrying input release")

    def process_one(self):
        try:
            item = self.queue.pop()
            if item is None:
                return False
            event, age = item
            with self.lock:
                now = self.clock()
                if self.blocked or not self.connected:
                    return True
                # A late paused-mode probe or an old/duplicate frame cannot
                # invalidate a different activation. No input is injected here.
                if event.op != Op.ACTIVATE:
                    if event.op in (Op.HEARTBEAT, Op.PROBE) and event.epoch == 0:
                        if self.epoch:
                            return True
                    elif event.epoch != self.epoch or not self.epoch or event.seq <= self.seq:
                        return True
                limit = self.network.timeout if event.op == Op.HEARTBEAT else self.network.input_timeout
                if event.op == Op.HEARTBEAT and not self.epoch:
                    # Calibrate while paused, within the absolute bound. This
                    # cannot activate control or keep any remote holds alive.
                    limit = NETWORK_TIMEOUT_MAX
                if not self.valid_lease(event.lease, now, limit):
                    self.reject_stale(event, now, limit)
                    return True
                if event.op == Op.ACTIVATE:
                    if (not event.epoch or event.epoch == self.last_epoch
                            or not self.injector.permitted()):
                        self.emit(control("paused", epoch=event.epoch))
                        return True
                    self.reset()
                    self.epoch = self.last_epoch = event.epoch
                    self.seq = event.seq
                    self.last_heartbeat = now
                    self.emit(control("activated", epoch=self.epoch))
                    self.status("Controlling")
                    return True
                if event.op == Op.PROBE and not self.epoch:
                    self.emit(ack(event, age * 1e6, 0))
                    return True
                if event.op == Op.HEARTBEAT and not self.epoch:
                    self.observe_heartbeat(event, now)
                    return True
                if event.epoch != self.epoch or not self.epoch or event.seq <= self.seq:
                    return True
                self.seq = event.seq
                if self.heartbeat_expired(now):
                    return True
                if event.op == Op.HEARTBEAT:
                    self.observe_heartbeat(event, now)
                    return True
                start = time.perf_counter_ns()
                if event.op == Op.KEY:
                    if hasattr(self.injector, "supports_key") and not self.injector.supports_key(event.a):
                        self.reset("Paused: this key is not supported on the target")
                        return True
                    if event.b:
                        self.keys.add(event.a)
                        self.injector.key(event.a, True)
                    elif event.a in self.keys:
                        self.injector.key(event.a, False)
                        self.keys.discard(event.a)
                elif event.op == Op.BUTTON:
                    if event.b:
                        self.buttons.add(event.a)
                        self.injector.button(event.a, True)
                    elif event.a in self.buttons:
                        self.injector.button(event.a, False)
                        self.buttons.discard(event.a)
                elif event.op == Op.MOVE:
                    self.injector.move(event.a, event.b)
                elif event.op == Op.SCROLL:
                    self.injector.scroll(event.a, event.b)
                duration_us = (time.perf_counter_ns() - start) / 1000
                if duration_us > 100_000:
                    self.reset("Paused: input injection stalled")
                if event.op == Op.PROBE or event.seq % 32 == 0:
                    self.emit(ack(event, age * 1e6, duration_us))
                return True
        except Exception:
            self.reset("Paused: input processing failed")
            return False

    def _run(self):
        try:
            while not self.stop_event.is_set():
                self.tick()
                # Check leases between every event, even during a continuous input stream.
                if not self.process_one():
                    self.signal.wait(.010)
                    self.signal.clear()
        finally:
            self.reset("Disconnected")
