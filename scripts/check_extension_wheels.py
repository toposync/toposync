#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EXTENSIONS: tuple[dict[str, object], ...] = (
    {
        "name": "structural",
        "package": "toposync_ext_structural",
        "required_files": ("toposync_ext_structural/extension.json", "toposync_ext_structural/static/remoteEntry.js"),
        "optional_source_dirs": (),
    },
    {
        "name": "models",
        "package": "toposync_ext_models",
        "required_files": ("toposync_ext_models/extension.json", "toposync_ext_models/static/remoteEntry.js"),
        "optional_source_dirs": (),
    },
    {
        "name": "images",
        "package": "toposync_ext_images",
        "required_files": ("toposync_ext_images/extension.json", "toposync_ext_images/static/remoteEntry.js"),
        "optional_source_dirs": (),
    },
    {
        "name": "home_assistant",
        "package": "toposync_ext_home_assistant",
        "required_files": (
            "toposync_ext_home_assistant/extension.json",
            "toposync_ext_home_assistant/static/remoteEntry.js",
        ),
        "optional_source_dirs": (),
    },
    {
        "name": "cameras",
        "package": "toposync_ext_cameras",
        "required_files": ("toposync_ext_cameras/extension.json", "toposync_ext_cameras/static/remoteEntry.js"),
        "optional_source_dirs": (),
    },
    {
        "name": "cinematic",
        "package": "toposync_ext_cinematic",
        "required_files": ("toposync_ext_cinematic/extension.json",),
        "optional_source_dirs": (),
    },
    {
        "name": "vision",
        "package": "toposync_ext_vision",
        "required_files": ("toposync_ext_vision/extension.json",),
        "required_prefixes": ("toposync_ext_vision/manifests/",),
        "forbidden_prefixes": ("toposync_ext_vision/models/",),
    },
    {
        "name": "streaming",
        "package": "toposync_ext_streaming",
        "required_files": (
            "toposync_ext_streaming/extension.json",
            "toposync_ext_streaming/static/remoteEntry.js",
            "toposync_ext_streaming/bin/ffmpeg/LICENSE",
            "toposync_ext_streaming/bin/mediamtx/LICENSE",
        ),
        "optional_source_dirs": (),
    },
)


def _build_wheel(extension_name: str, out_dir: Path) -> Path:
    extension_root = ROOT / "extensions" / extension_name
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(extension_root)],
        cwd=ROOT,
        check=True,
    )
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected exactly one wheel for {extension_name}, found {len(wheels)}")
    return wheels[0]


def _check_wheel(entry: dict[str, object], wheel_path: Path) -> None:
    required_files = tuple(str(item) for item in entry.get("required_files", ()))
    required_prefixes = tuple(str(item) for item in entry.get("required_prefixes", ()))
    forbidden_prefixes = tuple(str(item) for item in entry.get("forbidden_prefixes", ()))
    optional_source_dirs = tuple(entry.get("optional_source_dirs", ()))
    extension_name = str(entry["name"])
    extension_root = ROOT / "extensions" / extension_name

    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())

    missing = [item for item in required_files if item not in names]
    if missing:
        raise RuntimeError(f"{wheel_path.name} is missing required files: {', '.join(missing)}")

    for prefix in required_prefixes:
        if not any(name.startswith(prefix) for name in names):
            raise RuntimeError(f"{wheel_path.name} is missing packaged assets under {prefix}")

    for prefix in forbidden_prefixes:
        if any(name.startswith(prefix) for name in names):
            raise RuntimeError(f"{wheel_path.name} unexpectedly packaged files under {prefix}")

    for source_rel, wheel_prefix in optional_source_dirs:
        source_dir = extension_root / str(source_rel)
        if not source_dir.is_dir():
            continue
        expected = [
            f"{wheel_prefix}/{path.relative_to(source_dir).as_posix()}"
            for path in sorted(source_dir.rglob("*"))
            if path.is_file()
        ]
        missing_optional = [item for item in expected if item not in names]
        if missing_optional:
            raise RuntimeError(
                f"{wheel_path.name} is missing packaged files from {source_rel}: {', '.join(missing_optional[:5])}"
            )


def main() -> int:
    if shutil.which("uv") is None:
        raise RuntimeError("uv is required to verify extension wheels")

    with tempfile.TemporaryDirectory(prefix="toposync-ext-wheels-") as tmp:
        tmp_dir = Path(tmp)
        for entry in EXTENSIONS:
            name = str(entry["name"])
            wheel = _build_wheel(name, tmp_dir / name)
            _check_wheel(entry, wheel)
            print(f"[ok] {name}: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
