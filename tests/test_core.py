import concurrent.futures
import secrets
import ssl
import sys

import pytest

from remoteinput.capture import CapturePolicy, F9, F10
from remoteinput.config import client_options, relay_url
from remoteinput.platforms.keys import MAC_KEYS, MAC_MODIFIERS
from remoteinput.protocol import Event, Op, parse_control, unpack
from remoteinput.queueing import InputQueue, Overload
from remoteinput.registry import RateLimit, Registry
from remoteinput.safety import TargetEngine
from remoteinput.vault import begin_rotation, finish_rotation, target_identity

from helpers import FakeInjector, MemoryVault


@pytest.mark.parametrize("op,a,b", [(Op.MOVE, -40, 90), (Op.KEY, 65, 1), (Op.KEY, 65, 0),
                                   (Op.BUTTON, 1, 1), (Op.SCROLL, -120, 240), (Op.RESET, 0, 0)])
def test_wire_round_trip(op, a, b):
    event = Event(op, epoch=2, seq=50, stamp=1234, lease=8, a=a, b=b)
    assert unpack(event.pack()) == event


@pytest.mark.parametrize("data", [b"", b"x" * 100, Event(Op.KEY, a=256, b=1).pack(),
                                 Event(Op.BUTTON, a=6, b=0).pack(), Event(Op.KEY, a=65, b=3).pack(),
                                 Event(Op.MOVE, a=1_000_001).pack(), Event(Op.RESET, a=1).pack()])
def test_reject_malformed_input(data):
    with pytest.raises(ValueError):
        unpack(data)


@pytest.mark.parametrize("raw", ["[]", "null", '{"type":1}', '"x"', '{"type":"x","payload":"' + "x" * 3000 + '"}'])
def test_reject_unbounded_control(raw):
    with pytest.raises(ValueError):
        parse_control(raw)


def test_coalesce_adds_deltas_without_crossing_input_transitions():
    q = InputQueue()
    events = [Event(Op.MOVE, a=2, b=3), Event(Op.MOVE, a=5, b=-2), Event(Op.BUTTON, a=1, b=1),
              Event(Op.MOVE, a=4), Event(Op.KEY, a=65, b=1), Event(Op.SCROLL, b=120),
              Event(Op.BUTTON, a=1, b=0), Event(Op.KEY, a=65, b=0)]
    for event in events:
        assert q.put(event)
    assert q.pop()[0] == Event(Op.MOVE, a=7, b=1)
    assert [q.pop()[0] for _ in range(6)] == events[2:]
    assert q.pop() is None


def test_overload_requires_reset_instead_of_losing_a_release():
    q = InputQueue(limit=2)
    assert q.put(Event(Op.KEY, a=65, b=1))
    assert q.put(Event(Op.BUTTON, a=1, b=1))
    assert not q.put(Event(Op.KEY, a=65, b=0))
    with pytest.raises(Overload):
        q.pop()
    q.clear()
    assert q.put(Event(Op.KEY, a=65, b=0))


def test_coalescing_cannot_refresh_a_stale_backlog():
    now = [0.]
    q = InputQueue(clock=lambda: now[0])
    q.put(Event(Op.MOVE, a=1))
    now[0] = .05
    q.put(Event(Op.MOVE, a=1))
    now[0] = .101
    assert not q.put(Event(Op.MOVE, a=1))


def test_registry_collision_free_persistent_and_idempotent(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path)
    owners = [secrets.token_urlsafe(32) for _ in range(8)]
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        rows = list(pool.map(lambda owner: registry.register(owner, "a-strong-test-password-123456789"), owners))
    assert sorted(name for name, _ in rows) == [f"TARGET-{n:03d}" for n in range(1, 9)]
    restored = Registry(path)
    for owner, (name, generation) in zip(owners, rows):
        assert restored.register(owner, "ignored-retry-password") == (name, generation)
        assert restored.owner(name, owner) == 1
        assert restored.owner(name, "wrong-token") is None
    assert "TARGET-001" in [x[0] for x in rows]


def test_simultaneous_registration_retry_gets_one_id(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        names = list(pool.map(lambda _: registry.register("same-owner", "test-password"), range(4)))
    assert names == [("TARGET-001", 1)] * 4
    assert registry.register("next-owner", "password")[0] == "TARGET-002"


def test_rotation_is_idempotent_and_invalidates_old_password(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    name, _ = registry.register("owner-token", "old-password")
    assert registry.login(name, "old-password") == 1
    assert registry.rotate(name, "owner-token", "new-password", "rotation-one") == 2
    assert registry.rotate(name, "owner-token", "new-password", "rotation-one") == 2
    assert registry.login(name, "old-password") is None
    assert registry.login(name, "new-password") == 2
    with pytest.raises(ValueError):
        registry.rotate(name, "bad-owner", "bad-password", "rotation-two")


def test_local_credential_persistence_and_interrupted_rotation():
    vault = MemoryVault()
    initial = target_identity(vault)
    assert len(initial["owner_secret"]) >= 40
    assert len(initial["password"]) >= 32
    assert target_identity(vault) == initial
    pending = begin_rotation(vault, initial)
    assert begin_rotation(vault, target_identity(vault)) == pending
    finished = finish_rotation(vault, pending)
    assert finished["owner_secret"] == initial["owner_secret"]
    assert finished["password"] != initial["password"]
    assert "pending_password" not in target_identity(vault)
    vault.delete()
    assert vault.load() is None


def test_rate_limit_counts_inflight_attempts_and_recovers():
    now = [0.]
    limit = RateLimit(2, 10, capacity=2, clock=lambda: now[0])
    assert limit.allow("ip") and limit.allow("ip")
    assert not limit.allow("ip")
    assert limit.allow("second")
    assert not limit.allow("third")
    now[0] = 11
    assert limit.allow("ip")


@pytest.mark.parametrize("url", ["ws://localhost/ws", "wss://host/", "wss://u:p@host/ws", "wss://host/ws?token=x", "wss://x.invalid/ws"])
def test_production_connections_require_valid_wss(url):
    with pytest.raises(ValueError):
        relay_url(url)


def test_verified_tls_and_small_uncompressed_buffers():
    options = client_options()
    assert options["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert options["ssl"].check_hostname
    assert options["compression"] is None
    assert options["max_size"] <= 2048


def test_ca_bundle_and_no_tls_key_logging(tmp_path, monkeypatch):
    from remoteinput.config import public_tls_context
    path = tmp_path / "tls-secrets.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(path))
    context = public_tls_context()
    assert context.cert_store_stats()["x509_ca"] > 0
    assert context.keylog_filename is None
    assert not path.exists()


@pytest.fixture
def engine():
    clock = [1.]
    messages = []
    injection = FakeInjector()
    engine = TargetEngine(injection, messages.append, clock=lambda: clock[0])
    engine.set_connected(True)
    engine.tick()
    nonce = next(iter(engine.leases))
    engine.receive(Event(Op.ACTIVATE, epoch=123, seq=1, lease=nonce))
    engine.process_one()
    assert engine.epoch == 123
    return engine, injection, clock, nonce, messages


def deliver(engine, nonce, op, seq, a=0, b=0, epoch=123):
    engine.receive(Event(op, epoch=epoch, seq=seq, lease=nonce, a=a, b=b))
    engine.process_one()


def test_typing_hold_drag_and_reset(engine):
    e, inject, _, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    deliver(e, nonce, Op.KEY, 3, 65, 1)  # OS repeat / held key
    deliver(e, nonce, Op.BUTTON, 4, 1, 1)
    deliver(e, nonce, Op.MOVE, 5, 10, -20)
    deliver(e, nonce, Op.SCROLL, 6, 0, 120)
    e.reset()
    assert inject.events[:5] == [("key", 65, True), ("key", 65, True), ("button", 1, True),
                                  ("move", 10, -20), ("scroll", 0, 120)]
    assert ("key", 65, False) in inject.events
    assert ("button", 1, False) in inject.events
    assert not e.keys and not e.buttons and not e.epoch


def test_unresponsive_controller_releases_with_relay_still_connected(engine):
    e, inject, clock, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    deliver(e, nonce, Op.BUTTON, 3, 1, 1)
    clock[0] += .9
    e.tick()
    assert e.connected and e.epoch == 0
    assert ("key", 65, False) in inject.events and ("button", 1, False) in inject.events


def test_fresh_input_without_controller_heartbeat_cannot_keep_a_hold_alive(engine):
    e, _, clock, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    clock[0] += .6
    e.tick()
    fresh_nonce = list(e.leases)[-1]
    clock[0] += .3
    deliver(e, fresh_nonce, Op.MOVE, 3, 5, 5)
    assert e.epoch == 0 and not e.keys


def test_permissions_are_rechecked_and_reactivation_is_explicit(engine):
    e, inject, _, nonce, messages = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    inject.permission = False
    e.tick()
    assert not e.epoch and not e.keys
    e.receive(Event(Op.ACTIVATE, epoch=456, seq=4, lease=nonce))
    e.process_one()
    assert not e.epoch
    assert any('"paused"' in m for m in messages if isinstance(m, str))


def test_disconnect_reconnect_starts_paused_and_ignores_stale_input(engine):
    e, inject, _, nonce, _ = engine
    deliver(e, nonce, Op.BUTTON, 2, 1, 1)
    e.set_connected(False)
    e.set_connected(True)
    e.tick()
    deliver(e, nonce, Op.KEY, 3, 65, 1)
    assert not e.epoch and not e.keys and not e.buttons
    assert ("button", 1, False) in inject.events


def test_expired_lease_and_duplicate_sequence_are_not_injected(engine):
    e, inject, clock, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    deliver(e, nonce, Op.KEY, 2, 66, 1)
    assert ("key", 66, True) not in inject.events
    clock[0] += .9
    deliver(e, nonce, Op.KEY, 3, 67, 1)
    assert ("key", 67, True) not in inject.events
    assert not e.keys


def test_input_can_use_the_previous_challenge_while_heartbeats_remain_fresh(engine):
    e, inject, clock, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    deliver(e, nonce, Op.BUTTON, 3, 1, 1)
    clock[0] += .75
    e.tick()
    deliver(e, nonce, Op.HEARTBEAT, 4)
    # On a 750 ms round trip, the immediately echoed heartbeat arrives in time,
    # then input using that same token arrives before the next challenge echo.
    clock[0] += .2
    e.tick()
    deliver(e, nonce, Op.MOVE, 5, 6, -2)
    deliver(e, nonce, Op.BUTTON, 6, 1, 0)
    deliver(e, nonce, Op.KEY, 7, 65, 0)
    assert e.epoch == 123
    assert inject.events[-3:] == [("move", 6, -2), ("button", 1, False), ("key", 65, False)]
    assert not e.keys and not e.buttons


def test_old_epoch_and_duplicate_frames_cannot_pause_a_new_active_epoch(engine):
    e, _, clock, nonce, _ = engine
    for seq in (4, 5):
        clock[0] += .6
        e.tick()
        deliver(e, list(e.leases)[-1], Op.HEARTBEAT, seq)
    clock[0] += .1
    e.tick()
    deliver(e, nonce, Op.PROBE, 3, epoch=0)
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    assert e.epoch == 123 and not e.keys


def test_genuinely_expired_input_still_resets_a_session_with_fresh_heartbeats(engine):
    e, inject, clock, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    deliver(e, nonce, Op.BUTTON, 3, 1, 1)
    for seq in (4, 5):
        clock[0] += .6
        e.tick()
        deliver(e, list(e.leases)[-1], Op.HEARTBEAT, seq)
    deliver(e, nonce, Op.MOVE, 6, 90, 90)
    assert not e.epoch and not e.keys and not e.buttons
    assert ("move", 90, 90) not in inject.events
    assert ("key", 65, False) in inject.events and ("button", 1, False) in inject.events


def test_previous_challenge_cannot_extend_the_heartbeat_deadline(engine):
    e, _, clock, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    clock[0] += .6
    e.tick()
    deliver(e, list(e.leases)[-1], Op.HEARTBEAT, 3)
    clock[0] += .35
    deliver(e, nonce, Op.HEARTBEAT, 4)
    assert not e.epoch and not e.keys


def test_overload_releases_all_held_input(engine):
    e, inject, _, nonce, _ = engine
    deliver(e, nonce, Op.KEY, 2, 65, 1)
    e.queue.limit = 1
    e.receive(Event(Op.KEY, epoch=123, lease=nonce, seq=3, a=66, b=1))
    e.receive(Event(Op.KEY, epoch=123, lease=nonce, seq=4, a=65, b=0))
    assert not e.epoch and not e.keys
    assert ("key", 65, False) in inject.events


@pytest.mark.parametrize("hotkey", [F9, F10])
def test_control_hotkeys_stay_local_and_ordinary_keys_are_suppressed(hotkey):
    events, actions = [], []
    policy = CapturePolicy(events.append, lambda: actions.append("toggle"), lambda: actions.append("stop"))
    assert not policy.key(65, True)
    policy.key(65, False)
    policy.activate()
    assert policy.key(0xA2, True)
    assert policy.key(0xA4, True)
    assert policy.key(hotkey, True)
    for _ in range(4):
        assert policy.key(hotkey, True)  # Holding F9/F10 must not retrigger it.
    assert policy.key(hotkey, False)
    assert len(actions) == 1
    assert events == []
    policy.pause()
    assert not policy.key(66, True)


def test_modifier_shortcuts_and_drag_keep_input_order():
    events = []
    policy = CapturePolicy(events.append, lambda: None, lambda: None)
    policy.activate()
    policy.key(0xA2, True)
    policy.key(67, True)
    policy.key(67, False)
    policy.key(0xA2, False)
    policy.mouse(Op.BUTTON, 1, 1)
    policy.mouse(Op.MOVE, 100, -20)
    policy.mouse(Op.BUTTON, 1, 0)
    assert [(e.op, e.a, e.b) for e in events] == [
        (Op.KEY, 0xA2, 1), (Op.KEY, 67, 1), (Op.KEY, 67, 0), (Op.KEY, 0xA2, 0),
        (Op.BUTTON, 1, 1), (Op.MOVE, 100, -20), (Op.BUTTON, 1, 0)]


def test_macos_shortcut_mapping():
    assert MAC_KEYS[0xA2] == 55  # left Ctrl -> Command
    assert MAC_KEYS[0xA3] == 62  # right Ctrl -> Control
    assert MAC_KEYS[0x5B] == 59  # Windows -> Control
    assert MAC_MODIFIERS[0xA2] == 1 << 20
    assert all(vk in MAC_KEYS for vk in range(65, 91))


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows DPAPI")
def test_native_windows_vault_round_trip_and_deletion(tmp_path, monkeypatch):
    from remoteinput.vault import DPAPI
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    vault = DPAPI("test-only")
    vault.write(b"secret-value")
    assert b"secret-value" not in vault.path.read_bytes()
    assert DPAPI("test-only").read() == b"secret-value"
    vault.delete()
    assert vault.read() is None
