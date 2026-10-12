"""Local, expiring capture demand from visual observations in camera pipelines."""

import threading
import time


class ObservationWindowStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._leases = {}

    def retain(self, scope, owner, subject, *, seconds):
        with self._lock:
            self._expire()
            self._leases[(scope, owner, subject)] = time.monotonic() + seconds

    def release(self, owner, subject=None):
        with self._lock:
            for key in tuple(self._leases):
                if key[1] == owner and (subject is None or key[2] == subject):
                    self._leases.pop(key, None)

    def _expire(self):
        now = time.monotonic()
        for key, deadline in tuple(self._leases.items()):
            if deadline <= now:
                self._leases.pop(key, None)

    def active(self, scope):
        with self._lock:
            self._expire()
            return any(key[0] == scope for key in self._leases)


_STORE = ObservationWindowStore()


def get_global_observation_window_store():
    return _STORE
