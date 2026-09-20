"""Current-map readback is independent of a notification's claimed digest."""

import hashlib
import json
import asyncio

from fastapi import HTTPException

from test_cameras_mapping_api import _create_client_with_cameras
from toposync.runtime.auth import AuthRuntime
from toposync.runtime.config_store import Composition
from toposync_ext_cameras.processing.composition_revision import composition_revision


def test_person_ground_binds_persisted_map_without_pointing(tmp_path):
    from pathlib import Path
    import subprocess
    from types import SimpleNamespace

    from toposync.runtime.config_store import CompositionElement, ConfigStore, UserDataPaths
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
    from toposync_ext_cameras.pipelines.person_ground import PersonGroundRuntime
    from test_person_ground import mapping_config, packet

    root = Path(__file__).resolve().parents[1]
    script = """
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const source = fs.readFileSync('extensions/cameras/ui/src/notifications/humanObservation.ts', 'utf8');
const context = {exports:{}};
vm.runInNewContext(ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText, context);
const input = JSON.parse(fs.readFileSync(0,'utf8'));
const paths = context.exports.humanObservationPayloadPaths;
const model = input.notification ? context.exports.readHumanObservation(input.notification, input.now) : null;
process.stdout.write(JSON.stringify({paths,model}));
"""

    def read_frontend(value):
        result = subprocess.run(["node", "-e", script], input=json.dumps(value), text=True,
                                capture_output=True, cwd=root, check=True, timeout=15)
        return json.loads(result.stdout)

    paths = read_frontend({})["paths"]
    assert len(paths) == 16

    async def scenario():
        store = ConfigStore(paths=UserDataPaths(tmp_path, tmp_path / "config.json", tmp_path / "files"))
        composition = Composition(id="map", name="Map", elements=[CompositionElement(
            id="camera-placement", type="camera",
            props={"camera_id": "camera", "calibrated_views": mapping_config()["calibrated_views"]},
        )])
        await store.set_active_composition(composition)
        runtime = PersonGroundRuntime({"mapping": {"composition_id": "map", "ptz_state_fetch": {"enabled": False}}},
                                      PipelineRuntimeDependencies(config_store=store))
        try:
            original_revision = None
            for index in range(2):
                current = next(item for item in (await store.get_config()).compositions if item.id == "map")
                expected = composition_revision(current)
                incoming = packet(float(index), camera="camera")
                incoming.payload["source_stream_id"] = incoming.stream_id
                incoming.payload["vision"]["pose_frame_packet_id"] = incoming.packet_id
                incoming.payload["vision"]["poses"][0]["actor_subject_id"] = incoming.payload["subject"]["id"]
                # The math fixture uses tuples; runtime pose payloads use JSON arrays.
                incoming.payload["vision"]["poses"] = json.loads(json.dumps(incoming.payload["vision"]["poses"]))
                mapped = (await runtime.process_packet(incoming, None))[0]
                spatial = mapped.payload["spatial"]
                assert "pointing" not in spatial
                assert spatial["person_ground"]["status"] == "estimated"
                assert spatial["camera"].get("map_revision") == expected
                assert spatial["person_ground"].get("map_revision") == expected
                emitted = []

                async def upsert(**kwargs):
                    emitted.append(kwargs)

                notify = NotifyRuntime({"notification_type": "com.toposync.cameras.human_observation",
                    "update_interval_seconds": 0, "include_payload_paths": paths},
                    PipelineRuntimeDependencies(notifications_upsert=upsert))
                await notify.process_packet(mapped, SimpleNamespace(pipeline_name="ground-only", node_id="notify"))
                notification = {"id": "test", "type": emitted[0]["type"], "title": "Synthetic contract",
                                "payload": emitted[0]["payload"]}
                assert notification["payload"]["data"]["vision"]["poses"]
                # Explicit clock validates the transport contract, not elapsed latency.
                model = read_frontend({"notification": notification,
                                      "now": incoming.payload["capture_evidence"]["published_at"] * 1000 + 1})["model"]
                assert model["state"] == "current", model["reason"]
                assert model["mapRevision"] == expected
                assert model["body"]["position"] is not None
                assert model["left"]["position"] is not None
                assert model["right"]["position"] is not None
                assert model["pointing"]["ray"] is None
                assert model["gestures"] == []
                if original_revision is not None:
                    assert expected != original_revision
                original_revision = expected
                await store.set_active_composition(current.model_copy(update={"name": "Changed map"}))
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_revision_readback_uses_pointing_digest_and_changes_with_map(tmp_path, monkeypatch):
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        composition = client.get("/api/composition").json()
        url = f"/api/cameras/compositions/{composition['id']}/observation-revision"
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        value = response.json()
        model = Composition.model_validate(composition)
        assert value["map_revision"] == composition_revision(model)
        assert value["map_revision"] == hashlib.sha256(
            json.dumps(model.model_dump(), sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        assert value["elements"] == composition["elements"]
        assert client.get("/api/composition").json() == composition
        composition["name"] += " revision change"
        assert client.put("/api/composition", json=composition).status_code == 200
        assert client.get(url).json()["map_revision"] != value["map_revision"]
        assert client.get("/api/cameras/compositions/unknown/observation-revision").status_code == 404


def test_revision_endpoint_requires_composition_read_permission(tmp_path, monkeypatch):
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        composition = client.get("/api/composition").json()
        actions = []

        def deny(_self, *, context, action, **_kwargs):
            actions.append(action)
            raise HTTPException(status_code=403, detail="Denied")

        monkeypatch.setattr(AuthRuntime, "authorize", deny)
        response = client.get(f"/api/cameras/compositions/{composition['id']}/observation-revision")
        assert response.status_code == 403
        assert actions == ["core:compositions:read"]
        assert "elements" not in response.json()


def test_invalid_map_revision_fails_closed_without_internal_details(tmp_path, monkeypatch):
    from toposync_ext_cameras.processing import composition_revision as module

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        composition = client.get("/api/composition").json()

        def invalid(_composition):
            raise ValueError("private malformed map details")

        monkeypatch.setattr(module, "composition_revision", invalid)
        response = client.get(f"/api/cameras/compositions/{composition['id']}/observation-revision")
        assert response.status_code == 409
        assert response.json() == {"detail": "Composition revision unavailable"}
