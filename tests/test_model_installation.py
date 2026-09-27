"""Focused checks for installer integrity, ownership and subprocess cleanup."""
import hashlib
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys

import pytest
from toposync_ext_models import install_triposr as installer


def test_assets_reuse_verified_cache_and_preserve_existing(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.write_bytes(b"verified")
    target = tmp_path / "runtime/source/file.py"
    expected = hashlib.sha256(b"verified").hexdigest()
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unnecessary download"))
    installer.asset(target, expected, "unused", cache)
    assert target.read_bytes() == b"verified"
    cache.write_bytes(b"changed")
    assert target.read_bytes() == b"verified"
    target.write_bytes(b"existing work")
    with pytest.raises(ValueError, match="Existing asset differs"):
        installer.asset(target, expected, "unused", cache)
    assert target.read_bytes() == b"existing work"


def test_bad_or_oversized_download_is_not_published(tmp_path, monkeypatch):
    target = tmp_path / "source.py"
    for data in (b"wrong", b"x" * (1024**2 + 1)):
        monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: BytesIO(data))
        with pytest.raises(ValueError):
            installer.asset(target, "incorrect digest", "https://example.invalid/pinned")
        assert not target.exists()


def test_existing_directory_is_not_adopted(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(installer.sys, "version_info", (3, 12))
    monkeypatch.setattr(installer.shutil, "which", lambda name: name)
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda p: True if str(p).endswith("clang++") else original(p))
    for resume in (False, True):
        with pytest.raises(RuntimeError, match="Refusing existing destination"):
            installer.install(tmp_path, resume=resume)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_install_timeout_reaps_process(tmp_path):
    pidfile = tmp_path / "pid"
    script = "import os,time,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)"
    with (tmp_path / "log").open("w") as log:
        with pytest.raises(subprocess.TimeoutExpired):
            installer.run([sys.executable, "-c", script, str(pidfile)], os.environ.copy(), log, timeout=.3)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
