import asyncio
import time
from types import SimpleNamespace

from remoteinput.controller import Controller
from remoteinput.timing import NetworkTiming

from helpers import MemoryVault
from test_clients import FakeCapture


def test_network_estimator_is_bounded_and_ignores_unusable_measurements():
    timing = NetworkTiming()
    assert timing.timeout == 2
    for sample in (.9, .927, 1.6, 2.8, .4, .05):
        assert timing.observe(sample)
        assert 2 <= timing.timeout <= 3
    previous = timing.rtt, timing.variation, timing.timeout
    for sample in (-1, 3, 30, float("inf"), float("nan")):
        assert not timing.observe(sample)
        assert (timing.rtt, timing.variation, timing.timeout) == previous
    for _ in range(60):
        timing.observe(.05)
    assert timing.timeout == 2


async def test_controller_tolerates_short_update_gaps_but_restores_local_input_on_silence():
    client = Controller("unused", MemoryVault(), {}, capture_factory=FakeCapture)
    client.connected, client.epoch, client.lease = True, 123, 1
    client.outbox = SimpleNamespace(put=lambda _: None)
    client.capture.set_active(True)
    client.lease_received = time.monotonic() - .8
    monitor = asyncio.create_task(client.monitor(None))
    try:
        await asyncio.sleep(.075)
        assert client.epoch == 123 and client.capture.policy.active
        client.lease_received = time.monotonic() - 3.1
        await asyncio.sleep(.075)
        assert not client.epoch and not client.capture.policy.active
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


async def test_activation_uses_the_measured_network_budget():
    client = Controller("unused", MemoryVault(), {}, capture_factory=FakeCapture)
    client.connected, client.lease = True, 1
    sent = []
    client.outbox = SimpleNamespace(put=sent.append)
    client.network.observe(1.2)
    started = time.monotonic()
    client.lease_received = started - .8
    client.activate()
    assert client.pending_epoch and sent and not client.capture.policy.active
    assert client.activate_deadline - started >= 3
