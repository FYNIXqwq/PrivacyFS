"""OS-backed protection for D4 state; no custom encryption algorithm."""
from __future__ import annotations

import base64
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _windows_blob_type():
    """Reuse the ctypes type: POINTER caches each distinct Structure forever."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    return Blob


class KeyProtector:
    name = "acl-only"

    def protect(self, payload: bytes) -> bytes:
        return payload

    def unprotect(self, payload: bytes) -> bytes:
        return payload


class WindowsDPAPI(KeyProtector):
    name = "windows-dpapi-user"

    def _crypt(self, payload, decrypt):
        import ctypes
        from ctypes import wintypes
        Blob = _windows_blob_type()
        buffer = ctypes.create_string_buffer(payload)
        source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if decrypt:
            call = crypt.CryptUnprotectData
            call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        else:
            call = crypt.CryptProtectData
            call.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        call.restype = wintypes.BOOL
        if not call(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError("operating-system state protection failed")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def protect(self, payload):
        return self._crypt(payload, False)

    def unprotect(self, payload):
        return self._crypt(payload, True)


def protector(name=None):
    name = name or ("windows-dpapi-user" if os.name == "nt" else "acl-only")
    if name == "windows-dpapi-user" and os.name == "nt":
        return WindowsDPAPI()
    if name == "acl-only":
        return KeyProtector()
    raise ValueError("state key provider unavailable on this host")


def wrapped_key(key):
    provider = protector()
    blob = provider.protect(key)
    if provider.unprotect(blob) != key:
        raise ValueError("state key roundtrip failed")
    return {"schema_version": "d4-key-1", "provider": provider.name, "wrapped_key": base64.b64encode(blob).decode("ascii")}


def unwrap_key(data):
    if set(data) != {"schema_version", "provider", "wrapped_key"} or data["schema_version"] != "d4-key-1":
        raise ValueError("invalid protected state key")
    key = protector(data["provider"]).unprotect(base64.b64decode(data["wrapped_key"], validate=True))
    if len(key) != 32:
        raise ValueError("invalid protected key length")
    return key
