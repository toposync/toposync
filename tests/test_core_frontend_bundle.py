from __future__ import annotations

from pathlib import Path

import pytest

import toposync.app as app_mod


def test_resolve_frontend_dir_uses_bundled_frontend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package_root = tmp_path / "site-packages" / "toposync"
    frontend_dir = package_root / "_frontend" / "dist"
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app_file = package_root / "app.py"
    app_file.write_text("# test placeholder\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOPOSYNC_NO_FRONTEND", raising=False)
    monkeypatch.delenv("TOPOSYNC_FRONTEND_DIR", raising=False)
    monkeypatch.setattr(app_mod, "__file__", str(app_file))

    assert app_mod._resolve_frontend_dir() == frontend_dir.resolve()


def test_resolve_frontend_dir_prefers_override_over_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "site-packages" / "toposync"
    bundled_dir = package_root / "_frontend" / "dist"
    bundled_dir.mkdir(parents=True)
    (bundled_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app_file = package_root / "app.py"
    app_file.write_text("# test placeholder\n", encoding="utf-8")

    override_dir = tmp_path / "custom-frontend"
    override_dir.mkdir(parents=True)
    (override_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOPOSYNC_NO_FRONTEND", raising=False)
    monkeypatch.setenv("TOPOSYNC_FRONTEND_DIR", str(override_dir))
    monkeypatch.setattr(app_mod, "__file__", str(app_file))

    assert app_mod._resolve_frontend_dir() == override_dir.resolve()
