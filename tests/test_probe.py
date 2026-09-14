import asyncio
import errno
import socket

import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from remoteinput import cli
from remoteinput.config import client_options
from remoteinput.protocol import control
from remoteinput.registry import RateLimit
from remoteinput.relay import Relay

from helpers import tls_contexts


@pytest.fixture
def contexts(tmp_path):
    return tls_contexts(tmp_path)


def listener_url(listener):
    return f"wss://127.0.0.1:{listener.sockets[0].getsockname()[1]}/ws"


async def test_probe_reports_verified_relay_and_numeric_rtt(tmp_path, contexts, capsys):
    server_tls, client_tls = contexts
    relay = Relay(tmp_path / "registry.sqlite3")
    async with await relay.start(port=0, ssl_context=server_tls) as listener:
        result = await cli.probe(listener_url(listener), 2, client_tls)
    output = capsys.readouterr().out
    assert "TLS certificate verified; WebSocket upgrade accepted." in output
    assert "RemoteInput relay ready" in output
    assert "not end-to-end input latency" in output
    assert result["this_pc_relay_link_rtt"]["samples"] == 2
    assert relay.targets == {} and relay.controllers == {}


async def test_probe_rejects_untrusted_certificate_with_specific_error(tmp_path, contexts, capsys):
    server_tls, _ = contexts
    relay = Relay(tmp_path / "registry.sqlite3")
    async with await relay.start(port=0, ssl_context=server_tls) as listener:
        with pytest.raises(cli.ProbeError, match="TLS certificate verification failed"):
            await cli.probe(listener_url(listener), 1)
    assert "TLS certificate verified" not in capsys.readouterr().out


async def test_probe_dns_error_does_not_print_exception_payload(monkeypatch):
    async def no_dns(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "PASSWORD_MUST_NOT_APPEAR")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", no_dns)
    with pytest.raises(cli.ProbeError, match="DNS lookup failed") as caught:
        await cli.probe("wss://relay.example.org/ws", 1)
    assert "PASSWORD_MUST_NOT_APPEAR" not in str(caught.value)


@pytest.mark.parametrize("status,expected", [(502, "relay:8765"), (404, "/ws route")])
async def test_probe_distinguishes_http_rejections_without_logging_response(
        contexts, capsys, caplog, status, expected):
    server_tls, client_tls = contexts
    secret = "PASSWORD_MUST_NOT_APPEAR"

    def reject(ws, request):
        return Response(status, secret, Headers({"X-Private": secret}), secret.encode())

    async def handler(ws):
        pytest.fail("The HTTP rejection must prevent a WebSocket session")

    async with serve(handler, "127.0.0.1", 0, ssl=server_tls, process_request=reject) as listener:
        with pytest.raises(cli.ProbeError, match=f"HTTP {status}") as caught:
            await cli.probe(listener_url(listener), 1, client_tls)
    output = capsys.readouterr()
    assert expected in str(caught.value)
    assert secret not in str(caught.value) + output.out + output.err + caplog.text


async def test_probe_reports_rate_limit(tmp_path, contexts):
    server_tls, client_tls = contexts
    relay = Relay(tmp_path / "registry.sqlite3")
    relay.connect_limit = RateLimit(1, 60)
    assert relay.connect_limit.allow("127.0.0.1")
    async with await relay.start(port=0, ssl_context=server_tls) as listener:
        with pytest.raises(cli.ProbeError, match="rate limiting"):
            await cli.probe(listener_url(listener), 1, client_tls)


async def test_probe_reports_close_code_without_remote_reason(contexts):
    server_tls, client_tls = contexts
    secret = "PASSWORD_MUST_NOT_APPEAR"

    async def handler(ws):
        await ws.recv()
        await ws.close(code=1011, reason=secret)

    async with serve(handler, "127.0.0.1", 0, ssl=server_tls) as listener:
        with pytest.raises(cli.ProbeError, match="WebSocket code 1011") as caught:
            await cli.probe(listener_url(listener), 1, client_tls)
    assert "waiting for the relay diagnostic reply" in str(caught.value)
    assert secret not in str(caught.value)


async def test_probe_reports_invalid_reply_without_remote_payload(contexts):
    server_tls, client_tls = contexts
    secret = "PASSWORD_MUST_NOT_APPEAR"

    async def handler(ws):
        await ws.recv()
        await ws.send(control("unexpected", reason=secret))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0, ssl=server_tls) as listener:
        with pytest.raises(cli.ProbeError, match="did not accept the diagnostic request") as caught:
            await cli.probe(listener_url(listener), 1, client_tls)
    assert secret not in str(caught.value)


async def test_probe_open_timeout_has_no_permission_advice(monkeypatch):
    async def stall(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()

    def short_timeout(context):
        return {**client_options(context), "open_timeout": .05}

    monkeypatch.setattr(cli, "client_options", short_timeout)
    async with await asyncio.start_server(stall, "127.0.0.1", 0) as listener:
        with pytest.raises(cli.ProbeError, match="timed out") as caught:
            await cli.probe(listener_url(listener), 1)
    assert "opening the verified WSS connection" in str(caught.value)
    assert "Check connectivity and OS permissions" not in str(caught.value)


def test_probe_cli_prints_network_error_and_exits_unsuccessfully(monkeypatch, capsys):
    def refused(*args, **kwargs):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "PASSWORD_MUST_NOT_APPEAR")

    monkeypatch.setattr(cli, "connect", refused)
    monkeypatch.setattr("sys.argv", ["remoteinput", "probe", "--relay", "wss://relay.example.org/ws"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert "Connection refused" in output.err
    assert "PASSWORD_MUST_NOT_APPEAR" not in output.out + output.err
    assert "Traceback" not in output.err
