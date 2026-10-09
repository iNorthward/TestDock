"""RFC 6238 TOTP（与 trade-node TotpUtil 一致：HMAC-SHA1、30s、6 位）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

STEP_SECONDS = 30
DIGITS = 6


def _decode_secret(secret: str) -> bytes:
    normalized = secret.replace(" ", "").replace("-", "").strip().upper()
    pad = (8 - len(normalized) % 8) % 8
    return base64.b32decode(normalized + "=" * pad)


def _hotp(key: bytes, counter: int) -> int:
    data = struct.pack(">Q", counter)
    digest = hmac.new(key, data, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return truncated % (10**DIGITS)


def totp_generate(secret: str, time_ms: int | None = None) -> str:
    if not secret or not str(secret).strip():
        raise ValueError("TOTP secret 为空")
    tms = int(time.time() * 1000) if time_ms is None else int(time_ms)
    key = _decode_secret(secret)
    counter = (tms // 1000) // STEP_SECONDS
    return f"{_hotp(key, counter):0{DIGITS}d}"


def totp_remain_sec(time_ms: int | None = None) -> int:
    tms = int(time.time() * 1000) if time_ms is None else int(time_ms)
    now_sec = tms // 1000
    return STEP_SECONDS - (now_sec % STEP_SECONDS)


def totp_live(secret: str, time_ms: int | None = None) -> dict:
    """返回当前验证码与剩余秒数（不向调用方外泄 secret）。"""
    tms = int(time.time() * 1000) if time_ms is None else int(time_ms)
    return {
        "code": totp_generate(secret, tms),
        "remainSec": totp_remain_sec(tms),
        "stepSec": STEP_SECONDS,
    }
