from __future__ import annotations

from toposync_ext_cameras.processing.yolo import _normalize_track_id


def test_normalize_track_id_accepts_nested_singleton_sequences() -> None:
    assert _normalize_track_id([[17.0]]) == 17
    assert _normalize_track_id(((17,),)) == 17
    assert _normalize_track_id([[[17]]]) == 17


def test_normalize_track_id_accepts_stringified_numeric_values() -> None:
    assert _normalize_track_id("17") == 17
    assert _normalize_track_id("17.0") == 17
