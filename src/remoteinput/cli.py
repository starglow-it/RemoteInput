import argparse
import asyncio
import errno
import json
import socket
import ssl
import sys
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from . import __version__
from .config import RELAY_URL, client_options, private_logging, public_tls_context, relay_url
from .metrics import Metrics
from .protocol import control, parse_control


class ProbeError(RuntimeError):
    """A diagnostic message that never includes remote payloads or exception text."""


def _probe_error(error):
    # Exception strings, HTTP bodies/headers, and close reasons may contain secrets
    # or terminal escapes. Only use known categories and numeric status/error codes.
    if isinstance(error, socket.gaierror):
        return "DNS lookup failed. Check that the relay hostname resolves to the server's public IP."
    if isinstance(error, ssl.SSLCertVerificationError):
        return ("TLS certificate verification failed. Check the certificate's hostname, expiry, "
                "and full trust chain, and the clock on this PC. Keep certificate verification enabled.")
    if isinstance(error, ssl.SSLError):
        return ("TLS negotiation failed. Check Caddy's certificate logs and the configured hostname; "
                "the default deployment needs a public DNS hostname for a trusted certificate.")
    if isinstance(error, InvalidStatus):
        code = error.response.status_code
        if code in (502, 503, 504):
            hint = "Check that the Python relay is running and Caddy can reach relay:8765."
        elif code == 404:
            hint = "Check the Caddy hostname and /ws route."
        elif code in (401, 403):
            hint = "Check the reverse proxy's access rules for the /ws route."
        elif code == 429:
            hint = "The server is rate limiting connections. Wait a minute before retrying."
        else:
            hint = "Check that /ws forwards WebSocket upgrades to the RemoteInput relay."
        return f"WebSocket upgrade rejected (HTTP {int(code)}). {hint}"
    if isinstance(error, InvalidHandshake):
        return "WebSocket handshake failed. Check that the reverse proxy forwards WebSocket upgrades on /ws."
    if isinstance(error, ConnectionClosed):
        code = int(error.rcvd.code) if error.rcvd else 1006
        if code == 1008 and error.rcvd.reason == "rate_limited":
            # Admission may close before send() completes. Match this known token
            # without reproducing an arbitrary close reason in the console.
            return "The relay is rate limiting connections. Wait a minute before retrying."
        return f"The relay connection closed (WebSocket code {code}). Check the Caddy and relay logs."
    if isinstance(error, TimeoutError):
        return ("The operation timed out. Check relay availability, server TCP port 443, "
                "and the network connection. No mouse or keyboard permissions are needed for this probe.")
    if isinstance(error, OSError):
        code = getattr(error, "winerror", None) or error.errno
        if isinstance(error, ConnectionRefusedError) or code in (errno.ECONNREFUSED, 10061):
            return "Connection refused. Check that Caddy is running and listening on the relay's HTTPS port."
        if code in (errno.EACCES, errno.EPERM, 10013):
            return "Network access was denied. Check this PC's firewall or network policy for outbound HTTPS."
        suffix = f" (OS error {int(code)})" if code is not None else ""
        return f"Network or local I/O failed{suffix}. Check connectivity and the server logs."
    if isinstance(error, ValueError):
        return "The server returned an invalid RemoteInput diagnostic response. Check the /ws route and relay version."
    return ("Unexpected error in the connection check. Run the project's Python with '-m pip check' "
            "and report the failed stage.")


async def probe(url, count, ssl_context=None):
    url = relay_url(url)
    private_logging()
    metrics = Metrics()
    stage = "opening the verified WSS connection"
    print("Checking the relay connection (no mouse or keyboard permissions required)...", flush=True)
    try:
        async with connect(url, **client_options(ssl_context)) as ws:
            print("TLS certificate verified; WebSocket upgrade accepted.", flush=True)
            stage = "waiting for the relay diagnostic reply"
            await ws.send(control("diagnostic"))
            reply = parse_control(await asyncio.wait_for(ws.recv(), 10))
            if reply["type"] != "diagnostic_ready":
                if reply["type"] == "error" and reply.get("reason") == "rate_limited":
                    raise ProbeError("The relay is rate limiting connections. Wait a minute before retrying.")
                raise ProbeError("The relay did not accept the diagnostic request. Check the /ws route and relay version.")
            print("RemoteInput relay ready; measuring this PC's link RTT.", flush=True)
            stage = "waiting for a relay ping reply"
            for _ in range(count):
                start = time.perf_counter()
                pong = await ws.ping()
                await asyncio.wait_for(pong, 5)
                metrics.add("this_pc_relay_link_rtt", (time.perf_counter() - start) * 1000)
                await asyncio.sleep(.2)
            stage = "closing the diagnostic connection"
    except ProbeError:
        raise
    except Exception as error:
        raise ProbeError(f"Probe failed while {stage}. {_probe_error(error)}") from None
    metrics.display()
    print("Run on both PCs for each candidate relay region. This is link RTT, not end-to-end input latency.")
    return metrics.snapshot()


def self_check():
    from .protocol import FRAME, Event, Op, unpack
    assert unpack(Event(Op.MOVE, a=3, b=-4).pack()).b == -4
    trusted = public_tls_context().cert_store_stats()["x509_ca"]
    assert trusted > 0
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
            "trusted_certificate_authorities": trusted,
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
