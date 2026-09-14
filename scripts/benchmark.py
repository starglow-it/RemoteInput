"""Repeatable TLS loopback comparison. No real desktop events or WAN claims.

Reference policy deliberately flushes at 8 ms intervals. The optimized policy
uses the production queue's immediate wakeup. Both use the same protocol, TLS,
relay and threaded TargetEngine with a test sink. This is a policy comparison,
not a measurement of a previously deployed release.
"""
import argparse
import asyncio
import contextlib
import json
import platform
import secrets
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from helpers import FakeInjector, eventually, tls_contexts  # noqa: E402
from remoteinput.config import client_options  # noqa: E402
from remoteinput.metrics import Metrics  # noqa: E402
from remoteinput.protocol import ACK, Event, Op, control, parse_control, unpack  # noqa: E402
from remoteinput.queueing import InputQueue  # noqa: E402
from remoteinput.relay import Relay  # noqa: E402
from remoteinput.safety import TargetEngine  # noqa: E402
from remoteinput.transport import Outbox  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402


async def incoming(ws, delay):
    """Ordered delivery delay with concurrent receipt, not per-frame serialization delay."""
    queue = asyncio.Queue(256)

    async def receive():
        async for raw in ws:
            await queue.put((raw, time.monotonic() + delay))
    task = asyncio.create_task(receive())
    try:
        while True:
            raw, due = await queue.get()
            remaining = due - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            yield raw
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def trial(url, ssl_context, batched, delivery_delay, bursts):
    metrics = Metrics()
    owner, password = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    loop = asyncio.get_running_loop()
    tasks = []
    async with connect(url, **client_options(ssl_context)) as target_ws:
        await target_ws.send(control("target", owner_secret=owner, password=password))
        name = parse_control(await target_ws.recv())["id"]
        target_out = Outbox(loop)
        sink = FakeInjector()
        engine = TargetEngine(sink, target_out.put)
        engine.set_connected(True)
        engine.start()

        async def target_reader():
            async for raw in incoming(target_ws, delivery_delay):
                if isinstance(raw, bytes):
                    engine.receive(unpack(raw))
                else:
                    message = parse_control(raw)
                    if message["type"] in ("peer_joined", "peer_gone"):
                        engine.reset()

        tasks += [asyncio.create_task(target_reader()), asyncio.create_task(target_out.writer(target_ws))]
        try:
            async with connect(url, **client_options(ssl_context)) as ws:
                await ws.send(control("controller", id=name, password=password))
                assert parse_control(await ws.recv())["type"] == "connected"
                output = Outbox(loop, metrics=metrics)
                ready = asyncio.Event()
                queue = InputQueue(ready.set)
                state = {"nonce": 0, "seq": 0, "active": False, "sent": 0, "acks": 0}
                epoch = secrets.randbits(64)
                pending = {}

                def send(event, queue_age=0):
                    # Sample every event in this benchmark. Production samples every 32nd sequence.
                    state["seq"] += 32
                    frame = event.with_fields(epoch=epoch, seq=state["seq"], stamp=time.perf_counter_ns(),
                                              lease=state["nonce"])
                    if event.op in (Op.MOVE, Op.KEY):
                        pending[frame.seq] = queue_age * 1000
                        state["sent"] += 1
                    output.put(frame.pack())

                async def reader():
                    async for raw in incoming(ws, delivery_delay):
                        if isinstance(raw, bytes):
                            _, op, _, seq, stamp, target_queue, injection = ACK.unpack(raw)
                            if seq in pending:
                                rtt = (time.perf_counter_ns() - stamp) / 1e6
                                metrics.add("controller_target_controller_rtt", rtt)
                                metrics.add("capture_to_ack", rtt + pending.pop(seq))
                                metrics.add("target_queue", target_queue / 1000)
                                metrics.add("fake_sink_processing", injection / 1000)
                                state["acks"] += 1
                        else:
                            msg = parse_control(raw)
                            if msg["type"] == "challenge":
                                state["nonce"] = msg["nonce"]
                                if state["active"]:
                                    send(Event(Op.HEARTBEAT))
                            elif msg["type"] == "activated":
                                state["active"] = True
                            elif msg["type"] == "paused":
                                state["active"] = False

                async def sender():
                    while True:
                        if batched:
                            await asyncio.sleep(.008)
                        else:
                            await ready.wait()
                        ready.clear()
                        while (item := queue.pop()) is not None:
                            event, age = item
                            metrics.add("controller_queue", age * 1000)
                            send(event, age)

                tasks += [asyncio.create_task(reader()), asyncio.create_task(output.writer(ws))]
                await eventually(lambda: bool(state["nonce"]))
                send(Event(Op.ACTIVATE))
                await eventually(lambda: state["active"])
                tasks.append(asyncio.create_task(sender()))
                start = time.perf_counter()
                for _ in range(bursts):
                    # Representative ordering boundary: three moves, a key down, then its release.
                    for event in (Event(Op.MOVE, a=1, b=1), Event(Op.MOVE, a=2, b=-1), Event(Op.MOVE, a=-1, b=1),
                                  Event(Op.KEY, a=65, b=1), Event(Op.KEY, a=65, b=0)):
                        assert queue.put(event)
                    await asyncio.sleep(.005)
                await eventually(lambda: state["sent"] == bursts * 3 and state["acks"] == state["sent"], seconds=5)
                elapsed = time.perf_counter() - start
                send(Event(Op.RESET))
                await eventually(lambda: not engine.epoch)
                return {"policy": "reference_8ms_flush" if batched else "immediate_flush",
                        "endpoint_receive_delay_ms": delivery_delay * 1000,
                        "emulated_extra_round_trip_ms": delivery_delay * 2000,
                        "captured_input_events": bursts * 5, "transmitted_input_frames": state["sent"],
                        "target_acknowledgments": state["acks"], "elapsed_s": elapsed,
                        "timings": metrics.snapshot()}
        finally:
            engine.set_connected(False)
            engine.stop()
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    raise result


async def benchmark(bursts):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        server_ssl, client_ssl = tls_contexts(path)
        relay = Relay(path / "registry.sqlite3")
        listener = await relay.start(port=0, ssl_context=server_ssl)
        try:
            url = f"wss://127.0.0.1:{listener.sockets[0].getsockname()[1]}/ws"
            trials = []
            for delay in (0, .020):
                for batched in (True, False):
                    trials.append(await trial(url, client_ssl, batched, delay, bursts))
            return {"environment": {"platform": platform.platform(), "python": platform.python_version(),
                    "transport": "Certificate-verified WSS over loopback; both clients and relay on one host",
                    "network": "Loopback plus separate 40 ms RTT simulation (20 ms per endpoint receive path). "
                               "No WAN, packet loss, jitter, bandwidth cap, Wi-Fi or screen feed measured.",
                    "input": "Dedicated target worker with fake input sink; no native OS injection measured",
                    "baseline": "Reference policy adds an 8 ms flush timer; no earlier production build is implied",
                    "ack_sampling": "Every benchmark input; production acknowledges each 32nd input sequence and probes"},
                    "trials": trials}
        finally:
            listener.close()
            await listener.wait_closed()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/latency-local.json")
    parser.add_argument("--bursts", type=int, default=200)
    args = parser.parse_args()
    result = asyncio.run(benchmark(args.bursts))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    for trial_result in result["trials"]:
        rtt = trial_result["timings"]["controller_target_controller_rtt"]
        total = trial_result["timings"]["capture_to_ack"]
        print(f"{trial_result['policy']} +{trial_result['emulated_extra_round_trip_ms']:.0f}ms simulated RTT: "
              f"RTT median/p95 {rtt['median_ms']:.2f}/{rtt['p95_ms']:.2f} ms; "
              f"capture-to-ACK median/p95 {total['median_ms']:.2f}/{total['p95_ms']:.2f} ms")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()

