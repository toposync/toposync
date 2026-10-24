"""Reject host execution and unrelated persisted data before starting any application."""
from pathlib import Path

import pytest

from visual_state_producer_fixture import make_app, supervise


@pytest.mark.parametrize("entrypoint", [make_app, supervise])
@pytest.mark.parametrize("explicit_flag", [False, True])
def test_producer_requires_explicit_isolated_execution(tmp_path, monkeypatch, entrypoint, explicit_flag):
    if explicit_flag:
        monkeypatch.setenv("TOPOSYNC_FULL_PRODUCER_TEST", "1")
        monkeypatch.setattr(Path, "is_file", lambda self: False)
    else:
        monkeypatch.delenv("TOPOSYNC_FULL_PRODUCER_TEST", raising=False)
    arguments = (tmp_path / "data",) if entrypoint is make_app else (tmp_path / "data", tmp_path / "control")
    with pytest.raises(RuntimeError, match="[Ii]solated Docker test"):
        entrypoint(*arguments)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("resume,marker", [(False, None), (True, None), (True, '{"fixture":"unrelated"}')])
def test_producer_preserves_unrelated_data(tmp_path, monkeypatch, resume, marker):
    monkeypatch.setenv("TOPOSYNC_FULL_PRODUCER_TEST", "1")
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) == "/.dockerenv" or original(self))
    (tmp_path / "unrelated.txt").write_text("preserve")
    if marker is not None:
        (tmp_path / "synthetic-fixture.json").write_text(marker)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(RuntimeError, match="external test data|synthetic fixture"):
        make_app(tmp_path, resume=resume)
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
