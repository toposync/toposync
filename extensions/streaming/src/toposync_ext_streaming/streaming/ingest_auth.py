from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


INGEST_AUTH_USERNAME = "toposync_ingest"
REDACTED_PASSWORD = "********"


@dataclass(frozen=True, slots=True)
class CameraIngestCredentials:
    username: str
    password: str
    created_at_unix: float
    rotated_at_unix: float | None = None

    def redacted_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password": REDACTED_PASSWORD,
            "created_at_unix": self.created_at_unix,
            "rotated_at_unix": self.rotated_at_unix,
        }


class CameraIngestCredentialStore:
    def __init__(self, *, data_dir: Path) -> None:
        self._path = Path(data_dir) / "runtime" / "streaming" / "ingest-credentials.json"
        self._credentials: CameraIngestCredentials | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load_or_create(self) -> CameraIngestCredentials:
        if self._credentials is not None:
            return self._credentials
        loaded = self._load()
        if loaded is not None:
            self._credentials = loaded
            return loaded
        created = self._new_credentials(created_at_unix=time.time(), rotated_at_unix=None)
        self._write(created)
        self._credentials = created
        return created

    def rotate(self) -> CameraIngestCredentials:
        now = time.time()
        current = self.load_or_create()
        rotated = self._new_credentials(
            created_at_unix=float(current.created_at_unix or now),
            rotated_at_unix=now,
        )
        self._write(rotated)
        self._credentials = rotated
        return rotated

    def snapshot(self) -> dict[str, Any]:
        credentials = self.load_or_create()
        payload = credentials.redacted_dict()
        payload["path"] = str(self._path)
        payload["active"] = True
        return payload

    def _load(self) -> CameraIngestCredentials | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            return None

        if not isinstance(raw, dict):
            return None
        username = str(raw.get("username") or "").strip() or INGEST_AUTH_USERNAME
        password = str(raw.get("password") or "").strip()
        if not password:
            return None
        try:
            created_at = float(raw.get("created_at_unix") or time.time())
        except Exception:
            created_at = time.time()
        rotated_raw = raw.get("rotated_at_unix")
        try:
            rotated_at = float(rotated_raw) if rotated_raw is not None else None
        except Exception:
            rotated_at = None
        return CameraIngestCredentials(
            username=username,
            password=password,
            created_at_unix=created_at,
            rotated_at_unix=rotated_at,
        )

    def _write(self, credentials: CameraIngestCredentials) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(
                {
                    "username": credentials.username,
                    "password": credentials.password,
                    "created_at_unix": credentials.created_at_unix,
                    "rotated_at_unix": credentials.rotated_at_unix,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _chmod_private(tmp_path)
        tmp_path.replace(self._path)
        _chmod_private(self._path)

    @staticmethod
    def _new_credentials(*, created_at_unix: float, rotated_at_unix: float | None) -> CameraIngestCredentials:
        return CameraIngestCredentials(
            username=INGEST_AUTH_USERNAME,
            password=secrets.token_urlsafe(36),
            created_at_unix=float(created_at_unix),
            rotated_at_unix=rotated_at_unix,
        )


def redact_ingest_secret(value: Any, *, credentials: CameraIngestCredentials | None) -> Any:
    password = str(getattr(credentials, "password", "") or "")
    if not password:
        return value
    return _redact_value(value, password=password)


def _redact_value(value: Any, *, password: str) -> Any:
    if isinstance(value, dict):
        return {key: _redact_value(item, password=password) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, password=password) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, password=password) for item in value)
    if isinstance(value, str):
        return value.replace(password, REDACTED_PASSWORD)
    return value


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
