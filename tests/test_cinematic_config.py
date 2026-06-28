from __future__ import annotations

import pytest
from pydantic import ValidationError

from toposync_ext_cinematic.pipelines import CinematicDirectorSourceConfig


def test_cinematic_director_config_defaults_are_open_by_default() -> None:
    config = CinematicDirectorSourceConfig()

    assert config.cameras_mode == "all"
    assert config.camera_ids == []
    assert config.priority_filter == []
    assert config.idle_dwell_seconds == pytest.approx(8.0)
    assert config.event_min_seconds == pytest.approx(10.0)
    assert config.cut_cooldown_seconds == pytest.approx(1.5)
    assert config.close_hold_seconds == pytest.approx(3.0)
    assert config.preferred_source_role == "auto"
    assert config.warmup_mode == "off"
    assert config.max_warm_cameras == 0
    assert config.ignore_own_pipeline_events is True


def test_cinematic_director_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CinematicDirectorSourceConfig.model_validate({"unknown": True})


def test_cinematic_director_config_normalizes_user_filters() -> None:
    config = CinematicDirectorSourceConfig.model_validate(
        {
            "camera_ids": [" front ", "front", "", " back "],
            "priority_filter": [" HIGH ", "low", "high"],
            "include_pipelines": [" person-front ", "person-front", ""],
            "exclude_pipelines": "debug-pipeline",
            "manual_camera_priorities": {" front ": 3, "": 99},
            "manual_event_type_priorities": {" person ": 10},
        }
    )

    assert config.camera_ids == ["front", "back"]
    assert config.priority_filter == ["high", "low"]
    assert config.include_pipelines == ["person-front"]
    assert config.exclude_pipelines == ["debug-pipeline"]
    assert config.manual_camera_priorities == {"front": 3}
    assert config.manual_event_type_priorities == {"person": 10}
