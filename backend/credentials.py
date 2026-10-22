"""Windows user-scoped credential protection; no plaintext storage fallback.

Historical app-before-*.db backups and SQLite free pages/WAL may still contain
old plaintext. Migration deliberately preserves user backups; protect those
files separately. DPAPI data is tied to the current Windows user profile.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import sqlite3

PREFIX = "dpapi:v1:"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("Windows DPAPI 凭据保护失败；原有凭据未更改")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def encrypt_credential(value: str) -> str:
    return PREFIX + base64.b64encode(_crypt(value.encode("utf-8"), decrypt=False)).decode("ascii")


def decrypt_credential(value: str) -> str:
    if not value.startswith(PREFIX):
        raise RuntimeError("凭据尚未迁移为 Windows DPAPI 保护格式")
    return _crypt(base64.b64decode(value[len(PREFIX):], validate=True), decrypt=True).decode("utf-8")


def migrate_credentials(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT name,value FROM credentials").fetchall():
        if not row["value"].startswith(PREFIX):
            encrypted = encrypt_credential(row["value"])
            connection.execute("UPDATE credentials SET value=? WHERE name=?", (encrypted, row["name"]))
