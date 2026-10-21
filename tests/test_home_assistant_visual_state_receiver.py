from __future__ import annotations

from copy import deepcopy

import pytest

import test_home_assistant_manifest_cache  # noqa: F401 - isolate integration imports from Home Assistant bootstrap
from integrations.home_assistant.custom_components.toposync.api import ToposyncApiError
from integrations.home_assistant.custom_components.toposync.visual_state import VisualStateReceiver


def snapshot(version=1, sequence=1, *, server="server", runtime="runtime", validity=2000, state_version=None):
    return {"schema_version": 1, "server_epoch": server, "snapshot_version": version,
        "capabilities": {"local_states": True, "remote_states": False}, "states": [{"sensor_id": "door",
        "pipeline_name": "pipeline", "node_id": "node", "classes": [
            {"class_id": key, "key": key, "label": key, "enabled": True} for key in ("open", "closed")],
        "state": {"status": "confirmed", "runtime_epoch": runtime, "state_version": sequence if state_version is None else state_version,
            "class_id": "closed", "class_key": "closed", "reason": "matched", "valid_for_ms": validity,
            "sample": {"capture_instance": "capture", "generation": 0, "sequence": sequence},
            "observed_at": 1000 + sequence, "confirmed_at": 1000 + sequence, "method": "local", "freshness_basis": "receipt",
            "preparation_revision": "preparation", "reference_revision": "references", "model_revision": "model"}}]}


def current(receiver):
    return receiver.data["door"]["state"]


def test_transport_time_subtracted_and_duplicate_evidence_never_extends_lease_even_after_failure():
    receiver = VisualStateReceiver()
    payload = snapshot()
    receiver.apply(payload, started=10, received=10.4)
    assert receiver.deadlines["door"] == 12
    assert 1599 <= current(receiver)["valid_for_ms"] <= 1600
    receiver.apply(snapshot(2), started=11, received=11.2)
    assert receiver.deadlines["door"] == 12
    receiver.unavailable("request_failed")
    assert current(receiver)["status"] == "unavailable" and current(receiver)["class_id"] is None
    assert all(current(receiver)[key] is None for key in ("observed_at", "confirmed_at", "sample", "method", "freshness_basis"))
    receiver.apply(snapshot(3), started=12, received=12.1)
    assert current(receiver)["status"] == "unknown" and not receiver.deadlines
    receiver.apply(snapshot(4, 2), started=13, received=13.1)
    assert current(receiver)["status"] == "confirmed" and receiver.deadlines["door"] == 15
    assert payload["states"][0]["state"]["valid_for_ms"] == 2000


def test_slow_response_is_already_expired_and_timer_is_independent_of_polling():
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(), started=10, received=13)
    assert current(receiver)["status"] == "unknown" and current(receiver)["class_key"] is None
    receiver.apply(snapshot(2, 2), started=20, received=20.1)
    assert not receiver.expire(21.9)
    assert receiver.expire(22)
    assert current(receiver)["reason"] == "receiver_expired"
    assert not receiver.expire(23)


def test_snapshot_runtime_and_sample_replays_fail_atomically_and_restart_has_no_restored_value():
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(2, 2), started=10, received=10)
    for payload in [snapshot(1, 3), snapshot(2, 2), snapshot(3, 1), snapshot(3, 2, state_version=1)]:
        with pytest.raises(ToposyncApiError):
            receiver.apply(payload, started=11, received=11)
        assert receiver.snapshot_version == 2 and receiver.deadlines["door"] == 12
    receiver.apply(snapshot(3, runtime="new_runtime"), started=11, received=11)
    with pytest.raises(ToposyncApiError):
        receiver.apply(snapshot(4, 3), started=11, received=11)
    receiver.apply(snapshot(server="new_server"), started=12, received=12)
    with pytest.raises(ToposyncApiError):
        receiver.apply(snapshot(5, 5), started=12, received=12)
    restarted = VisualStateReceiver()
    assert restarted.data == {} and restarted.deadlines == {}


def test_unknown_unavailable_removal_and_unsupported_are_distinct_and_clear_current_class():
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(), started=10, received=10)
    unknown = snapshot(2, 2)
    unknown["states"][0]["state"].update(status="unknown", class_id=None, class_key=None, valid_for_ms=0)
    receiver.apply(unknown, started=10.1, received=10.1)
    assert current(receiver)["status"] == "unknown" and not receiver.deadlines
    receiver.apply(None, started=10.2, received=10.2)
    assert receiver.supported is False and current(receiver)["status"] == "unavailable"
    removed = snapshot(3)
    removed["states"] = []
    receiver.apply(removed, started=10.3, received=10.3)
    assert receiver.supported is True and receiver.data == {} and not receiver._history


@pytest.mark.parametrize("patch", [
    {"status": []}, {"method": {}}, {"freshness_basis": []}, {"reason": {}},
    {"class_id": "missing"}, {"class_key": "open"}, {"valid_for_ms": True}, {"valid_for_ms": 3_600_001},
    {"valid_for_ms": 0}, {"state_version": True}, {"observed_at": float("nan")}, {"sample": None},
    {"sample": {"capture_instance": "capture", "generation": 0, "sequence": False}},
    {"status": "unavailable"}, {"runtime_epoch": None},
])
def test_malformed_state_is_rejected_without_partially_replacing_valid_data(patch):
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(), started=10, received=10)
    payload = snapshot(2, 2)
    payload["states"][0]["state"].update(patch)
    with pytest.raises(ToposyncApiError):
        receiver.apply(payload, started=11, received=11)
    assert receiver.snapshot_version == 1 and current(receiver)["class_id"] == "closed"


def test_duplicate_rows_classes_disabled_class_and_value_changed_without_version_are_rejected():
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(), started=10, received=10)
    duplicate = snapshot(2)
    duplicate["states"].append(deepcopy(duplicate["states"][0]))
    classes = snapshot(2)
    classes["states"][0]["classes"].append(deepcopy(classes["states"][0]["classes"][0]))
    disabled = snapshot(2)
    disabled["states"][0]["classes"][1]["enabled"] = False
    changed = snapshot(2)
    changed["states"][0]["state"].update(class_id="open", class_key="open")
    for payload in (duplicate, classes, disabled, changed):
        with pytest.raises(ToposyncApiError):
            receiver.apply(payload, started=11, received=11)
        assert receiver.snapshot_version == 1


def test_stopped_execution_cannot_reappear_after_an_unavailable_tombstone():
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(), started=10, received=10)
    stopped = snapshot(2)
    stopped["states"][0]["state"].update(status="unavailable", class_id=None, class_key=None, valid_for_ms=0,
                                        runtime_epoch=None, state_version=None, sample=None,
                                        preparation_revision=None, reference_revision=None, model_revision=None)
    receiver.apply(stopped, started=11, received=11)
    with pytest.raises(ToposyncApiError):
        receiver.apply(snapshot(3, 2), started=11.5, received=11.5)
    receiver.apply(snapshot(4, runtime="replacement"), started=12, received=12)
    assert current(receiver)["class_id"] == "closed"


def test_fractional_millisecond_remainder_never_publishes_a_class_with_zero_validity():
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(validity=1), started=10, received=10.0005)
    assert current(receiver)["status"] == "unknown" and current(receiver)["valid_for_ms"] == 0


def test_revisions_cannot_change_inside_one_runtime_epoch_and_restart_fences_are_bounded():
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(), started=1, received=1)
    changed = snapshot(2, 2)
    changed["states"][0]["state"]["reference_revision"] = "different"
    with pytest.raises(ToposyncApiError):
        receiver.apply(changed, started=1, received=1)
    for index in range(128):
        receiver.apply(snapshot(server=f"server_{index}"), started=1, received=1)
    with pytest.raises(ToposyncApiError):
        receiver.apply(snapshot(server="another_server"), started=1, received=1)
    assert len(receiver._retired_servers) == 128


@pytest.mark.parametrize("patch", [
    {"enabled": "true"}, {"kind": []}, {"kind": "switch"}, {"name": 3}, {"name": "x" * 81},
    {"on_class_ids": []}, {"off_class_ids": ["open"]}, {"on_class_ids": ["open", "open"]},
    {"on_class_ids": ["missing"]}, {"off_class_ids": [False]}, {"on_class_ids": "open"},
])
def test_invalid_publication_mapping_is_rejected_atomically(patch):
    receiver = VisualStateReceiver()
    receiver.apply(snapshot(), started=1, received=1)
    payload = snapshot(2, 2)
    payload["states"][0]["home_assistant"] = {"enabled": True, "kind": "binary", "name": "Porta",
                                              "on_class_ids": ["open"], "off_class_ids": ["closed"], **patch}
    with pytest.raises(ToposyncApiError):
        receiver.apply(payload, started=1, received=1)
    assert receiver.snapshot_version == 1
