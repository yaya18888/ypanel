"""RFC 6238 TOTP (SHA1, 6 digits, 30s) + recovery codes — stdlib only."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote


def new_secret():
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def hotp(secret, counter, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


def verify(secret, code, last_step=0, window=1, now=None):
    """Return the matched time-step (for replay protection) or None."""
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != 6:
        return None
    cur = int((now if now is not None else time.time()) // 30)
    for step in range(cur - window, cur + window + 1):
        if step > (last_step or 0) and hmac.compare_digest(hotp(secret, step), code):
            return step
    return None


def uri(secret, account, issuer="YPanel"):
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


def recovery_codes(n=8):
    return [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]


def hash_code(code):
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()
