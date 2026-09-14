"""Testable hotkey and suppression decisions, with no OS imports."""
import threading

from .protocol import Event, Op

CTRL = {0x11, 0xA2, 0xA3}
ALT = {0x12, 0xA4, 0xA5}
RESERVED_MODS = CTRL | ALT
F9, F10 = 0x78, 0x79


class CapturePolicy:
    def __init__(self, emit, toggle, stop):
        self.emit, self.toggle, self.stop = emit, toggle, stop
        self.lock = threading.RLock()
        self.active = False
        self.keys, self.buttons = set(), set()
        self.ignored, self.swallowed = set(), set()
        self.ignored_buttons = set()
        self.pending = {}  # insertion-ordered modifier downs, flushed before input
        self.forwarded = set()

    def activate(self):
        with self.lock:
            self.active = True
            self.ignored = set(self.keys)
            self.ignored_buttons = set(self.buttons)
            self.pending.clear()
            self.forwarded.clear()

    def pause(self):
        with self.lock:
            self.active = False
            self.swallowed.update(self.forwarded)
            self.pending.clear()
            self.forwarded.clear()

    def flush_modifiers(self):
        for key in list(self.pending):
            self.emit(Event(Op.KEY, a=key, b=1))
            self.forwarded.add(key)
        self.pending.clear()

    def key(self, key, down):
        with self.lock:
            repeated = key in self.keys
            if down:
                self.keys.add(key)
            else:
                self.keys.discard(key)
            if down and key in (F9, F10) and self.keys & CTRL and self.keys & ALT:
                self.swallowed.add(key)
                if not repeated:
                    self.pending.clear()
                    (self.toggle if key == F9 else self.stop)()
                return True
            if key in self.swallowed:
                if not down:
                    self.swallowed.discard(key)
                return True
            if key in self.ignored:
                if not down:
                    self.ignored.discard(key)
                return self.active
            if not self.active:
                return False
            if key in RESERVED_MODS:
                if down and key not in self.forwarded:
                    self.pending[key] = True
                elif not down:
                    # A standalone modifier tap still reaches the target (e.g. Alt menus).
                    if key in self.pending:
                        self.emit(Event(Op.KEY, a=key, b=1))
                        self.pending.pop(key)
                        self.forwarded.add(key)
                    if key in self.forwarded:
                        self.emit(Event(Op.KEY, a=key, b=0))
                        self.forwarded.discard(key)
            else:
                self.flush_modifiers()
                if down or key in self.forwarded:
                    self.emit(Event(Op.KEY, a=key, b=int(down)))
                if down:
                    self.forwarded.add(key)
                else:
                    self.forwarded.discard(key)
            return True

    def mouse(self, op, a, b):
        with self.lock:
            if op == Op.BUTTON:
                (self.buttons.add if b else self.buttons.discard)(a)
                if a in self.ignored_buttons:
                    if not b:
                        self.ignored_buttons.discard(a)
                    return self.active
            if not self.active:
                return False
            self.flush_modifiers()
            self.emit(Event(op, a=a, b=b))
            return True

