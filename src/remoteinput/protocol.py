"""Bounded binary input protocol. Control messages are small JSON objects."""
import json
import struct
from dataclasses import dataclass, replace
from enum import IntEnum

from .config import MAX_FRAME


class Op(IntEnum):
    MOVE = 1
    KEY = 2
    BUTTON = 3
    SCROLL = 4
    RESET = 5
    ACTIVATE = 6
    HEARTBEAT = 7
    PROBE = 8


# opcode, activation epoch, sequence, controller send clock, target lease nonce, two arguments
FRAME = struct.Struct("!BQQQQii")
ACK = struct.Struct("!BBQQQII")
ACK_CODE = 128


@dataclass(frozen=True, slots=True)
class Event:
    op: Op
    epoch: int = 0
    seq: int = 0
    stamp: int = 0
    lease: int = 0
    a: int = 0
    b: int = 0

    def pack(self):
        return FRAME.pack(self.op, self.epoch, self.seq, self.stamp, self.lease, self.a, self.b)

    def with_fields(self, **kwargs):
        return replace(self, **kwargs)


def unpack(data):
    if not isinstance(data, bytes) or len(data) != FRAME.size:
        raise ValueError("Invalid input frame")
    raw = FRAME.unpack(data)
    e = Event(Op(raw[0]), *raw[1:])
    if e.op == Op.KEY and not (1 <= e.a <= 254 and e.b in (0, 1)):
        raise ValueError("Invalid key")
    if e.op == Op.BUTTON and not (1 <= e.a <= 5 and e.b in (0, 1)):
        raise ValueError("Invalid button")
    if e.op == Op.MOVE and max(abs(e.a), abs(e.b)) > 1_000_000:
        raise ValueError("Invalid movement")
    if e.op == Op.SCROLL and max(abs(e.a), abs(e.b)) > 12000:
        raise ValueError("Invalid scroll")
    if e.op in (Op.RESET, Op.ACTIVATE, Op.HEARTBEAT, Op.PROBE) and (e.a or e.b):
        raise ValueError("Invalid control frame")
    return e


def ack(event, queue_us, injection_us):
    return ACK.pack(ACK_CODE, event.op, event.epoch, event.seq, event.stamp,
                    min(int(queue_us), 2**32 - 1), min(int(injection_us), 2**32 - 1))


def control(kind, **fields):
    return json.dumps({"type": kind, **fields}, separators=(",", ":"))


def parse_control(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_FRAME:
        raise ValueError("Invalid control message")
    obj = json.loads(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ValueError("Invalid control message")
    return obj


def field(obj, name, minimum=1, maximum=128):
    value = obj.get(name)
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError("Invalid authentication message")
    return value
