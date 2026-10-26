"""Serialize mandatory images and optional diagnostics against one storage budget."""
from contextlib import contextmanager
from pathlib import Path
import threading


_storage_lock = threading.RLock()


@contextmanager
def storage_capacity(root: Path, maximum_bytes: int, *, reserved_bytes: int = 0):
    with _storage_lock:
        used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        yield max(0, maximum_bytes - used - reserved_bytes)
