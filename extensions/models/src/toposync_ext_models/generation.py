"""Optional local image-to-mesh generation, published through the existing file store."""
import asyncio
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import tempfile
import uuid

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from PIL import Image, UnidentifiedImageError
from toposync.runtime.auth import AuthContext

MAXIMUM_IMAGE_BYTES = 8 * 1024**2
GENERATION_TIMEOUT_SECONDS = 300


def authorize(request: Request):
    auth = request.app.state.auth
    context = getattr(request.state, "auth_context", None)
    if not isinstance(context, AuthContext):
        context = AuthContext(principal=None, mode=auth.mode, requires_setup=auth.requires_setup())
    for action in ("core:files:read", "core:files:write"):
        auth.authorize(context=context, action=action, resource_type=None, resource_selector="*")


def provider_home(app: FastAPI) -> Path:
    default = app.state.config_store.paths.data_dir / "models" / "triposr"
    home = Path(os.environ.get("TOPOSYNC_MODELS_TRIPOSR_HOME", str(default))).resolve()
    if os.name != "posix" or (home / "installing.json").exists() or not all((home / path).is_file() for path in (
        "environment/bin/python", "source/tsr/system.py", "weights/model.ckpt", "weights/config.yaml",
    )):
        raise HTTPException(503, "local_generation_not_installed")
    return home


@contextmanager
def provider_lease(home: Path):
    import fcntl
    with (home / ".generation.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(409, "local_generation_busy") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare_image(data: bytes, target: Path):
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 16_777_216 or min(image.size) < 16:
                raise ValueError("image_dimensions")
            image.load()
            rgba = image.convert("RGBA")
            low, high = rgba.getchannel("A").getextrema()
            if low == 255 or high == 0:
                raise HTTPException(422, "isolated_object_image_required")
            rgba.save(target, "PNG")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "invalid_generation_image") from exc


async def run_worker(home: Path, source: Path, output: Path, request: Request):
    env = {**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
           "TOKENIZERS_PARALLELISM": "false", "HF_HOME": str(home / "huggingface"),
           "HF_HUB_CACHE": str(home / "huggingface/hub"),
           "HUGGINGFACE_HUB_CACHE": str(home / "huggingface/hub"),
           "TRANSFORMERS_CACHE": str(home / "huggingface/hub"),
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    worker = await asyncio.create_subprocess_exec(
        str(home / "environment/bin/python"), str(Path(__file__).with_name("generation_worker.py")),
        str(home), str(source), str(output), env=env, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
            while worker.returncode is None:
                if await request.is_disconnected():
                    raise HTTPException(499, "generation_cancelled")
                await asyncio.sleep(0.1)
            if worker.returncode != 0:
                raise HTTPException(503, "generation_memory_limit" if worker.returncode == 75 else "local_generation_failed")
    except TimeoutError as exc:
        raise HTTPException(504, "generation_timeout") from exc
    finally:
        if worker.returncode is None:
            try:
                os.killpg(worker.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(worker.wait(), 5)
            except TimeoutError:
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await worker.wait()
        else:
            await worker.wait()


def install_generation_routes(app: FastAPI):
    @app.post("/api/models/generate")
    async def generate(request: Request, response: Response, file: UploadFile = File(...)):
        authorize(request)
        response.headers["Cache-Control"] = "private, no-store"
        try:
            home = provider_home(app)
            data = await file.read(MAXIMUM_IMAGE_BYTES + 1)
            if len(data) > MAXIMUM_IMAGE_BYTES:
                raise HTTPException(413, "generation_image_too_large")
            runtime = app.state.config_store.paths.data_dir / "runtime" / "model-generation"
            runtime.mkdir(parents=True, exist_ok=True)
            with provider_lease(home), tempfile.TemporaryDirectory(dir=runtime) as folder:
                source, output = Path(folder) / "input.png", Path(folder) / "model.glb"
                await asyncio.to_thread(prepare_image, data, source)
                await run_worker(home, source, output, request)
                authorize(request)
                if await request.is_disconnected():
                    raise HTTPException(499, "generation_cancelled")
                try:
                    size = output.stat().st_size
                    with output.open("rb") as handle:
                        header = handle.read(12)
                    if not 12 <= size <= 64 * 1024**2 or header != struct.pack("<4sII", b"glTF", 2, size):
                        raise ValueError("invalid_glb")
                    provenance = json.loads(output.with_suffix(".json").read_text())
                    if provenance.get("kind") != "generated_approximation" or provenance.get("metric_reconstruction") is not False:
                        raise ValueError("invalid_provenance")
                except (OSError, ValueError, AttributeError) as exc:
                    raise HTTPException(503, "invalid_generated_model") from exc
                identifier = uuid.uuid4().hex
                target = app.state.config_store.paths.files_dir / identifier
                target.mkdir(parents=True)
                try:
                    shutil.move(str(output), target / "model.glb")
                    shutil.move(str(output.with_suffix(".json")), target / "generation.json")
                except BaseException:
                    shutil.rmtree(target)
                    raise
                return {"dir": identifier, "filename": "model.glb", "url": f"/files/{identifier}/model.glb",
                        "size_bytes": size, "generation": provenance}
        finally:
            await file.close()
