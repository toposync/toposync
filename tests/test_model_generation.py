"""API/lifecycle checks; controlled worker output does not qualify the model."""
import asyncio
import hashlib
from io import BytesIO
import json
import os
import struct
import sys
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from toposync_ext_models import generation

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Local worker currently supports POSIX only")


def image_bytes(transparent=True):
    image = Image.new("RGBA", (32, 32), (255, 255, 255, 0 if transparent else 255))
    image.paste((100, 80, 60, 255), (8, 8, 24, 24))
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def app_for(tmp_path, monkeypatch, *, installed=True):
    home = tmp_path / "provider"
    if installed:
        for name in ["environment/bin/python", "source/tsr/system.py", "weights/model.ckpt", "weights/config.yaml"]:
            path = home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
    monkeypatch.setenv("TOPOSYNC_MODELS_TRIPOSR_HOME", str(home))

    class Auth:
        mode = "bypass"
        denied = False
        def requires_setup(self):
            return False
        def authorize(self, **kwargs):
            assert kwargs["action"] in {"core:files:read", "core:files:write"}
            if self.denied:
                raise HTTPException(403, "denied")

    app = FastAPI()
    app.state.auth = Auth()
    app.state.config_store = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path, files_dir=tmp_path / "files"))
    generation.install_generation_routes(app)
    return app, home


def test_unavailable_denied_and_invalid_inputs_never_start_worker(tmp_path, monkeypatch):
    calls = []
    async def unexpected(*args):
        calls.append(args)
    monkeypatch.setattr(generation, "run_worker", unexpected)
    app, _ = app_for(tmp_path, monkeypatch, installed=False)
    with TestClient(app) as client:
        assert client.post('/api/models/generate', files={'file': ('object.png', image_bytes())}).status_code == 503
        app.state.auth.denied = True
        assert client.post('/api/models/generate', files={'file': ('object.png', image_bytes())}).status_code == 403
    app, home = app_for(tmp_path, monkeypatch)
    with TestClient(app) as client:
        marker = home / "installing.json"
        marker.write_text("{}")
        assert client.post('/api/models/generate', files={'file': ('object.png', image_bytes())}).status_code == 503
        marker.unlink()
        for data, expected in [(b'bad', 422), (b'x' * (generation.MAXIMUM_IMAGE_BYTES + 1), 413)]:
            assert client.post('/api/models/generate', files={'file': ('object.png', data)}).status_code == expected
    assert not calls


@pytest.mark.parametrize('mode,expected', [('publish', 200), ('revoked', 403), ('invalid', 503), ('missing', 503)])
def test_publish_only_after_verified_output_and_reauthorization(tmp_path, monkeypatch, mode, expected):
    app, home = app_for(tmp_path, monkeypatch)
    async def controlled_worker(_home, source, output, request):
        assert Image.open(source).mode == 'RGBA'
        if mode != 'missing':
            output.write_bytes(struct.pack('<4sII', b'glTF', 2, 12) if mode != 'invalid' else b'bad')
            output.with_suffix('.json').write_text(json.dumps({'kind': 'generated_approximation', 'metric_reconstruction': False}))
        if mode == 'revoked':
            app.state.auth.denied = True
    monkeypatch.setattr(generation, 'run_worker', controlled_worker)
    with TestClient(app) as client:
        response = client.post('/api/models/generate', files={'file': ('object.png', image_bytes())})
    assert response.status_code == expected
    assert not list((tmp_path / 'runtime/model-generation').iterdir())
    assert len(list((tmp_path / 'files').glob('*/model.glb'))) == (1 if expected == 200 else 0)
    with generation.provider_lease(home):
        with pytest.raises(HTTPException) as error:
            with generation.provider_lease(home):
                pass
        assert error.value.status_code == 409
    if expected == 200:
        assert response.json()['generation']['kind'] == 'generated_approximation'


@pytest.mark.parametrize('disconnect,expected', [(True, 499), (False, 504)])
@pytest.mark.parametrize('prepare_only', [False, True])
def test_worker_is_reaped_on_disconnect_or_timeout(tmp_path, monkeypatch, disconnect, expected, prepare_only):
    spawn = asyncio.create_subprocess_exec
    processes = []
    async def sleeper(*args, **kwargs):
        process = await spawn(sys.executable, '-c', 'import time; time.sleep(30)', **kwargs)
        processes.append(process)
        return process
    class Request:
        async def is_disconnected(self):
            return disconnect
    monkeypatch.setattr(generation.asyncio, 'create_subprocess_exec', sleeper)
    monkeypatch.setattr(generation, 'GENERATION_TIMEOUT_SECONDS', .05)
    monkeypatch.setattr(generation, 'PREPARATION_TIMEOUT_SECONDS', .05)
    async def scenario():
        with pytest.raises(HTTPException) as error:
            await generation.run_worker(tmp_path, tmp_path/'input', tmp_path/'output', Request(), prepare_only=prepare_only)
        assert error.value.status_code == expected
        assert processes[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    asyncio.run(scenario())


def test_cancelling_server_task_reaps_worker(tmp_path, monkeypatch):
    spawn = asyncio.create_subprocess_exec
    processes = []
    async def sleeper(*args, **kwargs):
        process = await spawn(sys.executable, '-c', 'import time; time.sleep(30)', **kwargs)
        processes.append(process)
        return process
    class Request:
        async def is_disconnected(self):
            return False
    monkeypatch.setattr(generation.asyncio, 'create_subprocess_exec', sleeper)
    async def scenario():
        task = asyncio.create_task(generation.run_worker(tmp_path, tmp_path/'input', tmp_path/'output', Request()))
        while not processes:
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    asyncio.run(scenario())


def test_provider_source_integrity_is_checked_before_loading_model(tmp_path):
    import subprocess
    worker = generation.Path(generation.__file__).with_name('generation_worker.py')
    manifest = json.loads(worker.with_name('triposr_manifest.json').read_text())
    first_file = tmp_path / next(iter(manifest['files']))
    first_file.parent.mkdir(parents=True)
    first_file.write_text('changed source')
    (tmp_path/'input').write_bytes(image_bytes())
    result = subprocess.run([sys.executable, str(worker), str(tmp_path), str(tmp_path/'input'), str(tmp_path/'output')],
                            capture_output=True, timeout=5)
    assert result.returncode != 0
    assert b'provider_asset_integrity_failed' in result.stderr
    assert not (tmp_path/'output').exists()


def test_opaque_preparation_preserves_exif_orientation_and_rejects_empty_inputs(tmp_path):
    photo = Image.new("RGB", (64, 32), "white")
    photo.paste((120, 60, 20), (10, 10, 30, 25))
    exif = photo.getexif()
    exif[274] = 6
    source = BytesIO()
    photo.save(source, "JPEG", exif=exif)
    target = tmp_path / "prepared.png"
    generation.prepare_image(source.getvalue(), target)
    with Image.open(target) as prepared:
        assert prepared.size == (32, 64)
        assert prepared.mode == "RGBA"
    for image in (Image.new("RGB", (32, 32), "black"), Image.new("RGBA", (32, 32), (0, 0, 0, 0))):
        data = BytesIO()
        image.save(data, "PNG")
        with pytest.raises(HTTPException) as error:
            generation.prepare_image(data.getvalue(), target)
        assert error.value.status_code == 422


def test_background_checks_do_not_download_or_import_heavy_runtime(tmp_path):
    from toposync_ext_models.generation_worker import BACKGROUND, isolate_object, validate_background_mask
    cutout = Image.open(BytesIO(image_bytes()))
    result, provenance = isolate_object(tmp_path, cutout)
    assert result is cutout and provenance == {"method": "provided_alpha"}
    opaque = Image.open(BytesIO(image_bytes(False)))
    for corrupt in (False, True):
        if corrupt:
            target = tmp_path / BACKGROUND["file"]
            target.parent.mkdir()
            target.write_bytes(b"corrupt")
        with pytest.raises(SystemExit) as error:
            isolate_object(tmp_path, opaque)
        assert error.value.code == 76
    for color in (0, 255):
        with pytest.raises(SystemExit) as error:
            validate_background_mask(Image.new("L", (32, 32), color))
        assert error.value.code == 74
    validate_background_mask(cutout.getchannel("A"))
    with pytest.raises(SystemExit) as error:
        isolate_object(tmp_path, Image.new("RGBA", (32, 32), (0, 0, 0, 255)))
    assert error.value.code == 74


@pytest.mark.parametrize("code,status,detail", [(74, 422, "isolated_object_image_required"), (76, 503, "background_removal_not_installed"), (77, 409, "generation_preparation_changed")])
def test_background_worker_exit_codes_are_explicit(tmp_path, monkeypatch, code, status, detail):
    async def wait():
        return code
    async def process(*args, **kwargs):
        return SimpleNamespace(returncode=code, wait=wait)
    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", process)
    with pytest.raises(HTTPException) as error:
        asyncio.run(generation.run_worker(tmp_path, tmp_path / "input.png", tmp_path / "model.glb", None))
    assert (error.value.status_code, error.value.detail) == (status, detail)


@pytest.mark.parametrize('case,status', [('valid', 200), ('pixels_changed', 503), ('source_changed', 503), ('revoked', 403)])
def test_preparation_is_private_verified_and_never_published(tmp_path, monkeypatch, case, status):
    from toposync_ext_models.generation_worker import prepared_image_digest
    app, _ = app_for(tmp_path, monkeypatch)
    calls = []
    async def controlled_worker(home, source, output, request, *, prepare_only=False):
        calls.append(prepare_only)
        assert prepare_only
        image = Image.open(source).convert('RGBA')
        image.save(output, 'PNG')
        output.with_suffix('.json').write_text(json.dumps({
            'kind': 'image_preparation',
            'prepared_sha256': '0'*64 if case == 'pixels_changed' else prepared_image_digest(image),
            'input_sha256': '0'*64 if case == 'source_changed' else hashlib.sha256(source.read_bytes()).hexdigest(),
            'background_removal': {'method': 'provided_alpha'},
        }))
        if case == 'revoked':
            app.state.auth.denied = True
    monkeypatch.setattr(generation, 'run_worker', controlled_worker)
    with TestClient(app) as client:
        response = client.post('/api/models/prepare-image', files={'file': ('object.png', image_bytes())})
    assert response.status_code == status
    assert calls == [True]
    assert not list((tmp_path/'runtime/model-generation').iterdir())
    assert not (tmp_path/'files').exists()
    if status == 200:
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.headers['content-type'] == 'image/png'
        assert response.headers['x-toposync-background-method'] == 'provided_alpha'
        assert response.headers['x-toposync-prepared-sha256'] == prepared_image_digest(Image.open(BytesIO(response.content)))


def test_preparation_validates_input_and_shares_generation_lease(tmp_path, monkeypatch):
    app, home = app_for(tmp_path, monkeypatch)
    async def unexpected(*args, **kwargs):
        raise AssertionError('Worker must not start')
    monkeypatch.setattr(generation, 'run_worker', unexpected)
    with TestClient(app) as client:
        with generation.provider_lease(home):
            assert client.post('/api/models/prepare-image', files={'file': ('object.png', image_bytes())}).status_code == 409
        assert client.post('/api/models/prepare-image', files={'file': ('bad.png', b'bad')}).status_code == 422
        assert client.post('/api/models/prepare-image', files={'file': ('large.png', b'x'*(generation.MAXIMUM_IMAGE_BYTES+1))}).status_code == 413
        app.state.auth.denied = True
        assert client.post('/api/models/prepare-image', files={'file': ('object.png', image_bytes())}).status_code == 403
    assert not list((tmp_path/'runtime/model-generation').iterdir())


@pytest.mark.parametrize('verified', [False, True])
def test_reviewed_generation_requires_matching_worker_receipt(tmp_path, monkeypatch, verified):
    app, _ = app_for(tmp_path, monkeypatch)
    digest = 'a'*64
    async def worker(home, source, output, request, *, expected_prepared_sha256):
        assert expected_prepared_sha256 == digest
        output.write_bytes(struct.pack('<4sII', b'glTF', 2, 12))
        output.with_suffix('.json').write_text(json.dumps({
            'kind': 'generated_approximation', 'metric_reconstruction': False,
            'prepared_sha256': digest, 'preparation_review_verified': verified,
        }))
    monkeypatch.setattr(generation, 'run_worker', worker)
    with TestClient(app) as client:
        for invalid in ['../input', 'a'*63, 'A'*64]:
            assert client.post('/api/models/generate', data={'expected_prepared_sha256': invalid}, files={'file': ('object.png', image_bytes())}).status_code == 422
        response = client.post('/api/models/generate', data={'expected_prepared_sha256': digest}, files={'file': ('object.png', image_bytes())})
    assert response.status_code == (200 if verified else 503)
    assert len(list((tmp_path/'files').glob('*/model.glb'))) == int(verified)


def test_transparent_pixel_normalization_preserves_alpha_and_visible_composites():
    import random
    from toposync_ext_models.generation_worker import canonicalize_transparent_pixels

    randomizer = random.Random(0)
    source = Image.frombytes('RGBA', (32, 32), randomizer.randbytes(32 * 32 * 4))
    source.putalpha(Image.frombytes('L', source.size, bytes([0, 1, 128, 255]) * 256))
    normalized = source.copy()
    canonicalize_transparent_pixels(normalized)
    assert normalized.size == source.size
    assert normalized.getchannel('A').tobytes() == source.getchannel('A').tobytes()
    before, after = source.tobytes(), normalized.tobytes()
    for offset in range(0, len(before), 4):
        original, actual = before[offset:offset + 4], after[offset:offset + 4]
        assert actual == (original if original[3] else bytes(4))
    for color in [(0, 0, 0, 255), (255, 255, 255, 255), (17, 53, 201, 128)]:
        background = Image.new('RGBA', source.size, color)
        assert Image.alpha_composite(background, normalized).tobytes() == Image.alpha_composite(background, source).tobytes()


def test_preparation_and_mismatched_review_do_not_need_3d_assets_or_imports(tmp_path):
    import subprocess
    from toposync_ext_models.generation_worker import prepared_image_digest
    worker = generation.Path(generation.__file__).with_name('generation_worker.py')
    source = tmp_path/'input.png'
    source.write_bytes(image_bytes())
    output = tmp_path/'prepared.png'
    # Any attempted heavy import fails, even if the developer environment has it.
    launcher = "import sys, runpy; sys.modules.update({name: None for name in ['numpy', 'torch', 'trimesh', 'tsr']}); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    result = subprocess.run([sys.executable, '-c', launcher, str(worker), str(tmp_path), str(source), str(output), '--prepare-only'], capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr.decode()
    original = Image.open(source).convert('RGBA')
    expected = Image.new('RGBA', original.size, (0, 0, 0, 0))
    expected.paste((100, 80, 60, 255), (8, 8, 24, 24))
    assert Image.open(output).tobytes() == expected.tobytes()
    receipt = json.loads(output.with_suffix('.json').read_text())
    assert receipt['prepared_sha256'] == prepared_image_digest(expected)
    assert receipt['input_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    rejected = tmp_path/'rejected.glb'
    result = subprocess.run([sys.executable, '-c', launcher, str(worker), str(tmp_path), str(source), str(rejected), '--expected-prepared-sha256', '0'*64], capture_output=True, timeout=5)
    assert result.returncode == 77, result.stderr.decode()
    assert not rejected.exists()
    reshaped = Image.frombytes('RGBA', (16, 64), original.tobytes())
    assert prepared_image_digest(original) != prepared_image_digest(reshaped)
    original.putpixel((0, 0), (0, 0, 0, 1))
    assert prepared_image_digest(original) != receipt['prepared_sha256']
