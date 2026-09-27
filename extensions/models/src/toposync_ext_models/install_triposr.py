"""Explicit administrator installer for the evaluated macOS ARM CPU runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).parent
MANIFEST = HERE / "triposr_manifest.json"
REQUIREMENTS = HERE / "triposr_requirements.txt"
EXTRACTOR = "git+https://github.com/tatsy/torchmcubes.git@879926d0ef58e6ce0ac2630fdecb5e53af7ed3ff"
DINO = "f205d5d8e640a89a2b8ef0369670dfc37cc07fc2"
VERIFY = """
import importlib.metadata as metadata, json, sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    name, version = line.split("==")
    assert metadata.version(name) == version, name + " differs from the pinned environment"
source = json.loads(metadata.distribution("torchmcubes").read_text("direct_url.json"))
assert source["vcs_info"]["commit_id"] == sys.argv[2].rsplit("@", 1)[1]
"""

SMOKE = """
import sys, torch
from torchmcubes import marching_cubes
sys.path.insert(0, sys.argv[1])
from tsr.system import TSR
assert TSR.__name__ == 'TSR'
torch.set_num_threads(2)
x=torch.linspace(-1,1,24)
a,b,c=torch.meshgrid(x,x,x,indexing='ij')
v,f=marching_cubes(a*a+b*b+c*c,.5)
assert len(v)>0 and len(f)>0 and torch.isfinite(v).all()
assert int(f.min())>=0 and int(f.max())<len(v)
print('Provider import and CPU mesh extraction passed; no model loaded.')
"""


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def asset(target, expected, url, cache=None, content=None):
    if target.exists():
        if digest(target) != expected:
            raise ValueError(f"Existing asset differs: {target}; preserve and inspect it before resuming")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".download")
    if cache is not None and cache.is_file() and digest(cache) == expected:
        shutil.copyfile(cache, partial)
    elif content is not None:
        partial.write_bytes(content)
    else:
        limit = 1_677_246_742 if target.name == "model.ckpt" else 1024**2
        started = time.monotonic()
        count = 0
        with urllib.request.urlopen(url, timeout=30) as response, partial.open("wb") as stream:
            while block := response.read(1024**2):
                count += len(block)
                if count > limit or time.monotonic() - started > 300:
                    raise ValueError("Asset download exceeded size or time limit")
                stream.write(block)
    if digest(partial) != expected:
        raise ValueError(f"Asset integrity failed: {partial}")
    partial.replace(target)


def run(command, env, log, timeout=600):
    process = subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True)
    try:
        if process.wait(timeout=timeout):
            raise RuntimeError("Installation command failed; inspect installation.log before resuming")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def install(home, cache=None, resume=False, check=False):
    if (platform.system(), platform.machine(), sys.version_info[:2]) != ("Darwin", "arm64", (3, 12)):
        raise RuntimeError("This installer is qualified only for macOS ARM with Python 3.12")
    uv = shutil.which("uv")
    compiler = Path("/Library/Developer/CommandLineTools/usr/bin/clang++")
    if not uv or not compiler.is_file() or not shutil.which("cmake"):
        raise RuntimeError("Install uv, CMake and Apple Command Line Tools first; no global changes are made here")
    identity = hashlib.sha256(MANIFEST.read_bytes() + REQUIREMENTS.read_bytes()).hexdigest()
    marker = home / "installing.json"
    if check:
        if not home.is_dir() or marker.exists():
            raise RuntimeError("Runtime is missing or installation is incomplete")
    elif home.exists():
        if not resume or not marker.is_file() or json.loads(marker.read_text()).get("identity") != identity:
            raise RuntimeError("Refusing existing destination; --resume requires this installer's matching incomplete marker")
    else:
        home.mkdir(parents=True)
        marker.write_text(json.dumps({"identity": identity}))
    import fcntl
    with (home / ".generation.lock").open("a") as lease, (home / "installation.log").open("a") as log:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads(MANIFEST.read_text())
        for relative, expected in manifest["files"].items():
            target = home / relative
            if check:
                if digest(target) != expected:
                    raise ValueError(f"Asset integrity failed: {relative}")
                continue
            if relative.startswith("source/"):
                url = f'https://raw.githubusercontent.com/VAST-AI-Research/TripoSR/{manifest["source_revision"]}/{relative[7:]}'
            elif relative.startswith("weights/"):
                url = f'https://huggingface.co/stabilityai/TripoSR/resolve/{manifest["model_revision"]}/{relative[8:]}'
            else:
                url = f"https://huggingface.co/facebook/dino-vitb16/resolve/{DINO}/config.json"
            print(f"Verifying {relative}", flush=True)
            asset(target, expected, url, cache / relative if cache else None,
                  DINO.encode() if relative.endswith("refs/main") else None)
        python = home / "environment/bin/python"
        env = {**os.environ, "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
               "CC": str(compiler.with_name("clang")), "CXX": str(compiler),
               "CMAKE_BUILD_PARALLEL_LEVEL": "2", "MAX_JOBS": "2", "OMP_NUM_THREADS": "2",
               "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "HF_HUB_OFFLINE": "1",
               "TRANSFORMERS_OFFLINE": "1", "HF_HOME": str(home / "huggingface")}
        if not check:
            if not python.exists():
                run([uv, "venv", "--python", sys.executable, str(home / "environment")], env, log)
            run([uv, "pip", "install", "--python", str(python), "--no-deps", "-r", str(REQUIREMENTS)], env, log)
            torch = home / "environment/lib/python3.12/site-packages/torch"
            env["CMAKE_PREFIX_PATH"] = str(torch / "share/cmake")
            env["CMAKE_ARGS"] = f"-DTORCH_PYTHON_LIBRARY={torch / 'lib/libtorch_python.dylib'}"
            run([uv, "pip", "install", "--python", str(python), "--no-build-isolation", "--no-cache", "--reinstall", "--no-deps", EXTRACTOR], env, log)
        run([str(python), "-c", VERIFY, str(REQUIREMENTS), EXTRACTOR], env, log, timeout=30)
        run([uv, "pip", "check", "--python", str(python)], env, log)
        run([str(python), "-c", SMOKE, str(home / "source")], env, log, timeout=90)
        if not check:
            (home / "installation.json").write_text(json.dumps({"identity": identity, "platform": platform.platform(),
                "python": sys.version, "source_revision": manifest["source_revision"],
                "model_revision": manifest["model_revision"], "extractor": EXTRACTOR, "smoke_passed": True}, indent=2))
            marker.unlink()
    print("Runtime checks passed. Model inference/quality requires a separate generation check.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--asset-cache", type=Path, help="Read verified assets from an existing runtime; copied, never modified")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check existing assets, dependencies and extractor without installing")
    args = parser.parse_args()
    install(args.destination.expanduser().resolve(), args.asset_cache.expanduser().resolve() if args.asset_cache else None,
            args.resume, args.check)


if __name__ == "__main__":
    main()
