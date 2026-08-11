from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from toposync_ext_ptz_attention.models import AttentionProfile
from toposync_ext_ptz_attention.store import AttentionStore, DeviceProfileConflictError


def profile_payload(**overrides):  # noqa: ANN003, ANN201
    payload = {
        "id": "front_attention",
        "name": "Front attention",
        "mode": "shadow",
        "camera_id": "front",
        "source_id": "wide",
        "ptz_device_id": "front",
        "composition_id": "yard",
        "home_view_id": "home",
        "eligible_view_ids": ["home", "driveway"],
        "event_policies": [
            {
                "event_type": "person_near_vehicle",
                "enabled": True,
                "priority": 50,
                "preferred_view_id": "driveway",
            }
        ],
        "candidate_confirm_seconds": 0,
        "min_focus_seconds": 0,
        "close_grace_seconds": 0,
    }
    payload.update(overrides)
    return payload


def test_profile_is_strict_and_governs_event_policies() -> None:
    profile = AttentionProfile.model_validate(profile_payload())
    assert profile.event_policies[0].priority == 50
    assert profile.same_head_observer_acknowledged is False

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AttentionProfile.model_validate(profile_payload(raw_preset="vendor-token"))
    with pytest.raises(ValidationError, match="duplicate event policy"):
        AttentionProfile.model_validate(
            profile_payload(event_policies=[*profile_payload()["event_policies"]] * 2)
        )
    with pytest.raises(ValidationError, match="home_view_id must be included"):
        AttentionProfile.model_validate(
            profile_payload(mode="live_preset", eligible_view_ids=["driveway"])
        )
    with pytest.raises(ValidationError, match="distinct from home_view_id"):
        AttentionProfile.model_validate(
            profile_payload(mode="live_preset", eligible_view_ids=["home"])
        )
    with pytest.raises(ValidationError, match="ptz_device_id must equal camera_id"):
        AttentionProfile.model_validate(profile_payload(ptz_device_id="other-head"))


def test_store_persists_profiles_and_decisions_but_closes_live_sessions(tmp_path) -> None:
    path = tmp_path / "attention.sqlite3"
    store = AttentionStore(path)
    assert int(store._conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
    profile = AttentionProfile.model_validate(profile_payload(same_head_observer_acknowledged=True))
    store.create_profile(profile, now=10)
    store.record_decision(
        ptz_device_id=profile.ptz_device_id,
        profile_id=profile.id,
        state="FOCUSED",
        action="shadow_focus",
        reason="candidate_confirmed",
        now=11,
    )
    store.start_session(
        ptz_device_id=profile.ptz_device_id,
        profile_id=profile.id,
        event_key="event-one",
        preset_token="preset-driveway",
        priority=50,
        now=12,
    )
    store.close()

    reopened = AttentionStore(path)
    assert reopened.get_profile(profile.id) == profile
    assert reopened.get_profile(profile.id).same_head_observer_acknowledged is True
    assert reopened.is_recovery_required(profile.ptz_device_id) is True
    decisions, cursor = reopened.list_decisions()
    assert cursor is None
    assert decisions[0].action == "shadow_focus"
    reopened.close()

    reopened_again = AttentionStore(path)
    assert reopened_again.is_recovery_required(profile.ptz_device_id) is True
    reopened_again.clear_recovery_required(profile.ptz_device_id)
    assert reopened_again.is_recovery_required(profile.ptz_device_id) is False
    reopened_again.close()

    connection = sqlite3.connect(path)
    row = connection.execute("SELECT outcome, ended_at FROM attention_session LIMIT 1").fetchone()
    connection.close()
    assert row is not None
    assert row[0] == "process_restarted"
    assert row[1] is not None


def test_store_allows_only_one_profile_per_physical_device() -> None:
    store = AttentionStore(None)
    store.create_profile(AttentionProfile.model_validate(profile_payload()))
    with pytest.raises(DeviceProfileConflictError):
        store.create_profile(
            AttentionProfile.model_validate(
                profile_payload(id="other_profile", name="Other profile")
            )
        )
    store.close()


def test_invalid_persisted_profile_is_quarantined_from_runtime() -> None:
    store = AttentionStore(None)
    store._conn.execute(
        """
        INSERT INTO attention_profile(
          id, ptz_device_id, profile_json, revision, created_at, updated_at
        ) VALUES ('broken', 'broken', '{}', 1, 1, 1)
        """
    )
    assert store.list_profiles() == []
    assert store.get_profile("broken") is None
    store.close()
