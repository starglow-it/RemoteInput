"""Single-process relay. No screens, command execution, or input persistence."""
import asyncio
import contextlib
import ssl
import time
from dataclasses import dataclass, field as dcfield

from websockets.asyncio.server import serve

from .config import MAX_FRAME, MAX_QUEUE_AGE, SEND_TIMEOUT, private_logging
from .protocol import ACK, ACK_CODE, control, field, parse_control, unpack
from .registry import RateLimit, Registry


@dataclass(eq=False)
class Peer:
    ws: object
    role: str
    name: str
    generation: int
    out: asyncio.Queue = dcfield(default_factory=lambda: asyncio.Queue(256))

    def put(self, message):
        self.out.put_nowait((message, time.monotonic()))

    async def writer(self):
        while True:
            message, entered = await self.out.get()
            if time.monotonic() - entered > MAX_QUEUE_AGE:
                raise TimeoutError("Relay queue expired")
            await asyncio.wait_for(self.ws.send(message), SEND_TIMEOUT)


class Relay:
    def __init__(self, database, trust_proxy=False):
        self.registry = Registry(database)
        self.targets = {}
        self.controllers = {}
        self.auth_limit = RateLimit(10, 60)
        self.id_limit = RateLimit(12, 60)
        self.connect_limit = RateLimit(60, 60)
        self.hash_slots = asyncio.Semaphore(4)
        self.pair_lock = asyncio.Lock()
        self.trust_proxy = trust_proxy
        self.connections = 0

    async def reject(self, ws, reason="authentication_failed"):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.send(control("error", reason=reason)), SEND_TIMEOUT)
        await ws.close(code=1008, reason=reason)

    async def hash_call(self, function, *args):
        async with self.hash_slots:
            return await asyncio.to_thread(function, *args)

    async def handler(self, ws):
        peer = None
        self.connections += 1
        try:
            address = ws.remote_address[0] if ws.remote_address else "unknown"
            if self.trust_proxy:
                # Only enable on an isolated reverse-proxy network; see deploy/compose.yaml.
                address = ws.request.headers.get("X-Real-IP", address)
            if ws.request.path != "/ws" or self.connections > 512 or not self.connect_limit.allow(address):
                return await self.reject(ws, "rate_limited")
            hello = parse_control(await asyncio.wait_for(ws.recv(), 8))
            role = hello["type"]
            if role == "diagnostic":
                # Public link timing only. No pairing, registry access, or input forwarding.
                await ws.send(control("diagnostic_ready"))
                await asyncio.wait_for(ws.wait_closed(), 60)
                return
            if not self.auth_limit.allow(address):
                return await self.reject(ws, "rate_limited")
            if role == "target":
                owner = field(hello, "owner_secret", 40, 64)
                if hello.get("id"):
                    name = field(hello, "id", 10, 32)
                    generation = await self.hash_call(self.registry.owner, name, owner)
                    if generation is None:
                        return await self.reject(ws)
                else:
                    password = field(hello, "password", 24, 128)
                    name, generation = await self.hash_call(self.registry.register, owner, password)
                async with self.pair_lock:
                    if name in self.targets:
                        return await self.reject(ws, "target_already_running")
                    peer = Peer(ws, "target", name, generation)
                    self.targets[name] = peer
                await ws.send(control("registered", id=name, generation=generation))
            elif role == "controller":
                name = field(hello, "id", 10, 32)
                password = field(hello, "password", 1, 128)
                if not self.id_limit.allow(name):
                    return await self.reject(ws, "rate_limited")
                generation = await self.hash_call(self.registry.login, name, password)
                if generation is None:
                    return await self.reject(ws)
                async with self.pair_lock:
                    # Rotation also holds this lock: no password-check / admission race.
                    current = await asyncio.to_thread(self.registry.generation, name)
                    target = self.targets.get(name)
                    if generation != current:
                        return await self.reject(ws)
                    if target is None:
                        return await self.reject(ws, "target_offline")
                    if name in self.controllers:
                        return await self.reject(ws, "target_busy")
                    peer = Peer(ws, "controller", name, generation)
                    self.controllers[name] = peer
                    target.put(control("peer_joined"))
                await ws.send(control("connected", id=name))
            else:
                return await self.reject(ws)

            async def reader():
                async for raw in ws:
                    if peer.role == "target":
                        await self.from_target(peer, raw, owner)
                    else:
                        # There is no arbitrary data / JSON forwarding channel.
                        unpack(raw)
                        target = self.targets.get(peer.name)
                        if target is None or self.controllers.get(peer.name) is not peer:
                            raise ValueError("Session revoked")
                        target.put(raw)

            tasks = [asyncio.create_task(reader()), asyncio.create_task(peer.writer())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            # Only constant messages: exception strings may contain client-supplied content.
            with contextlib.suppress(Exception):
                await ws.close(code=1011, reason="session_closed")
        finally:
            self.connections -= 1
            if peer:
                await self.detach(peer)

    async def from_target(self, peer, raw, owner):
        if isinstance(raw, bytes):
            if len(raw) != ACK.size or raw[0] != ACK_CODE:
                raise ValueError("Invalid acknowledgement")
            outbound = raw
        else:
            msg = parse_control(raw)
            kind = msg["type"]
            if kind == "rotate":
                password = field(msg, "password", 24, 128)
                rotation_id = field(msg, "rotation_id", 24, 64)
                async with self.pair_lock:
                    generation = await self.hash_call(self.registry.rotate, peer.name, owner, password, rotation_id)
                    peer.generation = generation
                    controller = self.controllers.pop(peer.name, None)
                    if controller:
                        # Revoke admission immediately; abort unsent/backlogged input at the socket.
                        controller.ws.transport.abort()
                    peer.put(control("rotated", generation=generation))
                return
            if kind == "challenge":
                nonce = msg.get("nonce")
                if type(nonce) is not int or not 0 < nonce < 2**64:
                    raise ValueError("Invalid lease")
                outbound = control("challenge", nonce=nonce)
            elif kind in ("activated", "paused"):
                epoch = msg.get("epoch", 0)
                if type(epoch) is not int or not 0 <= epoch < 2**64:
                    raise ValueError("Invalid epoch")
                outbound = control(kind, epoch=epoch)
            else:
                raise ValueError("Unsupported message")
        controller = self.controllers.get(peer.name)
        if controller:
            controller.put(outbound)

    async def detach(self, peer):
        if peer.role == "target" and self.targets.get(peer.name) is peer:
            self.targets.pop(peer.name, None)
            controller = self.controllers.pop(peer.name, None)
            if controller:
                controller.ws.transport.abort()
        elif peer.role == "controller" and self.controllers.get(peer.name) is peer:
            self.controllers.pop(peer.name, None)
            target = self.targets.get(peer.name)
            if target:
                try:
                    target.put(control("peer_gone"))
                except asyncio.QueueFull:
                    target.ws.transport.abort()

    async def start(self, host="127.0.0.1", port=8765, ssl_context=None):
        private_logging()
        return await serve(self.handler, host, port, ssl=ssl_context, origins=[None],
                           compression=None, max_size=MAX_FRAME, max_queue=16, write_limit=4096,
                           ping_interval=10, ping_timeout=5, close_timeout=1)


async def run_relay(args):
    context = None
    if args.cert and args.key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
    elif not args.behind_proxy:
        raise ValueError("Provide --cert and --key, or use --behind-proxy on a private proxy network.")
    relay = Relay(args.database, trust_proxy=args.behind_proxy)
    async with await relay.start(args.host, args.port, context):
        print(f"RemoteInput relay listening on {args.host}:{args.port}", flush=True)
        await asyncio.Event().wait()

