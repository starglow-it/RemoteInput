"""Explicit native secret storage; unsupported/locked vaults fail closed."""
import ctypes as C
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path


class Vault:
    def __init__(self, relay, role):
        self.account = role + "-" + hashlib.sha256(relay.encode()).hexdigest()[:24]
        if sys.platform == "win32":
            self.backend = DPAPI(self.account)
        elif sys.platform == "darwin":
            self.backend = Keychain(self.account)
        else:
            raise RuntimeError("Credential storage requires Windows DPAPI or macOS Keychain.")

    def load(self):
        raw = self.backend.read()
        return json.loads(raw) if raw else None

    def save(self, value):
        self.backend.write(json.dumps(value, separators=(",", ":")).encode())

    def delete(self):
        self.backend.delete()


class DPAPI:
    def __init__(self, account):
        from ctypes import wintypes as W

        class Blob(C.Structure):
            _fields_ = [("size", W.DWORD), ("data", C.POINTER(C.c_ubyte))]

        self.Blob = Blob
        self.crypt = C.WinDLL("crypt32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.crypt.CryptProtectData.argtypes = [C.POINTER(Blob), W.LPCWSTR, C.c_void_p,
                                               C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob)]
        self.crypt.CryptUnprotectData.argtypes = [C.POINTER(Blob), C.c_void_p, C.c_void_p,
                                                 C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob)]
        self.crypt.CryptProtectData.restype = self.crypt.CryptUnprotectData.restype = W.BOOL
        self.kernel.LocalFree.argtypes = [C.c_void_p]
        self.kernel.LocalFree.restype = C.c_void_p
        self.path = Path(os.environ["LOCALAPPDATA"]) / "RemoteInput" / (account + ".bin")

    def _transform(self, raw, decrypt=False):
        buf = C.create_string_buffer(raw)
        src = self.Blob(len(raw), C.cast(buf, C.POINTER(C.c_ubyte)))
        dest = self.Blob()
        fn = self.crypt.CryptUnprotectData if decrypt else self.crypt.CryptProtectData
        # CRYPTPROTECT_UI_FORBIDDEN; user-bound, never machine-wide protection.
        if not fn(C.byref(src), None, None, None, None, 1, C.byref(dest)):
            raise RuntimeError("Windows could not unlock RemoteInput credentials for this user.")
        try:
            return C.string_at(dest.data, dest.size)
        finally:
            self.kernel.LocalFree(dest.data)

    def read(self):
        return self._transform(self.path.read_bytes(), True) if self.path.exists() else None

    def write(self, value):
        encrypted = self._transform(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix("." + secrets.token_hex(6) + ".tmp")
        try:
            with tmp.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self):
        self.path.unlink(missing_ok=True)


class Keychain:
    """Generic-password items in the user's default login Keychain (no subprocess secrets)."""

    def __init__(self, account):
        self.account = account.encode()
        self.service = b"RemoteInput"
        self.sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cf.CFRelease.argtypes = [C.c_void_p]
        self.sec.SecKeychainFindGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p,
            C.c_uint32, C.c_char_p, C.POINTER(C.c_uint32), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)]
        self.sec.SecKeychainAddGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p,
            C.c_uint32, C.c_char_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)]
        self.sec.SecKeychainItemModifyAttributesAndData.argtypes = [C.c_void_p, C.c_void_p,
                                                                  C.c_uint32, C.c_void_p]
        self.sec.SecKeychainItemDelete.argtypes = [C.c_void_p]
        self.sec.SecKeychainItemFreeContent.argtypes = [C.c_void_p, C.c_void_p]

    def _find(self):
        length, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        status = self.sec.SecKeychainFindGenericPassword(None, len(self.service), self.service,
            len(self.account), self.account, C.byref(length), C.byref(data), C.byref(item))
        if status == -25300:
            return None, None
        if status:
            raise RuntimeError(f"Unlock the login Keychain and allow RemoteInput access (status {status}).")
        try:
            return C.string_at(data, length.value), item
        finally:
            self.sec.SecKeychainItemFreeContent(None, data)

    def read(self):
        raw, item = self._find()
        if item:
            self.cf.CFRelease(item)
        return raw

    def write(self, raw):
        _, item = self._find()
        try:
            if item:
                status = self.sec.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
            else:
                status = self.sec.SecKeychainAddGenericPassword(None, len(self.service), self.service,
                    len(self.account), self.account, len(raw), raw, None)
            if status:
                raise RuntimeError(f"Could not securely save credentials in Keychain (status {status}).")
        finally:
            if item:
                self.cf.CFRelease(item)

    def delete(self):
        _, item = self._find()
        if item:
            try:
                if self.sec.SecKeychainItemDelete(item):
                    raise RuntimeError("Could not remove the saved target from Keychain.")
            finally:
                self.cf.CFRelease(item)


def target_identity(vault):
    identity = vault.load()
    if not identity:
        identity = {"owner_secret": secrets.token_urlsafe(32), "password": secrets.token_urlsafe(24)}
        # Save before registration; a lost registration reply must not allocate another ID.
        vault.save(identity)
    return identity


def begin_rotation(vault, identity):
    if "pending_password" not in identity:
        identity = {**identity, "pending_password": secrets.token_urlsafe(24),
                    "rotation_id": secrets.token_urlsafe(24)}
        vault.save(identity)
    return identity


def finish_rotation(vault, identity):
    identity = {**identity, "password": identity["pending_password"]}
    del identity["pending_password"]
    del identity["rotation_id"]
    vault.save(identity)
    return identity

