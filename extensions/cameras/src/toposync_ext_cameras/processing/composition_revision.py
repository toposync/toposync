"""Canonical persisted-map revision shared by inference and observation clients."""

import hashlib
import json

from toposync.runtime.config_store import Composition


def composition_revision(composition: Composition) -> str:
    return hashlib.sha256(
        json.dumps(composition.model_dump(), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
