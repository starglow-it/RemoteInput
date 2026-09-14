import argparse
import asyncio
import json
import sys
import time

from websockets.asyncio.client import connect

from . import __version__
from .config import RELAY_URL, client_options, private_logging, relay_url
from .metrics import Metrics
from .protocol import control, parse_control


async def probe(url, count):
    metrics = Metrics()
    async with connect(url, **client_options()) as ws:
        await ws.send(control("diagnostic"))
        reply = parse_control(await asyncio.wait_for(ws.recv(), 10))
        if reply["type"] != "diagnostic_ready":
            raise RuntimeError("Relay diagnostic unavailable.")
        for _ in range(count):
            start = time.perf_counter()
            pong = await ws.ping()
            await asyncio.wait_for(pong, 5)
            metrics.add("this_pc_relay_link_rtt", (time.perf_counter() - start) * 1000)
            await asyncio.sleep(.2)
    metrics.display()
    print("Run on both PCs for each candidate relay region. This is link RTT, not end-to-end input latency.")
    return metrics.snapshot()


def self_check():
    from .protocol import FRAME, Event, Op, unpack
    assert unpack(Event(Op.MOVE, a=3, b=-4).pack()).b == -4
    native = "not available on this platform"
    if sys.platform == "win32":
        import ctypes
        from .platforms.windows import INPUT, WindowsInjector
        assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        native = "Win32 loaded; desktop=" + str(WindowsInjector().permitted())
    elif sys.platform == "darwin":
        from .platforms.macos import MacInjector
        native = "CoreGraphics loaded; accessibility=" + str(MacInjector().permitted())
    return {"version": __version__, "platform": sys.platform, "input_frame_bytes": FRAME.size,
            "relay_embedded": bool(RELAY_URL), "native": native,
            "note": "Import/protocol check only; no real keys or mouse input were injected."}


def main():
    private_logging()
    parser = argparse.ArgumentParser(prog="RemoteInput", description="Mouse and keyboard control, without screen capture.")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="mode", required=True)
    for name in ("target", "controller", "forget-target", "probe"):
        sub = subs.add_parser(name)
        sub.add_argument("--relay", help="Developer override; distributed apps embed the relay address.")
        if name == "probe":
            sub.add_argument("--count", type=int, default=30)
            sub.add_argument("--output", help="Optional numeric-only JSON measurement report.")
    subs.add_parser("self-check")
    relay = subs.add_parser("relay")
    relay.add_argument("--database", default="remoteinput.sqlite3")
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=8765)
    relay.add_argument("--cert")
    relay.add_argument("--key")
    relay.add_argument("--behind-proxy", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "self-check":
            print(json.dumps(self_check(), indent=2))
        elif args.mode == "relay":
            from .relay import run_relay
            asyncio.run(run_relay(args))
        else:
            url = relay_url(args.relay)
            if args.mode == "probe":
                if not 1 <= args.count <= 100:
                    raise ValueError("Use between 1 and 100 samples.")
                result = asyncio.run(probe(url, args.count))
                if args.output:
                    from pathlib import Path
                    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
            else:
                if args.mode == "controller" and sys.platform != "win32":
                    raise RuntimeError("Start Controller requires Windows 11.")
                if args.mode == "target" and sys.platform not in ("win32", "darwin"):
                    raise RuntimeError("Start Target supports Windows 11 and macOS only.")
                from .instance import SingleInstance
                role = "controller" if args.mode == "forget-target" else args.mode
                with SingleInstance(role, url):
                    if args.mode == "target":
                        from .target import run_target
                        asyncio.run(run_target(url))
                    elif args.mode == "controller":
                        from .controller import run_controller
                        asyncio.run(run_controller(url))
                    else:
                        from .vault import Vault
                        Vault(url, "controller").delete()
                        print("Target forgotten.")
    except KeyboardInterrupt:
        print("Stopped.")
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Avoid tracebacks of networking/authentication internals, which could contain secrets.
        print("RemoteInput could not complete the operation. Check connectivity and OS permissions.", file=sys.stderr)
        raise SystemExit(1) from None

