"""Win32 console input. Hooks run on a dedicated, continuously pumped thread."""
import ctypes as C
import threading
import time
from ctypes import wintypes as W

from ..capture import CapturePolicy
from ..protocol import Op

U = C.WinDLL("user32", use_last_error=True)
K = C.WinDLL("kernel32", use_last_error=True)
LRESULT = C.c_ssize_t
ULONG_PTR = C.c_size_t
HOOKPROC = C.WINFUNCTYPE(LRESULT, C.c_int, W.WPARAM, W.LPARAM)
TAG = 0x52494E50
WM_CAPTURE_STATE = 0x8001


class KBD(C.Structure):
    _fields_ = [("vk", W.DWORD), ("scan", W.DWORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", ULONG_PTR)]


class MOUSE(C.Structure):
    _fields_ = [("pt", W.POINT), ("data", W.DWORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("vk", W.WORD), ("scan", W.WORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", ULONG_PTR)]


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("data", W.DWORD),
                ("flags", W.DWORD), ("time", W.DWORD), ("extra", ULONG_PTR)]


class INPUTUNION(C.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", INPUTUNION)]


U.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
U.SendInput.restype = W.UINT
U.SetWindowsHookExW.argtypes = [C.c_int, HOOKPROC, W.HINSTANCE, W.DWORD]
U.SetWindowsHookExW.restype = W.HANDLE
U.CallNextHookEx.argtypes = [W.HANDLE, C.c_int, W.WPARAM, W.LPARAM]
U.CallNextHookEx.restype = LRESULT
U.UnhookWindowsHookEx.argtypes = [W.HANDLE]
U.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
U.GetMessageW.restype = W.BOOL
U.TranslateMessage.argtypes = [C.POINTER(W.MSG)]
U.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
U.DispatchMessageW.restype = LRESULT
U.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
U.SetTimer.argtypes = [W.HWND, ULONG_PTR, W.UINT, C.c_void_p]
U.SetTimer.restype = ULONG_PTR
U.KillTimer.argtypes = [W.HWND, ULONG_PTR]
U.GetCursorPos.argtypes = [C.POINTER(W.POINT)]
U.SetCursorPos.argtypes = [C.c_int, C.c_int]
U.OpenInputDesktop.argtypes = [W.DWORD, W.BOOL, W.DWORD]
U.OpenInputDesktop.restype = W.HANDLE
U.CloseDesktop.argtypes = [W.HANDLE]
K.GetModuleHandleW.argtypes = [W.LPCWSTR]
K.GetModuleHandleW.restype = W.HMODULE
K.GetCurrentThreadId.restype = W.DWORD


class WindowsInjector:
    def permitted(self):
        desktop = U.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP, interactive desktop only
        if desktop:
            U.CloseDesktop(desktop)
            return True
        return False

    def ensure_permissions(self):
        if not self.permitted():
            raise RuntimeError("Unlock/sign in to the Windows desktop, then start the target again.")
        print("Desktop input ready. Windows UAC/secure desktops are controlled locally.", flush=True)

    @staticmethod
    def _send(value):
        if U.SendInput(1, C.byref(value), C.sizeof(INPUT)) != 1:
            raise RuntimeError("Windows refused input; check desktop access and application elevation.")

    def key(self, key, down):
        extended = key in (0xA3, 0xA5, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27,
                           0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x6F, 0x90)
        value = INPUT(type=1)
        value.ki = KEYBDINPUT(key, 0, (0 if down else 2) | int(extended), 0, TAG)
        self._send(value)

    def _mouse(self, dx=0, dy=0, flags=0, data=0):
        value = INPUT(type=0)
        value.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, TAG)
        self._send(value)

    def move(self, dx, dy):
        self._mouse(dx, dy, 0x0001)

    def button(self, button, down):
        flags = {1: (0x2, 0x4), 2: (0x8, 0x10), 3: (0x20, 0x40),
                 4: (0x80, 0x100), 5: (0x80, 0x100)}
        self._mouse(flags=flags[button][0 if down else 1], data=button - 3 if button >= 4 else 0)

    def scroll(self, horizontal, vertical):
        if vertical:
            self._mouse(flags=0x0800, data=vertical)
        if horizontal:
            self._mouse(flags=0x1000, data=horizontal)


class WindowsCapture:
    def __init__(self, emit, toggle, stop):
        self.policy = CapturePolicy(emit, toggle, stop)
        self.injector = WindowsInjector()
        self.ready = threading.Event()
        self.error = None
        self.last_pump = 0.0
        self.last_network_tick = 0.0
        self.thread_id = 0
        self.origin = None
        self.anchor = None
        self.requested_active = False
        self.state_version = 0
        self.state_pending = False
        self.thread = threading.Thread(target=self._run, name="windows-input-hooks", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError("Windows input hooks could not start. Run in an unlocked desktop session.")

    def healthy(self):
        return self.thread.is_alive() and time.monotonic() - self.last_pump < .5

    def network_tick(self):
        self.last_network_tick = time.monotonic()

    def set_active(self, active):
        # Never call cursor/SendInput APIs from the network thread while holding
        # the policy lock: those APIs can wait for this process's hook callback.
        # One posted message applies the latest requested state on the hook thread.
        with self.policy.lock:
            self.requested_active = active
            self.state_version += 1
            if not active:
                self.policy.pause()  # Restore ordinary local input immediately.
            if not self.thread_id or not self.thread.is_alive():
                if active:
                    raise RuntimeError("Input capture is unavailable.")
                return
            if self.state_pending:
                return
            self.state_pending = True
            if not U.PostThreadMessageW(self.thread_id, WM_CAPTURE_STATE, 0, 0):
                self.state_pending = False
                self.requested_active = False
                self.policy.pause()
                if active:
                    raise RuntimeError("Could not notify the Windows input thread.")

    def _restore_cursor(self):
        origin = self.origin
        self.origin = self.anchor = None
        if origin:
            U.SetCursorPos(*origin)

    def _apply_state(self):
        with self.policy.lock:
            self.state_pending = False
            version = self.state_version
            if self.requested_active:
                if self.policy.active:
                    return
                if not self.healthy() or not self.injector.permitted():
                    raise RuntimeError("Input capture is unavailable.")
                # A pause followed quickly by activation may share one wakeup.
                self._restore_cursor()
                point = W.POINT()
                if not U.GetCursorPos(C.byref(point)):
                    raise RuntimeError("Could not access the cursor.")
                self.origin = (point.x, point.y)
                # A fixed cursor in the screen interior cannot reach an edge while suppressed.
                self.anchor = (U.GetSystemMetrics(0) // 2, U.GetSystemMetrics(1) // 2)
                if not U.SetCursorPos(*self.anchor):
                    raise RuntimeError("Could not anchor the cursor.")
                # Ctrl/Alt may have reached the local app before the activation chord.
                for key in list(self.policy.keys):
                    self.injector.key(key, False)
                # A reentrant hotkey callback can cancel this activation while
                # a native API dispatches messages on the same hook thread.
                if self.requested_active and self.state_version == version:
                    self.policy.activate()
                else:
                    self._restore_cursor()
            else:
                self.policy.pause()
                self._restore_cursor()

    def _pump_tick(self):
        # GetMessage can dispatch many sent hook callbacks without returning a
        # posted message or low-priority WM_TIMER. Those callbacks are progress too.
        self.last_pump = time.monotonic()
        if self.policy.active and self.last_pump - self.last_network_tick > .65:
            self.set_active(False)
            self.policy.stop("controller network loop stopped responding")

    def stop(self):
        self.set_active(False)
        if self.thread_id:
            U.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
        self.thread.join(timeout=2)

    def _key(self, code, message, address):
        self._pump_tick()
        if code >= 0:
            data = C.cast(address, C.POINTER(KBD)).contents
            if not data.flags & 0x10:  # Never reflect injected input back to the target.
                try:
                    key = data.vk
                    if key == 0x10:
                        key = 0xA1 if data.scan == 0x36 else 0xA0
                    elif key == 0x11:
                        key = 0xA3 if data.flags & 1 else 0xA2
                    elif key == 0x12:
                        key = 0xA5 if data.flags & 1 else 0xA4
                    if self.policy.key(key, message in (0x100, 0x104)):
                        return 1
                except Exception:
                    self.policy.pause()
                    self.policy.stop("Windows keyboard capture failed")
        return U.CallNextHookEx(None, code, message, address)

    def _mouse(self, code, message, address):
        self._pump_tick()
        if code >= 0:
            data = C.cast(address, C.POINTER(MOUSE)).contents
            if not data.flags & 1:
                try:
                    if message == 0x200 and self.policy.active and self.anchor:
                        dx, dy = data.pt.x - self.anchor[0], data.pt.y - self.anchor[1]
                        if dx or dy:
                            self.policy.mouse(Op.MOVE, dx, dy)
                        return 1
                    buttons = {0x201: (1, 1), 0x202: (1, 0), 0x204: (2, 1),
                               0x205: (2, 0), 0x207: (3, 1), 0x208: (3, 0)}
                    if message in (0x20B, 0x20C):
                        args = (4 if data.data >> 16 == 1 else 5, int(message == 0x20B))
                    else:
                        args = buttons.get(message)
                    if args and self.policy.mouse(Op.BUTTON, *args):
                        return 1
                    if message in (0x20A, 0x20E):
                        delta = C.c_short(data.data >> 16).value
                        if self.policy.mouse(Op.SCROLL, delta if message == 0x20E else 0,
                                             delta if message == 0x20A else 0):
                            return 1
                    if self.policy.active:
                        return 1
                except Exception:
                    self.policy.pause()
                    self.policy.stop("Windows mouse capture failed")
        return U.CallNextHookEx(None, code, message, address)

    def _run(self):
        hooks = []
        timer = 0
        try:
            self.thread_id = K.GetCurrentThreadId()
            self.key_callback, self.mouse_callback = HOOKPROC(self._key), HOOKPROC(self._mouse)
            module = K.GetModuleHandleW(None)
            hooks = [U.SetWindowsHookExW(13, self.key_callback, module, 0),
                     U.SetWindowsHookExW(14, self.mouse_callback, module, 0)]
            if not all(hooks):
                raise RuntimeError("Cannot install input hooks")
            timer = U.SetTimer(None, 0, 100, None)
            if not timer:
                raise RuntimeError("Cannot monitor hook thread")
            self.last_pump = time.monotonic()
            self.ready.set()
            message = W.MSG()
            while True:
                result = U.GetMessageW(C.byref(message), None, 0, 0)
                if result <= 0:
                    break
                self._pump_tick()
                if message.message == WM_CAPTURE_STATE:
                    try:
                        self._apply_state()
                    except Exception:
                        self.set_active(False)
                        self.policy.stop("Windows could not activate input capture; check the unlocked desktop")
                    continue
                U.TranslateMessage(C.byref(message))
                U.DispatchMessageW(C.byref(message))
        except Exception as error:
            self.error = type(error).__name__
            self.ready.set()
        finally:
            self.policy.pause()
            self.requested_active = False
            self._restore_cursor()
            if timer:
                U.KillTimer(None, timer)
            for hook in hooks:
                if hook:
                    U.UnhookWindowsHookEx(hook)
            self.thread_id = 0
