from __future__ import annotations

import pytest

from toposync_ext_streaming.streaming.engine_manager import _normalize_path
from toposync_ext_streaming.streaming.mediamtx_config import normalize_path_slug


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "test"),
        (" \t-_/ ", "test"),
        (" __Camera-MAIN__ ", "camera-main"),
        ("camera/main?view=2", "camera-main-view-2"),
        ("camera??main", "camera--main"),
        (" CÂMERA_庭 ", "câmera_庭"),
        ("İSTANBUL", "i-stanbul"),
        ("A\u0301udio", "a-udio"),
        ("²Ⅳ¼", "²ⅳ¼"),
        ("a\x00😀b", "a--b"),
        ("a\ud800b", "a-b"),
    ],
)
def test_stream_paths_preserve_names_and_unicode(value: str, expected: str) -> None:
    assert normalize_path_slug(value) == expected
    assert _normalize_path(value) == expected


def test_empty_path_preserves_callers_fallback() -> None:
    assert normalize_path_slug("/-_", fallback="") == ""
    assert normalize_path_slug("", fallback="Unchanged_Fallback") == "Unchanged_Fallback"
