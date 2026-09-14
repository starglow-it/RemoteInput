import asyncio
import contextlib
import secrets
import ssl
import time

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from remoteinput.config import client_options
from remoteinput.protocol import ACK, Event, Op, control, parse_control, unpack
from remoteinput.registry import RateLimit
from remoteinput.relay import Relay
from remoteinput.safety import TargetEngine
from remoteinput.transport import Outbox

from helpers import FakeInjector, eventually, tls_contexts


@pytest.fixture
async def server(tmp_path):
    server_ssl, client_ssl = tls_contexts(tmp_path)
    relay = Relay(tmp_path / "registry.sqlite3")
    listener = await relay.start(port=0, ssl_context=server_ssl)
    url = f"wss://127.0.0.1:{listener.sockets[0].getsockname()[1]}/ws"
    try:
        yield relay, url, client_ssl
    finally:
        listener.close()
        await listener.wait_closed()


async def connection(url, context, kind, **fields):
    ws = await connect(url, **client_options(context))
    await ws.send(control(kind, **fields))
    reply = parse_control(await asyncio.wait_for(ws.recv(), 3))
    return ws, reply


async def read_until(ws, condition):
    async with asyncio.timeout(3):
        while True:
            raw = await ws.recv()
            value = parse_control(raw) if isinstance(raw, str) else raw
            if condition(value):
                return value


class LiveTarget:
    async def start(self, url, context):
        self.owner, self.password = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.ws, reply = await connection(url, context, "target", owner_secret=self.owner, password=self.password)
        self.name = reply["id"]
        self.sink = FakeInjector()
        self.outbox = Outbox(asyncio.get_running_loop())
        self.engine = TargetEngine(self.sink, self.outbox.put)
        self.engine.set_connected(True)
        self.engine.start()
        self.messages = asyncio.Queue()

        async def reader():
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    self.engine.receive(unpack(raw))
                else:
                    msg = parse_control(raw)
                    if msg["type"] in ("peer_joined", "peer_gone"):
                        self.engine.reset()
                    else:
                        self.messages.put_nowait(msg)
        self.tasks = [asyncio.create_task(reader()), asyncio.create_task(self.outbox.writer(self.ws))]
        return self

    async def close(self):
        self.engine.set_connected(False)
        self.engine.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.ws.close()


@pytest.fixture
async def live_target(server):
    _, url, context = server
    target = await LiveTarget().start(url, context)
    try:
        yield target
    finally:
        await target.close()


async def activate(ws, epoch=123):
    message = await read_until(ws, lambda obj: isinstance(obj, dict) and obj["type"] == "challenge")
    nonce = message["nonce"]
    await ws.send(Event(Op.ACTIVATE, epoch=epoch, seq=1, lease=nonce).pack())
    await read_until(ws, lambda obj: isinstance(obj, dict) and obj["type"] == "activated")
    return nonce


async def test_verified_wss_full_input_path_and_target_ack(server, live_target):
    _, url, context = server
    target = live_target
    ws, reply = await connection(url, context, "controller", id=target.name, password=target.password)
    try:
        assert reply["type"] == "connected"
        assert ws.protocol.extensions == []
        nonce = await activate(ws)
        events = [(Op.KEY, 65, 1), (Op.KEY, 65, 0), (Op.BUTTON, 1, 1),
                  (Op.MOVE, 80, -40), (Op.BUTTON, 1, 0), (Op.SCROLL, 0, 120)]
        for seq, (op, a, b) in enumerate(events, 2):
            await ws.send(Event(op, epoch=123, seq=seq, lease=nonce, a=a, b=b).pack())
        stamp = time.perf_counter_ns()
        await ws.send(Event(Op.PROBE, epoch=123, seq=50, stamp=stamp, lease=nonce).pack())
        raw = await read_until(ws, lambda obj: isinstance(obj, bytes))
        _, op, epoch, seq, echoed, queue_us, processing_us = ACK.unpack(raw)
        assert op == Op.PROBE and epoch == 123 and seq == 50 and echoed == stamp
        assert time.perf_counter_ns() > echoed and queue_us >= 0 and processing_us >= 0
        assert target.sink.events == [("key", 65, True), ("key", 65, False), ("button", 1, True),
                                      ("move", 80, -40), ("button", 1, False), ("scroll", 0, 120)]
    finally:
        await ws.close()


async def test_controller_freeze_releases_while_both_wss_connections_remain_open(server, live_target):
    _, url, context = server
    target = live_target
    ws, _ = await connection(url, context, "controller", id=target.name, password=target.password)
    try:
        nonce = await activate(ws)
        await ws.send(Event(Op.KEY, epoch=123, seq=2, lease=nonce, a=65, b=1).pack())
        await ws.send(Event(Op.BUTTON, epoch=123, seq=3, lease=nonce, a=1, b=1).pack())
        await eventually(lambda: bool(target.engine.keys))
        await eventually(lambda: not target.engine.keys and not target.engine.buttons, seconds=2)
        assert target.engine.connected and not target.engine.epoch
        assert ws.close_code is None and target.ws.close_code is None
        assert ("key", 65, False) in target.sink.events
        assert ("button", 1, False) in target.sink.events
    finally:
        await ws.close()


async def test_disconnect_cleanup_and_reconnect_paused(server, live_target):
    relay, url, context = server
    target = live_target
    ws, _ = await connection(url, context, "controller", id=target.name, password=target.password)
    nonce = await activate(ws)
    await ws.send(Event(Op.KEY, epoch=123, seq=2, lease=nonce, a=65, b=1).pack())
    await eventually(lambda: bool(target.engine.keys))
    await ws.close()
    await eventually(lambda: not target.engine.keys and target.name not in relay.controllers)
    restored, reply = await connection(url, context, "controller", id=target.name, password=target.password)
    try:
        assert reply["type"] == "connected"
        assert target.engine.epoch == 0
    finally:
        await restored.close()


async def test_only_one_controller_and_target_identity_authentication(server, live_target):
    _, url, context = server
    target = live_target
    first, _ = await connection(url, context, "controller", id=target.name, password=target.password)
    second, reply = await connection(url, context, "controller", id=target.name, password=target.password)
    try:
        assert reply["reason"] == "target_busy"
        impostor, reply = await connection(url, context, "target", id=target.name,
                                           owner_secret=secrets.token_urlsafe(32))
        assert reply["reason"] == "authentication_failed"
        await impostor.close()
    finally:
        await first.close()
        await second.close()


async def test_rotation_revokes_live_controller_and_old_password(server, live_target):
    relay, url, context = server
    target = live_target
    ws, _ = await connection(url, context, "controller", id=target.name, password=target.password)
    nonce = await activate(ws)
    await ws.send(Event(Op.BUTTON, epoch=123, seq=2, lease=nonce, a=1, b=1).pack())
    await eventually(lambda: bool(target.engine.buttons))
    new_password = secrets.token_urlsafe(24)
    target.engine.reset()
    target.outbox.put(control("rotate", password=new_password, rotation_id="r" * 32))
    response = await asyncio.wait_for(target.messages.get(), 3)
    assert response["type"] == "rotated"
    await asyncio.wait_for(ws.wait_closed(), 3)
    assert target.name not in relay.controllers and not target.engine.buttons
    old, reply = await connection(url, context, "controller", id=target.name, password=target.password)
    assert reply["reason"] == "authentication_failed"
    await old.close()
    new, reply = await connection(url, context, "controller", id=target.name, password=new_password)
    assert reply["type"] == "connected" and target.engine.epoch == 0
    await new.close()


async def test_rate_limit_applies_before_expensive_password_checks(server, live_target):
    relay, url, context = server
    target = live_target
    relay.auth_limit = RateLimit(2, 60)
    for i in range(3):
        ws, reply = await connection(url, context, "controller", id=target.name, password="wrong")
        assert reply["reason"] == ("authentication_failed" if i < 2 else "rate_limited")
        await ws.close()


async def test_untrusted_certificate_is_rejected(server):
    _, url, _ = server
    with pytest.raises(ssl.SSLCertVerificationError):
        async with connect(url, **client_options()):
            pass


async def test_permission_denial_prevents_activation(server, live_target):
    _, url, context = server
    target = live_target
    target.sink.permission = False
    ws, _ = await connection(url, context, "controller", id=target.name, password=target.password)
    try:
        challenge = await read_until(ws, lambda obj: isinstance(obj, dict) and obj["type"] == "challenge")
        await ws.send(Event(Op.ACTIVATE, epoch=123, seq=1, lease=challenge["nonce"]).pack())
        reply = await read_until(ws, lambda obj: isinstance(obj, dict) and obj["type"] == "paused")
        assert reply["epoch"] == 123 and target.sink.events == []
    finally:
        await ws.close()


async def test_malformed_controller_frame_closes_session_and_releases_holds(server, live_target):
    _, url, context = server
    target = live_target
    ws, _ = await connection(url, context, "controller", id=target.name, password=target.password)
    nonce = await activate(ws)
    await ws.send(Event(Op.KEY, epoch=123, seq=2, lease=nonce, a=65, b=1).pack())
    await eventually(lambda: bool(target.engine.keys))
    await ws.send(b"not-an-input-frame")
    with contextlib.suppress(ConnectionClosed):
        await read_until(ws, lambda _: False)
    await eventually(lambda: not target.engine.keys)
    assert not target.engine.epoch

