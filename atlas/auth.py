"""Investor authentication: scrypt password hashing + HMAC-signed, expiring session tokens (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from .config import DATA_DIR, settings
from .db import DB, now

SESSION_TTL = 12 * 3600


def _secret() -> bytes:
    if settings.secret_key:
        return settings.secret_key.encode()
    f = DATA_DIR / ".secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip().encode()


def hash_pw(pw: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return h.hex(), salt.hex()


def create_user(db: DB, email: str, pw: str) -> int:
    if len(pw) < 12:
        raise ValueError("password must be at least 12 characters")
    h, s = hash_pw(pw)
    return db.x("INSERT INTO users(email,pw_hash,salt,created_at) VALUES(?,?,?,?)", (email.lower(), h, s, now()))


def verify(db: DB, email: str, pw: str) -> bool:
    r = db.q("SELECT pw_hash, salt FROM users WHERE email=?", (email.lower(),))
    if not r:
        hash_pw(pw)  # constant-ish time for unknown users
        return False
    h, _ = hash_pw(pw, bytes.fromhex(r[0]["salt"]))
    return hmac.compare_digest(h, r[0]["pw_hash"])


def issue(email: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"sub": email, "exp": int(time.time()) + SESSION_TTL}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def check(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    data = json.loads(base64.urlsafe_b64decode(body.encode()))
    return data["sub"] if data["exp"] > time.time() else None
