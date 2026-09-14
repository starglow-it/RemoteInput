import asyncio
import contextlib
import random

from websockets.asyncio.client import connect

from .config import client_options
from .platforms import target_injector
from .protocol import control, parse_control, unpack
from .safety import TargetEngine
from .transport import Console, Outbox, run_tasks
from .vault import Vault, begin_rotation, finish_rotation, target_identity


class Target:
    def __init__(self, url, vault, injector):
        self.url, self.vault, self.injector = url, vault, injector
        self.identity = target_identity(vault)
        self.running = True
        self.rotating = "pending_password" in self.identity
        self.outbox = None
        self.engine = TargetEngine(injector, self.emit, lambda message: print(message, flush=True))

    def emit(self, raw):
        if self.outbox:
            self.outbox.put(raw)

    def display_credentials(self):
        print(f"Target ID: {self.identity['id']}\nPassword: {self.identity['password']}", flush=True)
        print("Share these only with someone you authorize to control this computer.", flush=True)

    def rotate(self):
        self.engine.reset("Paused: changing password")
        self.rotating = True
        self.identity = begin_rotation(self.vault, self.identity)
        self.emit(control("rotate", password=self.identity["pending_password"],
                          rotation_id=self.identity["rotation_id"]))

    async def run(self, ssl_context=None):
        console = Console()
        self.engine.start()
        try:
            attempt = 0
            while self.running:
                try:
                    async with connect(self.url, **client_options(ssl_context)) as ws:
                        hello = {"owner_secret": self.identity["owner_secret"]}
                        if self.identity.get("id"):
                            hello["id"] = self.identity["id"]
                        else:
                            hello["password"] = self.identity["password"]
                        await ws.send(control("target", **hello))
                        reply = parse_control(await asyncio.wait_for(ws.recv(), 10))
                        if reply["type"] != "registered":
                            reason = reply.get("reason", "connection_refused")
                            print(f"Disconnected: {reason}", flush=True)
                            if reason in ("authentication_failed", "target_already_running"):
                                return
                            raise ConnectionError()
                        self.identity["id"] = reply["id"]
                        self.vault.save(self.identity)
                        self.outbox = Outbox(asyncio.get_running_loop())
                        self.engine.set_connected(True)
                        attempt = 0
                        if self.rotating:
                            self.rotate()
                        else:
                            self.display_credentials()
                        print("Connected / Paused. P + Enter: Change Password; Q + Enter: quit.", flush=True)

                        async def reader():
                            async for raw in ws:
                                if isinstance(raw, bytes):
                                    if not self.rotating:
                                        self.engine.receive(unpack(raw))
                                else:
                                    message = parse_control(raw)
                                    if message["type"] in ("peer_joined", "peer_gone"):
                                        self.engine.reset()
                                    elif message["type"] == "rotated" and self.rotating:
                                        self.identity = finish_rotation(self.vault, self.identity)
                                        self.rotating = False
                                        self.display_credentials()
                                        print("Password changed. Previous access revoked; Paused.", flush=True)
                                    else:
                                        raise ValueError("Unexpected relay message")

                        async def commands():
                            while self.running:
                                command = await console.commands.get()
                                if command in ("p", "change password"):
                                    self.rotate()
                                elif command in ("q", "quit"):
                                    self.running = False
                                    return

                        await run_tasks(reader(), self.outbox.writer(ws), commands())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    print("Disconnected. Check internet/relay availability; retrying while paused.", flush=True)
                finally:
                    self.engine.set_connected(False)
                    self.outbox = None
                if self.running:
                    # Q remains available during a network outage.
                    delay = min(2 ** min(attempt, 5), 30) + random.random()
                    attempt += 1
                    with contextlib.suppress(asyncio.TimeoutError):
                        command = await asyncio.wait_for(console.commands.get(), delay)
                        if command in ("q", "quit"):
                            self.running = False
                        elif command in ("p", "change password"):
                            self.identity = begin_rotation(self.vault, self.identity)
                            self.rotating = True
                            print("Password change saved securely; it will revoke access when the relay reconnects.")
        finally:
            self.engine.stop()


async def run_target(url):
    injector = target_injector()
    injector.ensure_permissions()
    await Target(url, Vault(url, "target"), injector).run()

