import asyncio

from remoteinput.capture import CapturePolicy
from remoteinput.controller import Controller
from remoteinput.protocol import Op
from remoteinput.relay import Relay
from remoteinput.target import Target

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


async def test_real_client_lifecycle_and_password_change(tmp_path, monkeypatch):
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
        client.stop_control()
        await eventually(lambda: not target.engine.keys and not target.engine.buttons)
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
        listener.close()
        await listener.wait_closed()

