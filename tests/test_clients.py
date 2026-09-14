import asyncio

import pytest

from remoteinput.capture import CapturePolicy
from remoteinput.controller import Controller
from remoteinput.protocol import Op
from remoteinput.relay import Relay
from remoteinput.target import Target
from remoteinput.transport import Outbox

from helpers import FakeInjector, MemoryVault, eventually, tls_contexts


class FakeCapture:
    def __init__(self, emit, toggle, stop):
        self.policy = CapturePolicy(emit, toggle, stop)
        self.alive = True

    def set_active(self, active):
        (self.policy.activate if active else self.policy.pause)()

    def start(self):
        pass

    def stop(self):
        self.policy.pause()

    def healthy(self):
        return self.alive

    def network_tick(self):
        pass


class FakeConsole:
    def __init__(self):
        self.commands = asyncio.Queue()


@pytest.mark.parametrize("propagation_delay", [0, .350, .450])
async def test_real_client_lifecycle_and_password_change(tmp_path, monkeypatch, capsys, propagation_delay):
    delay_tasks = []
    stall_until = 0
    drop_heartbeats = False
    if propagation_delay:
        put = Outbox.put
        channels = {}

        async def propagate(outbox, channel):
            while True:
                raw, due = await channel.get()
                while max(due, stall_until) > outbox.loop.time():
                    await asyncio.sleep(max(due, stall_until) - outbox.loop.time())
                put(outbox, raw)

        def schedule(outbox, raw):
            if outbox not in channels:
                channels[outbox] = asyncio.Queue(256)
                delay_tasks.append(asyncio.create_task(propagate(outbox, channels[outbox])))
            channels[outbox].put_nowait((raw, outbox.loop.time() + propagation_delay))

        def delayed_put(outbox, raw):
            if drop_heartbeats and isinstance(raw, bytes) and raw[0] == Op.HEARTBEAT:
                return
            # Due times model propagation without charging another delay for
            # every packet. FIFO also preserves WSS ordering when coarse Windows
            # clock ticks give several packets identical deadlines (independent
            # call_later timers explicitly don't guarantee their relative order).
            outbox.loop.call_soon_threadsafe(schedule, outbox, raw)

        monkeypatch.setattr(Outbox, "put", delayed_put)
    server_ssl, client_ssl = tls_contexts(tmp_path)
    relay = Relay(tmp_path / "registry.sqlite3")
    listener = await relay.start(port=0, ssl_context=server_ssl)
    url = f"wss://127.0.0.1:{listener.sockets[0].getsockname()[1]}/ws"
    target_console, controller_console = FakeConsole(), FakeConsole()
    monkeypatch.setattr("remoteinput.target.Console", lambda: target_console)
    monkeypatch.setattr("remoteinput.controller.Console", lambda: controller_console)
    target_vault, controller_vault = MemoryVault(), MemoryVault()
    sink = FakeInjector()
    target = Target(url, target_vault, sink)
    target_task = asyncio.create_task(target.run(client_ssl))
    tasks = [target_task]
    try:
        await eventually(lambda: bool(target_vault.value.get("id")))
        identity = target_vault.load()
        client = Controller(url, controller_vault, {"id": identity["id"], "password": identity["password"]},
                            capture_factory=FakeCapture)
        client_task = asyncio.create_task(client.run(client_ssl))
        tasks.append(client_task)
        await eventually(lambda: client.connected and bool(client.lease))
        assert not client.capture.policy.active and not target.engine.epoch
        client.activate()
        await eventually(lambda: client.capture.policy.active)
        client.capture.policy.key(65, True)
        client.capture.policy.mouse(Op.BUTTON, 1, 1)
        client.capture.policy.mouse(Op.MOVE, 6, -2)
        await eventually(lambda: target.engine.keys and target.engine.buttons)
        if propagation_delay:
            # Sustain a drag on 700/900 ms RTT routes. A short wire stall also
            # creates an update gap beyond the old 650 ms controller cutoff.
            started = asyncio.get_running_loop().time()
            deadline = started + 10
            stalled = False
            while asyncio.get_running_loop().time() < deadline:
                assert client.epoch and target.engine.epoch
                if propagation_delay == .450 and not stalled and asyncio.get_running_loop().time() - started > 2:
                    stall_until = asyncio.get_running_loop().time() + .750
                    stalled = True
                client.capture.policy.mouse(Op.MOVE, 1, -1)
                await asyncio.sleep(.03)
            if propagation_delay == .450:
                assert stalled
                # Relay pings, challenges, probes and input still flow. Only
                # application heartbeats stop; the target must release holds.
                drop_heartbeats = True
                await eventually(lambda: not target.engine.epoch, seconds=5)
                await eventually(lambda: not client.capture.policy.active)
                assert not target.engine.keys and not target.engine.buttons
                assert client.connected and target.engine.connected
                assert "controller heartbeat timed out" in capsys.readouterr().out
                drop_heartbeats = False
                client.capture.policy.key(65, False)
                client.capture.policy.mouse(Op.BUTTON, 1, 0)
                client.activate()
                await eventually(lambda: client.capture.policy.active)
                client.capture.policy.key(65, True)
                client.capture.policy.mouse(Op.BUTTON, 1, 1)
                await eventually(lambda: target.engine.keys and target.engine.buttons)
        client.capture.alive = False
        await eventually(lambda: not client.capture.policy.active)
        await eventually(lambda: not target.engine.keys and not target.engine.buttons)
        assert client.connected and target.engine.connected
        assert "Paused: Windows input capture stopped responding" in capsys.readouterr().out
        client.capture.alive = True
        client.capture.policy.key(65, False)
        client.capture.policy.mouse(Op.BUTTON, 1, 0)
        client.activate()
        await eventually(lambda: client.capture.policy.active)
        await target_console.commands.put("p")
        await eventually(lambda: target_vault.value["password"] != identity["password"])
        await eventually(lambda: client_task.done(), seconds=5)
        assert not target.engine.epoch and not client.capture.policy.active
        assert controller_vault.load() is None
        assert target_vault.value["id"] == identity["id"]
        assert "pending_password" not in target_vault.value
        client_task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in delay_tasks:
            task.cancel()
        await asyncio.gather(*delay_tasks, return_exceptions=True)
        listener.close()
        await listener.wait_closed()
