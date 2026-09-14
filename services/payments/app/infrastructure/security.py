from collections import defaultdict
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone

import jwt

# ponytail: scrypt via stdlib hashlib instead of adding passlib/bcrypt —
# scrypt is an OWASP-endorsed password hashing KDF, no extra dependency needed.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me-before-any-real-deploy")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

LOGIN_RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "5"))
LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))
_login_attempts: dict[str, list[float]] = defaultdict(list)


class LoginRateLimitedError(Exception):
    pass


# ponytail: in-memory per-process sliding window keyed by client IP — fine
# for a single API instance; upgrade to a shared store (Redis) before
# running more than one replica, and consider a per-account key too.
def check_login_rate_limit(key: str) -> None:
    now = time.monotonic()
    attempts = _login_attempts[key]
    attempts[:] = [ts for ts in attempts if now - ts < LOGIN_RATE_LIMIT_WINDOW_SECONDS]
    if len(attempts) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
        raise LoginRateLimitedError("too many login attempts, try again later")


def record_login_failure(key: str) -> None:
    _login_attempts[key].append(time.monotonic())


def reset_login_attempts(key: str) -> None:
    _login_attempts.pop(key, None)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, hash_hex = stored_hash.split("$")
    except ValueError:
        return False
    if algorithm != "scrypt":
        return False
    expected = bytes.fromhex(hash_hex)
    derived = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt_hex),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=len(expected),
    )
    return hmac.compare_digest(derived, expected)


def create_access_token(user_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "type": "refresh", "iat": now, "exp": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
