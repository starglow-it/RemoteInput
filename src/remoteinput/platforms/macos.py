import ctypes as C
import subprocess
import time

from .keys import MAC_KEYS, MAC_MODIFIERS


class CGPoint(C.Structure):
    _fields_ = [("x", C.c_double), ("y", C.c_double)]


class CGSize(C.Structure):
    _fields_ = [("width", C.c_double), ("height", C.c_double)]


class CGRect(C.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


class MacInjector:
    def __init__(self):
        self.cg = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        signatures = {
            "CGPreflightPostEventAccess": (C.c_bool, []),
            "CGRequestPostEventAccess": (C.c_bool, []),
            "AXIsProcessTrusted": (C.c_bool, []),
            "CGEventCreate": (C.c_void_p, [C.c_void_p]),
            "CGEventGetLocation": (CGPoint, [C.c_void_p]),
            "CGEventCreateKeyboardEvent": (C.c_void_p, [C.c_void_p, C.c_uint16, C.c_bool]),
            "CGEventCreateMouseEvent": (C.c_void_p, [C.c_void_p, C.c_uint32, CGPoint, C.c_uint32]),
            "CGEventSetFlags": (None, [C.c_void_p, C.c_uint64]),
            "CGEventSetIntegerValueField": (None, [C.c_void_p, C.c_uint32, C.c_int64]),
            "CGEventPost": (None, [C.c_uint32, C.c_void_p]),
            "CGGetActiveDisplayList": (C.c_int32, [C.c_uint32, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32)]),
            "CGDisplayBounds": (CGRect, [C.c_uint32]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self.cg, name)
            function.restype, function.argtypes = restype, argtypes
        # Variadic function: specify fixed arguments; supply scroll deltas as explicit int32 values.
        self.cg.CGEventCreateScrollWheelEvent.restype = C.c_void_p
        self.cg.CGEventCreateScrollWheelEvent.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32]
        self.cf.CFRelease.argtypes = [C.c_void_p]
        self.keys, self.buttons = set(), set()
        self.scroll_x = self.scroll_y = 0
        self.clicks = {}

    def permitted(self):
        return bool(self.cg.CGPreflightPostEventAccess() and self.cg.AXIsProcessTrusted())

    def ensure_permissions(self):
        if self.permitted():
            return
        print("Allow RemoteInput (or Terminal when running from Terminal) in System Settings > "
              "Privacy & Security > Accessibility. Screen Recording permission is not required.", flush=True)
        self.cg.CGRequestPostEventAccess()
        subprocess.run(["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                       check=False)
        while not self.permitted():
            input("After enabling Accessibility, press Enter to check again (Ctrl+C to quit): ")
        print("Accessibility permission confirmed.", flush=True)

    def _flags(self):
        value = 0
        for key in self.keys:
            value |= MAC_MODIFIERS.get(key, 0)
        return value

    def _post(self, event):
        if not event:
            raise RuntimeError("macOS could not create the input event.")
        try:
            self.cg.CGEventSetFlags(event, self._flags())
            self.cg.CGEventPost(0, event)  # kCGHIDEventTap
        finally:
            self.cf.CFRelease(event)

    def position(self):
        event = self.cg.CGEventCreate(None)
        if not event:
            raise RuntimeError("macOS could not read the cursor position.")
        try:
            return self.cg.CGEventGetLocation(event)
        finally:
            self.cf.CFRelease(event)

    def key(self, key, down):
        if key not in MAC_KEYS:
            raise RuntimeError("This key has no macOS mapping; input paused.")
        repeat = down and key in self.keys
        (self.keys.add if down else self.keys.discard)(key)
        event = self.cg.CGEventCreateKeyboardEvent(None, MAC_KEYS[key], down)
        if event:
            self.cg.CGEventSetIntegerValueField(event, 8, int(repeat))  # kCGKeyboardEventAutorepeat
        self._post(event)

    def supports_key(self, key):
        return key in MAC_KEYS

    @staticmethod
    def native_button(button):
        return {1: 0, 2: 1, 3: 2, 4: 3, 5: 4}[button]

    def _clamp(self, point):
        ids, count = (C.c_uint32 * 32)(), C.c_uint32()
        if self.cg.CGGetActiveDisplayList(32, ids, C.byref(count)):
            return point
        candidates = []
        for display in ids[:count.value]:
            rect = self.cg.CGDisplayBounds(display)
            x = min(max(point.x, rect.origin.x), rect.origin.x + rect.size.width - 1)
            y = min(max(point.y, rect.origin.y), rect.origin.y + rect.size.height - 1)
            candidates.append(((x - point.x) ** 2 + (y - point.y) ** 2, x, y))
        if not candidates:
            return point
        _, x, y = min(candidates)
        return CGPoint(x, y)

    def move(self, dx, dy):
        point = self.position()
        point = self._clamp(CGPoint(point.x + dx, point.y + dy))
        button = min(self.buttons) if self.buttons else 1
        event_type = (6 if button == 1 else 7 if button == 2 else 27) if self.buttons else 5
        self._post(self.cg.CGEventCreateMouseEvent(None, event_type, point, self.native_button(button)))

    def button(self, button, down):
        point = self.position()
        (self.buttons.add if down else self.buttons.discard)(button)
        event_type = {1: (1, 2), 2: (3, 4)}.get(button, (25, 26))[0 if down else 1]
        event = self.cg.CGEventCreateMouseEvent(None, event_type, point, self.native_button(button))
        now = time.monotonic()
        previous = self.clicks.get(button)
        count = 1
        if down:
            if previous and now - previous[0] < .5 and abs(point.x - previous[1]) < 4 and abs(point.y - previous[2]) < 4:
                count = previous[3] + 1
            self.clicks[button] = (now, point.x, point.y, count)
        elif previous:
            count = previous[3]
        if event:
            self.cg.CGEventSetIntegerValueField(event, 1, count)
        self._post(event)

    def scroll(self, horizontal, vertical):
        self.scroll_x += horizontal
        self.scroll_y += vertical
        x, y = int(self.scroll_x / 120), int(self.scroll_y / 120)
        self.scroll_x -= x * 120
        self.scroll_y -= y * 120
        if x or y:
            self._post(self.cg.CGEventCreateScrollWheelEvent(None, 1, 2, C.c_int32(y), C.c_int32(-x)))
