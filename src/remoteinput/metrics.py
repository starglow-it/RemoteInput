import math
import threading
from collections import defaultdict, deque


class Metrics:
    """Numeric timings only. Never retains input values or authentication data."""

    def __init__(self, limit=4096):
        self.values = defaultdict(lambda: deque(maxlen=limit))
        self.lock = threading.Lock()

    def add(self, name, milliseconds):
        with self.lock:
            self.values[name].append(max(0.0, milliseconds))

    def snapshot(self):
        with self.lock:
            result = {}
            for name, data in self.values.items():
                if data:
                    ordered = sorted(data)
                    result[name] = {"samples": len(ordered),
                                    "median_ms": ordered[(len(ordered) - 1) // 2],
                                    "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1]}
            return result

    def display(self):
        for name, entry in self.snapshot().items():
            print(f"{name}: median {entry['median_ms']:.2f} ms; p95 {entry['p95_ms']:.2f} ms "
                  f"({entry['samples']} samples)", flush=True)

