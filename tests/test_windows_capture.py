"""Exercise the Windows message-pump logic with simulated Win32 calls, on every OS.

No hooks or input events are installed on the machine running these tests.
"""
import ctypes as C
import importlib.util
import queue
import threading
import time
from pathlib import Path

import pytest


class Function:
    def __init__(self, call=lambda *args: 1):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class DLL:
    def __getattr__(self, name):
        function = Function()
        setattr(self, name, function)
        return function


class MessagePump:
    def __init__(self, module, user32, kernel32):
        self.module = module
        self.messages = queue.Queue()
        self.native_threads = []
        self.cursor = (100, 200)
        self.callback_timed_out = False
        user32.GetMessageW.call = self.get_message
        user32.PostThreadMessageW.call = self.post
        user32.GetCursorPos.call = self.get_cursor
        user32.SetCursorPos.call = self.set_cursor
        user32.GetSystemMetrics.call = lambda index: (1920, 1080)[index]
        user32.SendInput.call = self.send_input
        kernel32.GetCurrentThreadId.call = threading.get_ident

    def post(self, thread_id, message, wparam, lparam):
        self.messages.put((message, wparam, lparam))
        return 1

    def get_message(self, pointer, *args):
        while True:
            try:
                item = self.messages.get(timeout=.05)
            except queue.Empty:
                item = (0x113, 0, 0)  # WM_TIMER
            if callable(item):
                item()  # Sent hook callbacks need not make GetMessage return.
                continue
            message = C.cast(pointer, C.POINTER(self.module.W.MSG)).contents
            message.message, message.wParam, message.lParam = item
            return int(item[0] != 0x12)

    def get_cursor(self, pointer):
        point = C.cast(pointer, C.POINTER(self.module.W.POINT)).contents
        point.x, point.y = self.cursor
        return 1

    def set_cursor(self, x, y):
        self.native_threads.append(threading.get_ident())
        self.cursor = (x, y)
        return 1

    def on_hook(self, callback, timeout=2):
        if threading.get_ident() == self.capture.thread_id:
            callback()
            return True
        done = threading.Event()

        def run():
            try:
                callback()
            finally:
                done.set()

        self.messages.put(run)
        return done.wait(timeout)

    def send_input(self, *args):
        self.native_threads.append(threading.get_ident())

        def pending_physical_input():
            # A physical key-up can reach the hook while SendInput dispatches.
            data = self.module.KBD(vk=0xA0)
            self.capture._key(0, 0x101, C.addressof(data))

        if not self.on_hook(pending_physical_input, timeout=.15):
            self.callback_timed_out = True
        return 1


@pytest.fixture
def capture(monkeypatch):
    user32, kernel32 = DLL(), DLL()
    # Load an isolated module against API doubles, including on native Windows CI.
    path = Path(__file__).parents[1] / "src/remoteinput/platforms/windows.py"
    spec = importlib.util.spec_from_file_location("remoteinput.platforms._capture_test", path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(C, "WinDLL", lambda name, **kwargs: user32 if name == "user32" else kernel32,
                      raising=False)
        patch.setattr(C, "WINFUNCTYPE", C.CFUNCTYPE, raising=False)
        spec.loader.exec_module(module)
    pump = MessagePump(module, user32, kernel32)
    events, stops = [], []
    instance = module.WindowsCapture(events.append, lambda: None, lambda reason=None: stops.append(reason))
    pump.capture = instance
    instance.start()
    instance.network_tick()
    try:
        yield instance, pump, events, stops
    finally:
        instance.stop()
        assert not instance.thread.is_alive()


def test_activation_does_not_block_hook_dispatch_or_run_native_calls_on_network_thread(capture):
    instance, pump, events, stops = capture
    instance.policy.key(0xA2, True)
    instance.policy.key(0xA4, True)
    instance.set_active(True)
    assert pump.on_hook(lambda: None)
    assert not pump.callback_timed_out, "Activation held a lock needed by the Windows hook"
    assert instance.policy.active and instance.healthy()
    assert pump.native_threads and set(pump.native_threads) == {instance.thread_id}
    assert not events and not stops
    instance.set_active(False)
    assert not instance.policy.active
    assert pump.on_hook(lambda: None)
    assert pump.cursor == (100, 200)


@pytest.mark.parametrize("kind", ["keyboard", "mouse"])
def test_busy_hook_callbacks_count_as_a_healthy_pump(capture, kind):
    instance, pump, _, _ = capture
    observed = []

    def busy_callback():
        instance.last_pump = time.monotonic() - 1
        if kind == "keyboard":
            data = pump.module.KBD(vk=65)
            instance._key(0, 0x101, C.addressof(data))
        else:
            data = pump.module.MOUSE()
            instance._mouse(0, 0x200, C.addressof(data))
        observed.append(instance.healthy())

    assert pump.on_hook(busy_callback)
    assert observed == [True]


def test_pause_cancels_an_activation_waiting_for_the_hook_thread(capture):
    instance, pump, events, stops = capture
    entered, resume = threading.Event(), threading.Event()

    def occupied():
        entered.set()
        resume.wait(2)

    pump.messages.put(occupied)
    assert entered.wait(1)
    try:
        instance.set_active(True)
        instance.set_active(False)
        assert not instance.policy.active
    finally:
        resume.set()
    assert pump.on_hook(lambda: None)
    assert not instance.policy.active
    assert pump.cursor == (100, 200)
    assert not events and not stops


def test_busy_hooks_still_restore_local_input_when_network_loop_freezes(capture):
    instance, pump, events, stops = capture
    instance.set_active(True)
    assert pump.on_hook(lambda: None)
    assert instance.policy.active

    def network_frozen():
        instance.last_network_tick = time.monotonic() - 1
        data = pump.module.KBD(vk=65)
        instance._key(0, 0x101, C.addressof(data))

    assert pump.on_hook(network_frozen)
    assert not instance.policy.active
    assert stops == ["controller network loop stopped responding"]
    assert not events
    assert pump.on_hook(lambda: None)
    assert pump.cursor == (100, 200)


def test_activation_failure_restores_cursor_and_allows_an_explicit_retry(capture):
    instance, pump, events, stops = capture
    native_set_cursor = pump.module.U.SetCursorPos.call
    pump.module.U.SetCursorPos.call = lambda *args: 0
    instance.set_active(True)
    assert pump.on_hook(lambda: None)
    assert not instance.policy.active
    assert stops == ["Windows could not activate input capture; check the unlocked desktop"]
    assert instance.thread.is_alive()
    pump.module.U.SetCursorPos.call = native_set_cursor
    instance.set_active(True)
    assert pump.on_hook(lambda: None)
    assert instance.policy.active and not events
