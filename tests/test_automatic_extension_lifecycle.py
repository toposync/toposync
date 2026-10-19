from functools import partial

import pytest

from test_auth_local import _create_client, _setup_owner
from test_automatic_api import register_capability
from toposync.runtime import extension_management as management


@pytest.mark.parametrize("operation", ["disable", "remove"])
def test_extension_change_revokes_before_ack_preserves_history_and_dependencies(
    tmp_path, monkeypatch, operation
):
    calls, package_calls = [], []
    installed = True
    uninstall_ok = False

    def discover():
        return {"fixture": management.InstalledExtensionProbe(
            extension_id="fixture", name="Fixture", version="1", package="toposync-ext-fixture",
            package_version="1", entry_point_name="fixture", entry_point_value="fixture:Extension",
        )} if installed else {}

    async def uninstall(package):
        nonlocal installed
        package_calls.append(package)
        if uninstall_ok:
            installed = False
        return management.PipOperationResult(ok=uninstall_ok, command=["fake-uninstall", package])

    monkeypatch.setenv("TOPOSYNC_EXTENSION_AUTO_INSTALL_ON_STARTUP", "0")
    monkeypatch.setattr(management, "discover_installed_extensions", discover)
    monkeypatch.setattr(management, "run_pip_uninstall", uninstall)
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        register_capability(client, calls)
        store = client.app.state.config_store
        client.portal.call(management.save_extension_management_config, store,
                           management.ExtensionManagementConfig(desired=[management.ManagedExtensionSpec(
                               extension_id="fixture", package="toposync-ext-fixture",
                               pip_spec="toposync-ext-fixture", source="manual",
                           )]))
        saved = client.put("/api/automatic", json={"expected_revision": 0, "configuration": {
            "enabled": True, "intents": [{"id": "garage", "name": "Garage",
            "capability": "test.presence", "scope": {"camera_ids": ["fixture"]}}],
        }})
        assert saved.status_code == 200, saved.text
        state = saved.json()["configuration"]
        flow = client.post("/api/automatic/garage/flow", json={"expected_revision": state["revision"]})
        assert flow.status_code == 200, flow.text
        demands = client.app.state.pipeline_demands
        lease = demands.flow_leases[flow.json()["pipeline"]["name"]]
        record = client.portal.call(partial(client.app.state.notifications.upsert,
            type="fixture.activity", title="Recorded before removal", description="Synthetic history"))

        def dependents():
            response = client.get("/api/extensions/manage")
            assert response.status_code == 200
            return next(item for item in response.json()["items"] if item["extension_id"] == "fixture")["dependents"]

        expected = {"automatic_garage", "manual_garage"}
        assert {item["id"] for item in dependents()} == expected
        if operation == "remove":
            failed = client.delete("/api/extensions/manage/fixture")
            assert failed.status_code == 200 and not failed.json()["ok"]
            assert demands.allowed(lease) and calls.count("stopped") == 0
            uninstall_ok = True
            changed = client.delete("/api/extensions/manage/fixture")
        else:
            changed = client.post("/api/extensions/manage/fixture/disable")
        assert changed.status_code == 200 and changed.json()["ok"], changed.text
        assert not demands.allowed(lease) and not client.app.state.automatic.leases
        assert calls.count("started") == calls.count("stopped") == 1
        assert "fixture" in changed.json()["catalog"]["disabled_extension_ids"]
        assert {item["id"] for item in dependents()} == expected
        assert client.get("/api/notifications/" + record["id"]).json()["title"] == record["title"]
        for _ in range(3):
            client.portal.call(client.app.state.pipelines_orchestrator._reconcile)
        assert calls.count("started") == 1

        member = client.post("/api/access/users", json={"username": "member", "display_name": "Member",
            "role": "member", "password": "password123"})
        assert member.status_code == 200
        assert client.post("/api/auth/login", json={"username": "member", "password": "password123"}).status_code == 200
        assert dependents() == []
        assert client.get("/api/automatic").status_code == 403

    # The plugin and its operators are absent after restart; editing preserves the intent and contract.
    with _create_client(tmp_path, monkeypatch) as client:
        assert client.post("/api/auth/login", json={"username": "owner", "password": "password123"}).status_code == 200
        state = client.get("/api/automatic").json()["configuration"]
        state["intents"][0]["name"] = "Renamed while unavailable"
        saved = client.put("/api/automatic", json={"expected_revision": state["revision"], "configuration": state})
        assert saved.status_code == 200, saved.text
        assert saved.json()["activities"]["garage"]["state"] == "unavailable"
        item = next(item for item in client.get("/api/extensions/manage").json()["items"] if item["extension_id"] == "fixture")
        assert {item["id"] for item in item["dependents"]} == expected
        assert client.get("/api/notifications/" + record["id"]).json()["description"] == "Synthetic history"
        assert not client.app.state.automatic.leases and not client.app.state.pipeline_demands.flow_leases
        # Making the old provider available cannot activate a plan saved while unavailable.
        register_capability(client, calls)
        if operation == "disable":
            response = client.post("/api/extensions/manage/fixture/enable")
            assert response.status_code == 200, response.text
            assert not client.app.state.automatic.leases and not client.app.state.pipeline_demands.flow_leases
        assert calls.count("started") == 1
    assert package_calls == (["toposync-ext-fixture"] * 2 if operation == "remove" else [])
