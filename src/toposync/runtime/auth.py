from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Request, Response


RoleName = Literal["owner", "admin", "member", "guest", "service"]
CookieSecureMode = Literal["auto", "true", "false"]
DEFAULT_ACCESS_TTL_S = 30 * 60  # 30 minutes
DEFAULT_REFRESH_TTL_S = 90 * 24 * 3600  # 90 days
DEFAULT_PAIRING_TTL_S = 5 * 60  # 5 minutes
DEFAULT_REFRESH_ROTATION_GRACE_S = 30  # seconds


def _now() -> float:
    return float(time.time())


def _uuid() -> str:
    return uuid.uuid4().hex


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((4 - (len(data) % 4)) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def _hash_password(password: str) -> str:
    pwd = str(password or "")
    if len(pwd) < 8:
        raise ValueError("Password must have at least 8 characters")
    salt = secrets.token_bytes(16)
    rounds_default = 2**14
    block_default = 8
    parallel_default = 1

    rounds = rounds_default
    rounds_raw = str(os.getenv("TOPOSYNC_AUTH_SCRYPT_N") or "").strip()
    if rounds_raw:
        try:
            parsed = int(rounds_raw)
            if parsed >= 2**14 and parsed <= 2**18 and (parsed & (parsed - 1)) == 0:
                rounds = parsed
        except Exception:
            rounds = rounds_default

    block_size = block_default
    block_raw = str(os.getenv("TOPOSYNC_AUTH_SCRYPT_R") or "").strip()
    if block_raw:
        try:
            parsed = int(block_raw)
            if 1 <= parsed <= 16:
                block_size = parsed
        except Exception:
            block_size = block_default

    parallel = parallel_default
    parallel_raw = str(os.getenv("TOPOSYNC_AUTH_SCRYPT_P") or "").strip()
    if parallel_raw:
        try:
            parsed = int(parallel_raw)
            if 1 <= parsed <= 8:
                parallel = parsed
        except Exception:
            parallel = parallel_default

    key = hashlib.scrypt(
        pwd.encode("utf-8"),
        salt=salt,
        n=rounds,
        r=block_size,
        p=parallel,
        dklen=64,
    )
    return "scrypt${rounds}${block_size}${parallel}${salt_b64}${key_b64}".format(
        rounds=rounds,
        block_size=block_size,
        parallel=parallel,
        salt_b64=_b64url_encode(salt),
        key_b64=_b64url_encode(key),
    )


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, rounds_raw, block_raw, parallel_raw, salt_b64, key_b64 = str(stored_hash or "").split(
            "$", 5
        )
        if algo != "scrypt":
            return False
        rounds = int(rounds_raw)
        block_size = int(block_raw)
        parallel = int(parallel_raw)
        if rounds < 2**14 or rounds > 2**18:
            return False
        if (rounds & (rounds - 1)) != 0:
            return False
        if block_size < 1 or block_size > 16:
            return False
        if parallel < 1 or parallel > 8:
            return False
        salt = _b64url_decode(salt_b64)
        expected = _b64url_decode(key_b64)
        if len(salt) < 8 or len(salt) > 32:
            return False
        if len(expected) < 32 or len(expected) > 128:
            return False
        derived = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=salt,
            n=rounds,
            r=block_size,
            p=parallel,
            dklen=len(expected),
        )
    except Exception:
        return False
    return hmac.compare_digest(derived, expected)


def _parse_mode(raw: str | None) -> str:
    mode = str(raw or "").strip().lower()
    if mode in {"bypass", "off", "disabled"}:
        return "bypass"
    return "enforced"


def _parse_cookie_secure_mode(raw: str | None) -> CookieSecureMode:
    value = str(raw or "").strip().lower()
    if not value or value == "auto":
        return "auto"
    if value in {"1", "true", "yes", "on"}:
        return "true"
    if value in {"0", "false", "no", "off"}:
        return "false"
    return "auto"


def _request_is_https(request: Request | None) -> bool:
    if request is None:
        return False
    forwarded = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded in {"https", "http"}:
        return forwarded == "https"
    try:
        return str(request.url.scheme or "").lower() == "https"
    except Exception:
        return False


def _parse_bool_env(raw: str | None, default: bool) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return bool(default)
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _parse_json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        out.append(text)
    return out


def _normalize_selector(selector: str) -> str:
    return str(selector or "").strip()


def _normalize_pairing_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _new_pairing_code(length: int = 8) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(max(6, int(length))))


def _selector_matches(selector: str, target: str) -> bool:
    s = _normalize_selector(selector)
    t = _normalize_selector(target)
    if not s:
        return False
    if s == "*":
        return True
    if s.endswith(".*") and t.startswith(s[:-1]):
        return True
    return fnmatch.fnmatchcase(t, s)


@dataclass(frozen=True, slots=True)
class AuthUser:
    id: str
    username: str
    display_name: str
    role: RoleName
    password_hash: str
    password_updated_at: float
    is_disabled: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class GrantRule:
    id: str
    user_id: str
    action: str
    resource_type: str
    include: list[str]
    exclude: list[str]
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: str
    username: str
    display_name: str
    role: RoleName
    bypass: bool = False


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal: AuthPrincipal | None
    mode: str
    requires_setup: bool
    cookies_to_set: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RefreshSession:
    token_id: str
    user: AuthUser
    expires_at: float


@dataclass(frozen=True, slots=True)
class RefreshSessionInfo:
    id: str
    user_id: str
    device_label: str
    created_at: float
    last_used_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class PairingSession:
    user: AuthUser
    device_label: str
    expires_at: float


class AuthStore:
    _INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS auth_meta (
  key          TEXT PRIMARY KEY,
  value        TEXT NOT NULL,
  updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_user (
  id                  TEXT PRIMARY KEY,
  username            TEXT NOT NULL,
  username_lc         TEXT NOT NULL UNIQUE,
  display_name        TEXT NOT NULL,
  role                TEXT NOT NULL,
  password_hash       TEXT NOT NULL,
  password_updated_at REAL NOT NULL,
  is_disabled         INTEGER NOT NULL DEFAULT 0,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_refresh_token (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  token_hash    TEXT NOT NULL UNIQUE,
  device_label  TEXT NOT NULL,
  created_at    REAL NOT NULL,
  expires_at    REAL NOT NULL,
  last_used_at  REAL NOT NULL,
  revoked_at    REAL,
  rotated_from  TEXT,
  FOREIGN KEY(user_id) REFERENCES auth_user(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auth_grant (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  action        TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  include_json  TEXT NOT NULL,
  exclude_json  TEXT NOT NULL,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  UNIQUE(user_id, action, resource_type),
  FOREIGN KEY(user_id) REFERENCES auth_user(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_user ON auth_refresh_token(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_grant_user ON auth_grant(user_id);

CREATE TABLE IF NOT EXISTS auth_pairing_code (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  code_hash     TEXT NOT NULL UNIQUE,
  device_label  TEXT NOT NULL,
  created_at    REAL NOT NULL,
  expires_at    REAL NOT NULL,
  consumed_at   REAL,
  FOREIGN KEY(user_id) REFERENCES auth_user(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_pairing_user ON auth_pairing_code(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_pairing_expires ON auth_pairing_code(expires_at);
"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(self._INIT_SQL)
        try:
            if os.name != "nt" and self._db_path.is_file():
                self._db_path.chmod(0o600)
        except Exception:
            pass

    def _row_to_user(self, row: sqlite3.Row | None) -> AuthUser | None:
        if row is None:
            return None
        return AuthUser(
            id=str(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            password_hash=str(row["password_hash"]),
            password_updated_at=float(row["password_updated_at"] or 0.0),
            is_disabled=bool(int(row["is_disabled"] or 0)),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )

    def _row_to_grant(self, row: sqlite3.Row) -> GrantRule:
        return GrantRule(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            action=str(row["action"]),
            resource_type=str(row["resource_type"]),
            include=_parse_json_list(str(row["include_json"] or "[]")),
            exclude=_parse_json_list(str(row["exclude_json"] or "[]")),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )

    def count_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM auth_user").fetchone()
        return int(row["c"] if row else 0)

    def get_or_create_secret(self, key: str) -> str:
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM auth_meta WHERE key = ? LIMIT 1", (key,)
            ).fetchone()
            if row is not None:
                return str(row["value"])
            secret = secrets.token_urlsafe(48)
            self._conn.execute(
                "INSERT INTO auth_meta(key, value, updated_at) VALUES(?, ?, ?)",
                (key, secret, now),
            )
        return secret

    def get_user_by_id(self, user_id: str) -> AuthUser | None:
        user_id = str(user_id or "").strip()
        if not user_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM auth_user WHERE id = ? LIMIT 1", (user_id,)
            ).fetchone()
        return self._row_to_user(row)

    def get_user_by_username(self, username: str) -> AuthUser | None:
        username_lc = str(username or "").strip().lower()
        if not username_lc:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM auth_user WHERE username_lc = ? LIMIT 1", (username_lc,)
            ).fetchone()
        return self._row_to_user(row)

    def bootstrap_owner(self, *, username: str, display_name: str, password: str) -> AuthUser:
        uname = str(username or "").strip()
        if len(uname) < 3:
            raise ValueError("Username must have at least 3 characters")
        display = str(display_name or "").strip() or uname
        pwd_hash = _hash_password(password)
        now = _now()
        user = AuthUser(
            id=_uuid(),
            username=uname,
            display_name=display,
            role="owner",
            password_hash=pwd_hash,
            password_updated_at=now,
            is_disabled=False,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            if self.count_users() > 0:
                raise ValueError("Auth is already configured")
            self._conn.execute(
                """
                INSERT INTO auth_user(
                  id, username, username_lc, display_name, role,
                  password_hash, password_updated_at, is_disabled,
                  created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    user.id,
                    user.username,
                    user.username.lower(),
                    user.display_name,
                    user.role,
                    user.password_hash,
                    user.password_updated_at,
                    user.created_at,
                    user.updated_at,
                ),
            )
        return user

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        role: RoleName,
        password: str,
    ) -> AuthUser:
        uname = str(username or "").strip()
        if len(uname) < 3:
            raise ValueError("Username must have at least 3 characters")
        if role not in {"owner", "admin", "member", "guest", "service"}:
            raise ValueError("Invalid role")
        display = str(display_name or "").strip() or uname
        pwd_hash = _hash_password(password)
        now = _now()
        user = AuthUser(
            id=_uuid(),
            username=uname,
            display_name=display,
            role=role,
            password_hash=pwd_hash,
            password_updated_at=now,
            is_disabled=False,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            existing = self.get_user_by_username(uname)
            if existing is not None:
                raise ValueError("Username already exists")
            self._conn.execute(
                """
                INSERT INTO auth_user(
                  id, username, username_lc, display_name, role,
                  password_hash, password_updated_at, is_disabled,
                  created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    user.id,
                    user.username,
                    user.username.lower(),
                    user.display_name,
                    user.role,
                    user.password_hash,
                    user.password_updated_at,
                    user.created_at,
                    user.updated_at,
                ),
            )
        return user

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        role: RoleName | None = None,
        password: str | None = None,
        is_disabled: bool | None = None,
    ) -> AuthUser:
        user = self.get_user_by_id(user_id)
        if user is None:
            raise KeyError("Unknown user")

        next_display = str(display_name).strip() if display_name is not None else user.display_name
        if not next_display:
            next_display = user.username

        next_role: RoleName = role if role is not None else user.role
        if next_role not in {"owner", "admin", "member", "guest", "service"}:
            raise ValueError("Invalid role")

        next_disabled = bool(is_disabled) if is_disabled is not None else user.is_disabled

        next_pwd_hash = user.password_hash
        next_pwd_updated_at = user.password_updated_at
        if password is not None:
            next_pwd_hash = _hash_password(password)
            next_pwd_updated_at = _now()

        now = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE auth_user
                SET display_name = ?, role = ?, password_hash = ?, password_updated_at = ?,
                    is_disabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_display,
                    next_role,
                    next_pwd_hash,
                    next_pwd_updated_at,
                    1 if next_disabled else 0,
                    now,
                    user.id,
                ),
            )
            if password is not None:
                self._conn.execute(
                    "UPDATE auth_refresh_token SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user.id),
                )
        updated = self.get_user_by_id(user.id)
        if updated is None:
            raise RuntimeError("User disappeared")
        return updated

    def delete_user(self, user_id: str) -> None:
        uid = str(user_id or "").strip()
        if not uid:
            raise KeyError("Unknown user")
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM auth_user WHERE id = ? LIMIT 1", (uid,)
            ).fetchone()
            if row is None:
                raise KeyError("Unknown user")
            self._conn.execute("DELETE FROM auth_user WHERE id = ?", (uid,))

    def verify_credentials(self, username: str, password: str) -> AuthUser | None:
        user = self.get_user_by_username(username)
        if user is None or user.is_disabled:
            return None
        if not _verify_password(password, user.password_hash):
            return None
        return user

    def list_users(self) -> list[AuthUser]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM auth_user ORDER BY created_at ASC").fetchall()
        out: list[AuthUser] = []
        for row in rows:
            user = self._row_to_user(row)
            if user is not None:
                out.append(user)
        return out

    def active_sessions_count(self, user_id: str) -> int:
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM auth_refresh_token
                WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (user_id, now),
            ).fetchone()
        return int(row["c"] if row else 0)

    def issue_refresh_token(
        self, *, user_id: str, device_label: str, ttl_s: int
    ) -> tuple[str, float]:
        user = self.get_user_by_id(user_id)
        if user is None:
            raise KeyError("Unknown user")
        raw_token = secrets.token_urlsafe(54)
        token_hash = _sha256(raw_token)
        token_id = _uuid()
        now = _now()
        expires_at = now + max(60, int(ttl_s))
        device = str(device_label or "").strip()[:80]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO auth_refresh_token(
                  id, user_id, token_hash, device_label,
                  created_at, expires_at, last_used_at, revoked_at, rotated_from
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (token_id, user.id, token_hash, device, now, expires_at, now),
            )
        return raw_token, expires_at

    def list_refresh_sessions(self, user_id: str) -> list[RefreshSessionInfo]:
        uid = str(user_id or "").strip()
        if not uid:
            return []
        now = _now()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, user_id, device_label, created_at, last_used_at, expires_at
                FROM auth_refresh_token
                WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
                ORDER BY last_used_at DESC, created_at DESC
                """,
                (uid, now),
            ).fetchall()
        out: list[RefreshSessionInfo] = []
        for row in rows:
            out.append(
                RefreshSessionInfo(
                    id=str(row["id"]),
                    user_id=str(row["user_id"]),
                    device_label=str(row["device_label"] or "").strip() or "device",
                    created_at=float(row["created_at"] or 0.0),
                    last_used_at=float(row["last_used_at"] or 0.0),
                    expires_at=float(row["expires_at"] or 0.0),
                )
            )
        return out

    def _refresh_session_from_row(self, row: sqlite3.Row | None) -> RefreshSession | None:
        if row is None:
            return None
        user = self._row_to_user(row)
        if user is None:
            return None
        if bool(int(row["revoked_at"] is not None)):
            return None
        return RefreshSession(
            token_id=str(row["rt_id"]), user=user, expires_at=float(row["expires_at"] or 0.0)
        )

    def get_refresh_session(self, raw_refresh_token: str) -> RefreshSession | None:
        token_hash = _sha256(str(raw_refresh_token or ""))
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                  rt.id AS rt_id,
                  rt.expires_at AS expires_at,
                  rt.revoked_at AS revoked_at,
                  u.*
                FROM auth_refresh_token rt
                JOIN auth_user u ON u.id = rt.user_id
                WHERE rt.token_hash = ?
                LIMIT 1
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is not None:
                return None
            if float(row["expires_at"] or 0.0) <= now:
                return None
            self._conn.execute(
                "UPDATE auth_refresh_token SET last_used_at = ? WHERE id = ?",
                (now, str(row["rt_id"])),
            )
        session = self._refresh_session_from_row(row)
        if session is None:
            return None
        if session.user.is_disabled:
            return None
        return session

    def rotate_refresh_token(
        self,
        raw_refresh_token: str,
        *,
        ttl_s: int,
        rotation_grace_s: int = 0,
        device_label: str | None = None,
    ) -> tuple[RefreshSession, str, float] | None:
        token_hash = _sha256(str(raw_refresh_token or ""))
        now = _now()
        grace_s = max(0, int(rotation_grace_s))
        new_raw = secrets.token_urlsafe(54)
        new_hash = _sha256(new_raw)
        new_id = _uuid()
        expires_at = now + max(60, int(ttl_s))
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                  rt.id AS rt_id,
                  rt.device_label AS rt_device_label,
                  rt.expires_at AS rt_expires_at,
                  rt.revoked_at AS rt_revoked_at,
                  u.*
                FROM auth_refresh_token rt
                JOIN auth_user u ON u.id = rt.user_id
                WHERE rt.token_hash = ?
                LIMIT 1
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None

            if float(row["rt_expires_at"] or 0.0) <= now:
                return None

            token_id = str(row["rt_id"])
            revoked_at_raw = row["rt_revoked_at"]
            revoked_at = float(revoked_at_raw or 0.0) if revoked_at_raw is not None else None

            if revoked_at is not None:
                # If a refresh token was revoked because of rotation, we allow a small grace window
                # for concurrent requests to still refresh. Manual revocation (no successor) stays strict.
                rotated = self._conn.execute(
                    "SELECT 1 FROM auth_refresh_token WHERE rotated_from = ? LIMIT 1",
                    (token_id,),
                ).fetchone()
                if rotated is None:
                    return None
                if grace_s <= 0:
                    return None
                if revoked_at + grace_s < now:
                    return None

            user = self._row_to_user(row)
            if user is None or user.is_disabled:
                return None

            current_device = str(row["rt_device_label"] or "").strip()
            device = str(device_label or "").strip()[:80] or current_device or "device"

            self._conn.execute(
                "UPDATE auth_refresh_token SET last_used_at = ? WHERE id = ?",
                (now, token_id),
            )

            if revoked_at is None:
                self._conn.execute(
                    "UPDATE auth_refresh_token SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (now, token_id),
                )

            self._conn.execute(
                """
                INSERT INTO auth_refresh_token(
                  id, user_id, token_hash, device_label,
                  created_at, expires_at, last_used_at, revoked_at, rotated_from
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    new_id,
                    user.id,
                    new_hash,
                    device,
                    now,
                    expires_at,
                    now,
                    token_id,
                ),
            )
        next_session = self.get_refresh_session(new_raw)
        if next_session is None:
            return None
        return next_session, new_raw, expires_at

    def revoke_refresh_token(self, raw_refresh_token: str) -> None:
        token_hash = _sha256(str(raw_refresh_token or ""))
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE auth_refresh_token SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (now, token_hash),
            )

    def revoke_refresh_session(self, *, token_id: str, user_id: str | None = None) -> bool:
        tid = str(token_id or "").strip()
        if not tid:
            return False
        uid = str(user_id or "").strip()
        now = _now()
        with self._lock:
            if uid:
                row = self._conn.execute(
                    "SELECT id FROM auth_refresh_token WHERE id = ? AND user_id = ? AND revoked_at IS NULL LIMIT 1",
                    (tid, uid),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT id FROM auth_refresh_token WHERE id = ? AND revoked_at IS NULL LIMIT 1",
                    (tid,),
                ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE auth_refresh_token SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, tid),
            )
        return True

    def revoke_all_refresh_tokens(self, user_id: str) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE auth_refresh_token SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )

    def create_pairing_code(
        self, *, user_id: str, ttl_s: int, device_label: str | None = None
    ) -> tuple[str, float]:
        user = self.get_user_by_id(user_id)
        if user is None:
            raise KeyError("Unknown user")
        if user.is_disabled:
            raise ValueError("User is disabled")
        now = _now()
        expires_at = now + max(30, int(ttl_s))
        device = str(device_label or "").strip()[:80] or "mobile"
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM auth_pairing_code
                WHERE user_id = ? OR consumed_at IS NOT NULL OR expires_at <= ?
                """,
                (user.id, now),
            )
            for _ in range(8):
                code = _new_pairing_code()
                code_hash = _sha256(_normalize_pairing_code(code))
                try:
                    self._conn.execute(
                        """
                        INSERT INTO auth_pairing_code(
                          id, user_id, code_hash, device_label, created_at, expires_at, consumed_at
                        ) VALUES(?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (_uuid(), user.id, code_hash, device, now, expires_at),
                    )
                    return code, expires_at
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("Failed to create pairing code")

    def consume_pairing_code(
        self, *, code: str, device_label: str | None = None
    ) -> PairingSession | None:
        normalized = _normalize_pairing_code(code)
        if not normalized:
            return None
        code_hash = _sha256(normalized)
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT p.id AS pair_id, p.device_label AS pair_device_label, p.expires_at AS pair_expires_at, u.*
                FROM auth_pairing_code p
                JOIN auth_user u ON u.id = p.user_id
                WHERE p.code_hash = ? AND p.consumed_at IS NULL AND p.expires_at > ?
                LIMIT 1
                """,
                (code_hash, now),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE auth_pairing_code SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now, str(row["pair_id"])),
            )
        user = self._row_to_user(row)
        if user is None or user.is_disabled:
            return None
        next_device_label = str(device_label or "").strip()[:80] or str(
            row["pair_device_label"] or "mobile"
        )
        return PairingSession(
            user=user,
            device_label=next_device_label or "mobile",
            expires_at=float(row["pair_expires_at"] or 0.0),
        )

    def list_grants(self, user_id: str) -> list[GrantRule]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM auth_grant WHERE user_id = ? ORDER BY action, resource_type",
                (user_id,),
            ).fetchall()
        return [self._row_to_grant(row) for row in rows]

    def get_grant(self, user_id: str, action: str, resource_type: str) -> GrantRule | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM auth_grant WHERE user_id = ? AND action = ? AND resource_type = ? LIMIT 1",
                (user_id, action, resource_type),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_grant(row)

    def upsert_grant(
        self,
        *,
        user_id: str,
        action: str,
        resource_type: str,
        include: list[str],
        exclude: list[str],
    ) -> GrantRule:
        if self.get_user_by_id(user_id) is None:
            raise KeyError("Unknown user")
        include_norm = sorted(
            {_normalize_selector(item) for item in include if _normalize_selector(item)}
        )
        exclude_norm = sorted(
            {_normalize_selector(item) for item in exclude if _normalize_selector(item)}
        )
        now = _now()

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM auth_grant WHERE user_id = ? AND action = ? AND resource_type = ? LIMIT 1",
                (user_id, action, resource_type),
            ).fetchone()
            if existing is None:
                gid = _uuid()
                self._conn.execute(
                    """
                    INSERT INTO auth_grant(
                      id, user_id, action, resource_type,
                      include_json, exclude_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gid,
                        user_id,
                        action,
                        resource_type,
                        json.dumps(include_norm, ensure_ascii=False),
                        json.dumps(exclude_norm, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            else:
                gid = str(existing["id"])
                self._conn.execute(
                    """
                    UPDATE auth_grant
                    SET include_json = ?, exclude_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(include_norm, ensure_ascii=False),
                        json.dumps(exclude_norm, ensure_ascii=False),
                        now,
                        gid,
                    ),
                )
        grant = self.get_grant(user_id, action, resource_type)
        if grant is None:
            raise RuntimeError("Failed to persist grant")
        return grant

    def delete_grant(self, *, user_id: str, action: str, resource_type: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM auth_grant WHERE user_id = ? AND action = ? AND resource_type = ?",
                (user_id, action, resource_type),
            )


class AuthRuntime:
    access_cookie_name = "toposync_at"
    refresh_cookie_name = "toposync_rt"

    role_defaults: dict[RoleName, set[str]] = {
        "owner": {"*"},
        "admin": {"*"},
        "member": {
            "core:extensions:list",
            "core:extension:use",
            "core:compositions:read",
            "core:files:read",
            "core:events:emit",
            "core:devices:read",
            "core:area:read",
            "core:area:control",
            "core:notifications:read",
            "core:notifications:stream",
            "core:auth:pair",
        },
        "guest": {
            "core:extensions:list",
            "core:compositions:read",
            "core:files:read",
            "core:devices:read",
            "core:area:read",
            "core:notifications:read",
            "core:notifications:stream",
            "core:auth:pair",
        },
        "service": set(),
    }

    # Registry used by UX to configure include/exclude quickly.
    configurable_actions: dict[str, list[str]] = {
        "core:extension": ["core:extension:use", "core:extension:settings:write"],
        "core:event": ["core:events:emit"],
        "core:area": ["core:area:read", "core:area:control", "core:area:edit"],
    }

    public_routes: set[str] = {
        "/api/health",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/pair/complete",
    }

    def __init__(self, *, data_dir: Path) -> None:
        self.mode = _parse_mode(os.getenv("TOPOSYNC_AUTH_MODE"))
        self.cookie_secure_mode = _parse_cookie_secure_mode(
            os.getenv("TOPOSYNC_AUTH_COOKIE_SECURE")
        )
        self.access_ttl_s = int(os.getenv("TOPOSYNC_AUTH_ACCESS_TTL_S") or DEFAULT_ACCESS_TTL_S)
        self.refresh_ttl_s = int(os.getenv("TOPOSYNC_AUTH_REFRESH_TTL_S") or DEFAULT_REFRESH_TTL_S)
        self.pairing_ttl_s = int(os.getenv("TOPOSYNC_AUTH_PAIRING_TTL_S") or DEFAULT_PAIRING_TTL_S)
        self.refresh_rotation_grace_s = int(
            os.getenv("TOPOSYNC_AUTH_REFRESH_ROTATION_GRACE_S") or DEFAULT_REFRESH_ROTATION_GRACE_S
        )
        self.store = AuthStore(data_dir / "auth" / "auth.sqlite3")
        self._access_secret = self.store.get_or_create_secret("access_secret")
        # Comentário: credenciais opcionais para autenticação serviço->serviço (ex: sync de extensões).
        self._streaming_sync_username = str(os.getenv("TOPOSYNC_STREAMING_SYNC_USERNAME") or "").strip()
        self._streaming_sync_password = str(os.getenv("TOPOSYNC_STREAMING_SYNC_PASSWORD") or "").strip()

    @property
    def bypass_principal(self) -> AuthPrincipal:
        return AuthPrincipal(
            user_id="bypass",
            username="bypass",
            display_name="Bypass",
            role="owner",
            bypass=True,
        )

    def requires_setup(self) -> bool:
        return self.store.count_users() == 0

    def _sign_access_payload(self, payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = _b64url_encode(blob)
        sig = hmac.new(
            self._access_secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{payload_b64}.{_b64url_encode(sig)}"

    def _verify_access_token(self, token: str) -> dict[str, Any] | None:
        try:
            payload_b64, sig_b64 = str(token or "").split(".", 1)
            expected = hmac.new(
                self._access_secret.encode("utf-8"),
                payload_b64.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(_b64url_encode(expected), sig_b64):
                return None
            payload_raw = _b64url_decode(payload_b64)
            payload = json.loads(payload_raw.decode("utf-8"))
            if not isinstance(payload, dict):
                return None
            exp = float(payload.get("exp") or 0)
            if exp <= _now():
                return None
            return payload
        except Exception:
            return None

    def _issue_access_token(self, user: AuthUser) -> tuple[str, float]:
        now = _now()
        expires_at = now + max(60, int(self.access_ttl_s))
        payload = {
            "sub": user.id,
            "username": user.username,
            "role": user.role,
            "iat": now,
            "exp": expires_at,
            "pwd": user.password_updated_at,
        }
        return self._sign_access_payload(payload), expires_at

    def _principal_from_user(self, user: AuthUser) -> AuthPrincipal:
        return AuthPrincipal(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            bypass=False,
        )

    def _principal_from_access(self, token: str) -> AuthPrincipal | None:
        payload = self._verify_access_token(token)
        if payload is None:
            return None
        user_id = str(payload.get("sub") or "").strip()
        if not user_id:
            return None
        user = self.store.get_user_by_id(user_id)
        if user is None or user.is_disabled:
            return None
        pwd_marker = float(payload.get("pwd") or 0.0)
        if pwd_marker and user.password_updated_at > pwd_marker + 1e-6:
            return None
        return self._principal_from_user(user)

    def _tokens_from_refresh(
        self, raw_refresh_token: str
    ) -> tuple[AuthPrincipal, tuple[str, str]] | None:
        rotated = self.store.rotate_refresh_token(
            raw_refresh_token,
            ttl_s=self.refresh_ttl_s,
            rotation_grace_s=self.refresh_rotation_grace_s,
        )
        if rotated is None:
            return None
        next_session, next_refresh, _ = rotated
        user = next_session.user
        access_token, _ = self._issue_access_token(user)
        return self._principal_from_user(user), (access_token, next_refresh)

    def _authorization_header_token(self, request: Request) -> str:
        header = str(request.headers.get("authorization") or "")
        if not header.lower().startswith("bearer "):
            return ""
        return header.split(" ", 1)[1].strip()

    def _authorization_header_basic_credentials(self, request: Request) -> tuple[str, str] | None:
        header = str(request.headers.get("authorization") or "")
        if not header.lower().startswith("basic "):
            return None
        encoded = header.split(" ", 1)[1].strip()
        if not encoded:
            return None
        try:
            decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8", errors="replace")
        except Exception:
            return None
        if ":" not in decoded:
            return None
        username, password = decoded.split(":", 1)
        return username, password

    def resolve_request(self, request: Request) -> AuthContext:
        if self.mode == "bypass":
            return AuthContext(
                principal=self.bypass_principal, mode=self.mode, requires_setup=False
            )

        requires_setup = self.requires_setup()
        path = request.url.path
        if path in self.public_routes:
            return AuthContext(principal=None, mode=self.mode, requires_setup=requires_setup)

        if path == "/api/auth/setup":
            return AuthContext(principal=None, mode=self.mode, requires_setup=requires_setup)

        # Comentário: autenticação por Basic para sync interno do streaming (processing -> core).
        # Mantemos escopo estreito por path para evitar expor Basic globalmente.
        if path.startswith("/api/streams/distributed/settings/"):
            if self._streaming_sync_username and self._streaming_sync_password:
                maybe_basic = self._authorization_header_basic_credentials(request)
                if maybe_basic is not None:
                    username, password = maybe_basic
                    if hmac.compare_digest(username, self._streaming_sync_username) and hmac.compare_digest(
                        password, self._streaming_sync_password
                    ):
                        principal = AuthPrincipal(
                            user_id="service:streaming_sync",
                            username="streaming-sync",
                            display_name="Streaming Sync",
                            role="service",
                        )
                        return AuthContext(principal=principal, mode=self.mode, requires_setup=requires_setup)

        bearer = self._authorization_header_token(request)
        if bearer:
            principal = self._principal_from_access(bearer)
            return AuthContext(principal=principal, mode=self.mode, requires_setup=requires_setup)

        access_cookie = str(request.cookies.get(self.access_cookie_name) or "")
        if access_cookie:
            principal = self._principal_from_access(access_cookie)
            if principal is not None:
                return AuthContext(
                    principal=principal, mode=self.mode, requires_setup=requires_setup
                )

        refresh_cookie = str(request.cookies.get(self.refresh_cookie_name) or "")
        if refresh_cookie:
            refreshed = self._tokens_from_refresh(refresh_cookie)
            if refreshed is not None:
                principal, new_tokens = refreshed
                return AuthContext(
                    principal=principal,
                    mode=self.mode,
                    requires_setup=requires_setup,
                    cookies_to_set=new_tokens,
                )

        return AuthContext(principal=None, mode=self.mode, requires_setup=requires_setup)

    def apply_context_cookies(
        self, response: Response, context: AuthContext, *, request: Request | None = None
    ) -> None:
        if context.cookies_to_set is None:
            return
        access_token, refresh_token = context.cookies_to_set
        self.apply_session_cookies(
            response, access_token=access_token, refresh_token=refresh_token, request=request
        )

    def apply_session_cookies(
        self,
        response: Response,
        *,
        access_token: str,
        refresh_token: str,
        request: Request | None = None,
    ) -> None:
        access_max_age = max(60, int(self.access_ttl_s))
        refresh_max_age = max(60, int(self.refresh_ttl_s))
        now = datetime.now(timezone.utc)
        secure = False
        if self.cookie_secure_mode == "true":
            secure = True
        elif self.cookie_secure_mode == "auto":
            secure = _request_is_https(request)
        response.set_cookie(
            key=self.access_cookie_name,
            value=access_token,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
            max_age=access_max_age,
            expires=now + timedelta(seconds=access_max_age),
        )
        response.set_cookie(
            key=self.refresh_cookie_name,
            value=refresh_token,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
            max_age=refresh_max_age,
            expires=now + timedelta(seconds=refresh_max_age),
        )

    def clear_session_cookies(self, response: Response) -> None:
        response.delete_cookie(self.access_cookie_name, path="/")
        response.delete_cookie(self.refresh_cookie_name, path="/")

    def login(
        self, *, username: str, password: str, device_label: str
    ) -> tuple[AuthPrincipal, str, str]:
        if self.mode == "bypass":
            raise HTTPException(status_code=400, detail="Login is disabled in bypass mode")
        user = self.store.verify_credentials(username, password)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        access_token, _ = self._issue_access_token(user)
        refresh_token, _ = self.store.issue_refresh_token(
            user_id=user.id,
            device_label=device_label,
            ttl_s=self.refresh_ttl_s,
        )
        return self._principal_from_user(user), access_token, refresh_token

    def logout(self, raw_refresh_token: str | None) -> None:
        token = str(raw_refresh_token or "").strip()
        if token:
            self.store.revoke_refresh_token(token)

    def start_pairing(self, *, user_id: str, device_label: str | None = None) -> tuple[str, float]:
        return self.store.create_pairing_code(
            user_id=user_id, ttl_s=self.pairing_ttl_s, device_label=device_label
        )

    def complete_pairing(
        self, *, code: str, device_label: str | None = None
    ) -> tuple[AuthPrincipal, str, str]:
        pairing = self.store.consume_pairing_code(code=code, device_label=device_label)
        if pairing is None:
            raise HTTPException(status_code=401, detail="Invalid or expired pairing code")
        access_token, _ = self._issue_access_token(pairing.user)
        refresh_token, _ = self.store.issue_refresh_token(
            user_id=pairing.user.id,
            device_label=pairing.device_label,
            ttl_s=self.refresh_ttl_s,
        )
        return self._principal_from_user(pairing.user), access_token, refresh_token

    def setup_owner(self, *, username: str, display_name: str, password: str) -> AuthUser:
        if self.mode == "bypass":
            raise HTTPException(status_code=400, detail="Setup is disabled in bypass mode")
        if not self.requires_setup():
            raise HTTPException(status_code=409, detail="Auth is already configured")
        try:
            return self.store.bootstrap_owner(
                username=username, display_name=display_name, password=password
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def require_authenticated(self, context: AuthContext) -> AuthPrincipal:
        if self.mode == "bypass":
            return self.bypass_principal
        if context.requires_setup:
            raise HTTPException(status_code=503, detail="Auth setup is required")
        if context.principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return context.principal

    def _action_allowed_by_role(self, *, role: RoleName, action: str) -> bool:
        allowed = self.role_defaults.get(role, set())
        if "*" in allowed:
            return True
        return action in allowed

    def _allow_by_grant(
        self, *, user_id: str, action: str, resource_type: str, resource_selector: str
    ) -> bool | None:
        grant = self.store.get_grant(user_id, action, resource_type)
        if grant is None:
            return None
        include = grant.include
        exclude = grant.exclude

        if not include:
            include_ok = True
        else:
            include_ok = any(_selector_matches(selector, resource_selector) for selector in include)

        exclude_hit = any(_selector_matches(selector, resource_selector) for selector in exclude)
        return include_ok and not exclude_hit

    def authorize(
        self,
        *,
        context: AuthContext,
        action: str,
        resource_type: str | None = None,
        resource_selector: str = "*",
    ) -> AuthPrincipal:
        principal = self.require_authenticated(context)
        if principal.bypass or principal.role == "owner":
            return principal

        role_allowed = self._action_allowed_by_role(role=principal.role, action=action)
        if resource_type:
            grant_allowed = self._allow_by_grant(
                user_id=principal.user_id,
                action=action,
                resource_type=resource_type,
                resource_selector=resource_selector,
            )
            if grant_allowed is not None:
                if not grant_allowed:
                    raise HTTPException(status_code=403, detail="Permission denied")
                return principal

        if not role_allowed:
            raise HTTPException(status_code=403, detail="Permission denied")
        return principal

    def serialize_user(self, user: AuthUser, *, include_grants: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "is_disabled": bool(user.is_disabled),
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "sessions": self.store.active_sessions_count(user.id),
        }
        if include_grants:
            data["grants"] = [
                {
                    "id": grant.id,
                    "action": grant.action,
                    "resource_type": grant.resource_type,
                    "include": grant.include,
                    "exclude": grant.exclude,
                    "created_at": grant.created_at,
                    "updated_at": grant.updated_at,
                }
                for grant in self.store.list_grants(user.id)
            ]
        return data
