from pathlib import Path

import pytest

from visual_state_browser_fixture import make_app


def test_browser_fixture_rejects_repository_and_preserves_nonempty_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", "unchanged")
    with pytest.raises(RuntimeError, match="outside the repository"):
        make_app(Path(__file__).parent)
    marker = tmp_path / "existing-data.txt"
    marker.write_text("Do not overwrite")
    with pytest.raises(RuntimeError, match="new empty"):
        make_app(tmp_path)
    assert marker.read_text() == "Do not overwrite"
    assert not (tmp_path / "config.json").exists()
    import os
    assert os.environ["TOPOSYNC_DATA_DIR"] == "unchanged"


def test_browser_fixture_resolves_symlinks_before_repository_check(tmp_path):
    link = tmp_path / "checkout"
    link.symlink_to(Path(__file__).parent, target_is_directory=True)
    with pytest.raises(RuntimeError, match="outside the repository"):
        make_app(link)
