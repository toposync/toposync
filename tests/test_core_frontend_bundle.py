from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import toposync.app as app_mod
from toposync.app import create_app


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


def test_frontend_root_injects_ingress_base_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "index.html").write_text(
        '<!doctype html><html><head><title>Toposync</title><script src="main.js"></script><link href="/style.css"></head><body></body></html>',
        encoding="utf-8",
    )

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_FRONTEND_DIR", str(frontend_dir))
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.delenv("TOPOSYNC_NO_FRONTEND", raising=False)

    with TestClient(create_app()) as client:
        response = client.get(
            "/settings/pipelines",
            headers={"accept": "text/html", "x-ingress-path": "/api/hassio_ingress/test123"},
        )

    assert response.status_code == 200
    body = response.text
    assert 'window.__TOPOSYNC_PUBLIC_BASE_PATH__="/api/hassio_ingress/test123"' in body
    assert 'src="/api/hassio_ingress/test123/main.js"' in body
    assert 'href="/api/hassio_ingress/test123/style.css"' in body


def test_frontend_deep_link_uses_root_assets_without_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "index.html").write_text(
        '<!doctype html><html><head><script src="main.js"></script><link href="/style.css"></head><body></body></html>',
        encoding="utf-8",
    )

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_FRONTEND_DIR", str(frontend_dir))
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.delenv("TOPOSYNC_NO_FRONTEND", raising=False)

    with TestClient(create_app()) as client:
        response = client.get("/settings/pipelines", headers={"accept": "text/html"})

    assert response.status_code == 200
    body = response.text
    assert 'window.__TOPOSYNC_PUBLIC_BASE_PATH__="/"' in body
    assert 'src="/main.js"' in body
    assert 'href="/style.css"' in body


def test_frontend_static_assets_are_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "index.html").write_text(
        '<!doctype html><html><head><script src="main.js"></script></head><body></body></html>',
        encoding="utf-8",
    )
    (frontend_dir / "main.js").write_text("console.log('toposync')", encoding="utf-8")

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_FRONTEND_DIR", str(frontend_dir))
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.delenv("TOPOSYNC_NO_FRONTEND", raising=False)

    with TestClient(create_app()) as client:
        response = client.get("/main.js")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"
