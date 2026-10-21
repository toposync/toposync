"""Validate snapshots and maintain observation leases using only receiver time."""
from __future__ import annotations

from copy import deepcopy
import math
import re

from .api import ToposyncApiError


def _require(condition):
    if not condition:
        raise ToposyncApiError("Toposync visual state snapshot is invalid or out of order")


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def _counter(value):
    return type(value) is int and value >= 0


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _sample(value):
    if value is None:
        return None
    _require(isinstance(value, dict) and _identifier(value.get("capture_instance"))
             and _counter(value.get("generation")) and _counter(value.get("sequence")))
    return value["capture_instance"], value["generation"], value["sequence"]


def _validate_row(row):
    _require(isinstance(row, dict) and _identifier(row.get("sensor_id")))
    classes, state = row.get("classes"), row.get("state")
    _require(isinstance(classes, list) and len(classes) <= 32 and isinstance(state, dict))
    identifiers, keys = set(), set()
    for category in classes:
        _require(isinstance(category, dict) and _identifier(category.get("class_id"))
                 and isinstance(category.get("key"), str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", category["key"])
                 and isinstance(category.get("label"), str) and 0 < len(category["label"]) <= 80
                 and type(category.get("enabled")) is bool)
        _require(category["class_id"] not in identifiers and category["key"] not in keys)
        identifiers.add(category["class_id"])
        keys.add(category["key"])
        _require(category["key"] not in {"unknown", "unavailable"})
    publication = row.get("home_assistant")
    if publication is not None:
        _require(isinstance(publication, dict) and type(publication.get("enabled")) is bool
                 and isinstance(publication.get("kind"), str) and publication["kind"] in {"enum", "binary"}
                 and isinstance(publication.get("name"), str) and len(publication["name"]) <= 80)
        mapped = []
        for key in ("on_class_ids", "off_class_ids"):
            values = publication.get(key)
            _require(isinstance(values, list) and len(values) <= 32 and all(_identifier(value) for value in values))
            mapped.extend(values)
        _require(len(mapped) == len(set(mapped)) and set(mapped) <= identifiers)
        _require(not publication["enabled"] or publication["kind"] != "binary"
                 or bool(publication["on_class_ids"] and publication["off_class_ids"]))
    status, epoch, version = state.get("status"), state.get("runtime_epoch"), state.get("state_version")
    _require(isinstance(status, str) and status in {"confirmed", "pending", "unknown", "unavailable"}
             and isinstance(state.get("reason"), str) and 0 < len(state["reason"]) <= 160)
    _require(status == "unavailable" or len(classes) >= 2)
    _require((_identifier(epoch) and _counter(version)) or (epoch is None and version is None and status == "unavailable"))
    _require(all(state.get(key) is None or _timestamp(state[key]) for key in ("observed_at", "confirmed_at")))
    _require(all((isinstance(state.get(key), str) and 0 < len(state[key]) <= 128) if epoch is not None
                 else state.get(key) is None for key in ("preparation_revision", "reference_revision", "model_revision")))
    validity = state.get("valid_for_ms")
    _require(_counter(validity) and validity <= 3_600_000)
    sample = _sample(state.get("sample"))
    category = next((item for item in classes if item["class_id"] == state.get("class_id")), None)
    if state.get("class_id") is not None:
        _require(status in {"confirmed", "pending"} and category is not None and category["enabled"]
                 and state.get("class_key") == category["key"] and validity > 0 and sample is not None
                 and _timestamp(state.get("observed_at")) and _timestamp(state.get("confirmed_at"))
                 and isinstance(state.get("method"), str) and state["method"] in {"local", "multimodal"}
                 and isinstance(state.get("freshness_basis"), str) and state["freshness_basis"] in {"receipt", "capture"}
                 and all(isinstance(state.get(key), str) and 0 < len(state[key]) <= 128 for key in
                         ("preparation_revision", "reference_revision", "model_revision")))
    else:
        _require(status != "confirmed" and state.get("class_key") is None and validity == 0)
    return sample


class VisualStateReceiver:
    def __init__(self):
        self.data = {}
        self.supported = None
        self.server_epoch = None
        self.snapshot_version = 0
        self._retired_servers = set()
        self._history = {}
        self.deadlines = {}

    def apply(self, payload, *, started, received):
        _require(math.isfinite(started) and math.isfinite(received) and received >= started)
        if payload is None:
            self.supported = False
            return self.unavailable("unsupported_server")
        _require(isinstance(payload, dict) and type(payload.get("schema_version")) is int and payload["schema_version"] == 1
                 and _identifier(payload.get("server_epoch")) and _counter(payload.get("snapshot_version"))
                 and payload["snapshot_version"] > 0 and isinstance(payload.get("states"), list)
                 and isinstance(payload.get("capabilities"), dict) and payload["capabilities"].get("local_states") is True)
        epoch, version = payload["server_epoch"], payload["snapshot_version"]
        _require(epoch not in self._retired_servers)
        changed_server = epoch != self.server_epoch
        _require(changed_server or version > self.snapshot_version)
        # Fail closed instead of evicting replay fences after pathological restart churn.
        _require(not changed_server or len(self._retired_servers) < 128)
        data, histories, deadlines = {}, {}, {}
        for raw in payload["states"]:
            sample = _validate_row(raw)
            row = deepcopy(raw)
            identifier, state = row["sensor_id"], row["state"]
            _require(identifier not in data)
            previous = None if changed_server else self._history.get(identifier)
            runtime, state_version = state["runtime_epoch"], state["state_version"]
            revisions = tuple(state.get(key) for key in ("preparation_revision", "reference_revision", "model_revision"))
            retired = set(previous["retired"]) if previous else set()
            deadline = started + state["valid_for_ms"] / 1000 if state["class_id"] is not None else None
            remembered_sample, remembered_deadline = sample, deadline
            if previous:
                _require(runtime is None or runtime not in retired)
                if runtime is not None and previous["runtime"] is not None and runtime != previous["runtime"]:
                    _require(len(retired) < 128)
                    retired.add(previous["runtime"])
                if runtime == previous["runtime"] and runtime is not None:
                    _require(state_version >= previous["version"])
                    _require(revisions == previous["revisions"])
                    if sample is not None and previous["sample"] is not None:
                        _require(sample[0] == previous["sample"][0] and sample[1:] >= previous["sample"][1:])
                    if state_version == previous["version"]:
                        _require(sample == previous["wire_sample"] and (state["status"], state["class_id"], state["class_key"])
                                 == previous["value"])
                    if sample == previous["sample"] and deadline is not None and previous["deadline"] is not None:
                        deadline = min(deadline, previous["deadline"])
                    remembered_sample = sample or previous["sample"]
                    remembered_deadline = deadline if deadline is not None else previous["deadline"]
                elif runtime is None:
                    # A stopped entry must not erase the last execution's replay fence.
                    if previous["runtime"] is not None:
                        _require(len(retired) < 128)
                        retired.add(previous["runtime"])
            histories[identifier] = {"runtime": runtime, "version": state_version, "sample": remembered_sample,
                "wire_sample": sample, "deadline": remembered_deadline, "retired": retired,
                "revisions": revisions,
                "value": (state["status"], state["class_id"], state["class_key"])}
            if deadline is not None:
                deadlines[identifier] = deadline
                state["valid_for_ms"] = max(0, math.floor((deadline - received) * 1000))
            data[identifier] = row
        if changed_server and self.server_epoch is not None:
            self._retired_servers.add(self.server_epoch)
        self.server_epoch, self.snapshot_version = epoch, version
        self._history, self.data, self.deadlines, self.supported = histories, data, deadlines, True
        self.expire(received)
        return self.data

    def expire(self, now):
        expired = {identifier for identifier, deadline in self.deadlines.items() if deadline - now < .001}
        if expired:
            self.data = {identifier: self._cleared(row, "unknown", "receiver_expired") if identifier in expired else row
                         for identifier, row in self.data.items()}
            for identifier in expired:
                del self.deadlines[identifier]
        return bool(expired)

    @staticmethod
    def _cleared(row, status, reason):
        return {**row, "state": {**row["state"], "status": status, "reason": reason,
                                 "class_id": None, "class_key": None, "valid_for_ms": 0,
                                 "observed_at": None, "confirmed_at": None, "sample": None,
                                 "method": None, "freshness_basis": None}}

    def unavailable(self, reason):
        self.deadlines.clear()
        self.data = {identifier: self._cleared(row, "unavailable", reason) for identifier, row in self.data.items()}
        return self.data
