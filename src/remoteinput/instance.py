import ctypes as C
import hashlib
import os
import sys
from pathlib import Path


class SingleInstance:
    def __init__(self, role, url):
        self.name = role + "-" + hashlib.sha256(url.encode()).hexdigest()[:24]
        self.handle = None

    def __enter__(self):
        if sys.platform == "win32":
            from ctypes import wintypes as W
            self.kernel = C.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
            self.kernel.CreateMutexW.restype = W.HANDLE
            self.kernel.CloseHandle.argtypes = [W.HANDLE]
            self.handle = self.kernel.CreateMutexW(None, False, "Local\\RemoteInput-" + self.name)
            if not self.handle or C.get_last_error() == 183:
                if self.handle:
                    self.kernel.CloseHandle(self.handle)
                self.handle = None
                raise RuntimeError("This RemoteInput role is already running for this relay.")
        else:
            import fcntl
            path = Path.home() / "Library" / "Application Support" / "RemoteInput" / (self.name + ".lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = path.open("a")
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.handle.close()
                self.handle = None
                raise RuntimeError("This RemoteInput role is already running for this relay.") from None
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            if os.name == "nt":
                self.kernel.CloseHandle(self.handle)
            else:
                self.handle.close()

