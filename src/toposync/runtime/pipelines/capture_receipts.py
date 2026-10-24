"""Short-lived proof binding prepared preview bytes to their owner and evidence."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Callable


class CaptureReceipts:
    # Process-local keys deliberately invalidate pending captures after a restart.
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._key = secrets.token_bytes(32)
        self._clock = clock

    def issue(self, owner: str, image: bytes, evidence: dict) -> str:
        payload = json.dumps({"owner": owner, "sha256": hashlib.sha256(image).hexdigest(),
                              "expires": self._clock() + 60, "evidence": evidence},
                             separators=(",", ":"), allow_nan=False).encode()
        encoded = base64.urlsafe_b64encode(payload).decode()
        signature = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def verify(self, token: str, owner: str, image: bytes) -> dict:
        try:
            if len(token) > 4096:
                raise ValueError
            encoded, signature = token.split(".")
            expected = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(base64.urlsafe_b64decode(encoded))
            if (payload["owner"] != owner or payload["expires"] <= self._clock()
                    or payload["sha256"] != hashlib.sha256(image).hexdigest()):
                raise ValueError
            return payload["evidence"]
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise ValueError("reference_capture_receipt_invalid") from error
