"""API/lifecycle checks; controlled worker output does not qualify the model."""
import asyncio
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
        for data, expected in [(b'bad', 422), (image_bytes(False), 422), (b'x' * (generation.MAXIMUM_IMAGE_BYTES + 1), 413)]:
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
def test_worker_is_reaped_on_disconnect_or_timeout(tmp_path, monkeypatch, disconnect, expected):
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
    async def scenario():
        with pytest.raises(HTTPException) as error:
            await generation.run_worker(tmp_path, tmp_path/'input', tmp_path/'output', Request())
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
    result = subprocess.run([sys.executable, str(worker), str(tmp_path), str(tmp_path/'input'), str(tmp_path/'output')],
                            capture_output=True, timeout=5)
    assert result.returncode != 0
    assert b'provider_asset_integrity_failed' in result.stderr
    assert not (tmp_path/'output').exists()
