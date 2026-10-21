from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("visual_state_client", Path(__file__).resolve().parents[1]
    / "integrations/home_assistant/custom_components/toposync/api.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def response():
    return {"schema_version": 1, "server_epoch": "execution", "snapshot_version": 1,
            "capabilities": {"local_states": True, "remote_states": False}, "states": []}


def test_client_distinguishes_missing_capability_from_authentication_and_transport_failure():
    async def run():
        client = module.ToposyncClient(None, url="http://local/ingress/installation", token="local-test")
        calls = []
        status = None
        async def read(method, path):
            calls.append((method, path))
            if status is not None:
                raise module.ToposyncApiError("failure", status=status)
            return response()
        client._json = read
        assert await client.get_visual_states() == response()
        assert client.url_for(calls[0][1]) == "http://local/ingress/installation/api/vision/visual-state/states"
        for status in (404, 405):
            assert await client.get_visual_states() is None
        for status in (401, 403, 429, 500, 503):
            with pytest.raises(module.ToposyncApiError) as failure:
                await client.get_visual_states()
            assert failure.value.status == status
    asyncio.run(run())


@pytest.mark.parametrize("invalid", [None, [], {}, {"schema_version": True}, {"schema_version": 2},
    {"server_epoch": ""}, {"snapshot_version": True}, {"snapshot_version": 0}, {"states": {}}, {"capabilities": {}}])
def test_client_rejects_invalid_snapshot_envelopes_instead_of_treating_them_as_missing_entities(invalid):
    async def run():
        client = module.ToposyncClient(None, url="http://local")
        async def read(*_):
            return {**response(), **invalid} if isinstance(invalid, dict) and invalid else invalid
        client._json = read
        with pytest.raises(module.ToposyncApiError, match="response is invalid"):
            await client.get_visual_states()
    asyncio.run(run())
