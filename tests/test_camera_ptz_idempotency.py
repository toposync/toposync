from __future__ import annotations

import asyncio
from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import AppConfig, AppSettings
import toposync.extensions.manager as ext_manager_mod


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

    def _entry_points(_group: str):
        return [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            ),
        ]

    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", _entry_points)
    return TestClient(create_app())


def _configure_camera(client: TestClient) -> None:
    client.portal.call(
        client.app.state.config_store.save_config,
        AppConfig(
            settings=AppSettings(
                extensions={
                    "com.toposync.cameras": {
                        "devices": [
                            {
                                "id": "cam1",
                                "name": "Camera 1",
                                "kind": "camera",
                                "control": {"type": "onvif"},
                                "onvif": {
                                    "xaddr": "192.168.0.10",
                                    "username": "camera-user",
                                    "password": "camera-pass",
                                    "media_xaddr": "http://192.168.0.10/onvif/media_service",
                                    "ptz_xaddr": "http://192.168.0.10/onvif/ptz_service",
                                },
                                "sources": [
                                    {
                                        "id": "zoom",
                                        "kind": "video",
                                        "is_default": True,
                                        "origin": {
                                            "type": "onvif_profile",
                                            "profile_token": "ptz-token",
                                            "rtsp_url": "rtsp://ingest.local/front",
                                            "has_ptz": True,
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            )
        ),
    )


def test_ptz_preset_http_retries_only_reconcile_ambiguous_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif import (
        OnvifAmbiguousMutationError,
        OnvifPtzPreset,
    )

    presets = {
        "cancelled-remove": "Cancelled remove",
        "stuck": "Stuck",
    }
    set_calls = {
        "Automatic restore": 0,
        "Cancelled create": 0,
        "Temporary restore": 0,
        "Never appeared": 0,
    }
    remove_calls = {"stuck": 0}

    class FakeOnvifClient:
        def __init__(
            self,
            *,
            xaddr: str,
            username: str,
            password: str,
            timeout_s: float,
            auth_mode: str,
        ) -> None:
            _ = xaddr, username, password, timeout_s, auth_mode

        async def get_ptz_presets(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
        ) -> list[OnvifPtzPreset]:
            _ = ptz_xaddr, profile_token
            return [OnvifPtzPreset(token=token, name=name) for token, name in presets.items()]

        async def set_preset(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
            preset_name: str = "",
            preset_token: str = "",
        ) -> str:
            _ = ptz_xaddr, profile_token
            assert preset_token == ""
            set_calls[preset_name] += 1
            if preset_name == "Cancelled create":
                raise asyncio.CancelledError
            if preset_name in {"Automatic restore", "Temporary restore"}:
                device_token = f"device-{preset_name.lower().replace(' ', '-')}"
                presets[device_token] = preset_name
            raise OnvifAmbiguousMutationError("SetPreset response timed out after send")

        async def remove_preset(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
            preset_token: str,
        ) -> None:
            _ = ptz_xaddr, profile_token
            remove_calls[preset_token] = remove_calls.get(preset_token, 0) + 1
            if preset_token == "cancelled-remove":
                raise asyncio.CancelledError
            if preset_token != "stuck":
                presets.pop(preset_token, None)
            raise OnvifAmbiguousMutationError("RemovePreset response timed out after send")

    monkeypatch.setattr("toposync_ext_cameras.plugin.OnvifClient", FakeOnvifClient)

    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        endpoint = "/api/cameras/cameras/cam1/ptz/presets"

        automatic_create = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Automatic restore"},
        )
        retried_automatic_create = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Automatic restore"},
        )
        assert automatic_create.status_code == 200, automatic_create.text
        assert retried_automatic_create.status_code == 200, retried_automatic_create.text
        assert retried_automatic_create.json() == automatic_create.json()
        assert set_calls["Automatic restore"] == 1

        automatic_token = str(automatic_create.json()["token"])
        removed_automatic = client.delete(
            f"{endpoint}/{automatic_token}",
            params={"source_id": "zoom"},
        )
        recreated_automatic = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Automatic restore"},
        )
        assert removed_automatic.status_code == 200, removed_automatic.text
        assert recreated_automatic.status_code == 200, recreated_automatic.text
        assert recreated_automatic.json()["token"] == automatic_token
        assert set_calls["Automatic restore"] == 2

        headers = {"Idempotency-Key": "restore-before-validation"}
        created = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Temporary restore"},
            headers=headers,
        )
        retried_create = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Temporary restore"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        assert retried_create.status_code == 200, retried_create.text
        assert retried_create.json() == created.json()
        assert set_calls["Temporary restore"] == 1

        created_token = str(created.json()["token"])
        removed = client.delete(f"{endpoint}/{created_token}", params={"source_id": "zoom"})
        retried_remove = client.delete(
            f"{endpoint}/{created_token}",
            params={"source_id": "zoom"},
        )
        assert removed.status_code == 200, removed.text
        assert retried_remove.status_code == 200, retried_remove.text
        assert remove_calls[created_token] == 1
        replayed_removed_create = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Temporary restore"},
            headers=headers,
        )
        assert replayed_removed_create.status_code == 409, replayed_removed_create.text
        assert set_calls["Temporary restore"] == 1

        unresolved_headers = {"Idempotency-Key": "set-never-observed"}
        unresolved_create = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Never appeared"},
            headers=unresolved_headers,
        )
        retried_unresolved_create = client.post(
            endpoint,
            json={"source_id": "zoom", "name": "Never appeared"},
            headers=unresolved_headers,
        )
        assert unresolved_create.status_code == 503, unresolved_create.text
        assert retried_unresolved_create.status_code == 503, retried_unresolved_create.text
        assert set_calls["Never appeared"] == 1

        unresolved_remove = client.delete(f"{endpoint}/stuck", params={"source_id": "zoom"})
        retried_unresolved_remove = client.delete(
            f"{endpoint}/stuck",
            params={"source_id": "zoom"},
        )
        assert unresolved_remove.status_code == 503, unresolved_remove.text
        assert retried_unresolved_remove.status_code == 503, retried_unresolved_remove.text
        assert remove_calls["stuck"] == 1

        async def exercise_cancellation_windows() -> None:
            services = client.app.state.services
            common = {"camera_id": "cam1", "camera_source_id": "zoom"}

            try:
                await services.call(
                    "cameras.ptz.set_preset",
                    preset_name="Cancelled create",
                    idempotency_key="cancelled-create",
                    **common,
                )
            except asyncio.CancelledError:
                pass
            else:  # pragma: no cover - protects the fake's cancellation contract
                raise AssertionError("SetPreset cancellation was not propagated")

            with pytest.raises(HTTPException) as set_retry:
                await services.call(
                    "cameras.ptz.set_preset",
                    preset_name="Cancelled create",
                    idempotency_key="cancelled-create",
                    **common,
                )
            assert set_retry.value.status_code == 503

            try:
                await services.call(
                    "cameras.ptz.remove_preset",
                    preset_token="cancelled-remove",
                    **common,
                )
            except asyncio.CancelledError:
                pass
            else:  # pragma: no cover - protects the fake's cancellation contract
                raise AssertionError("RemovePreset cancellation was not propagated")

            with pytest.raises(HTTPException) as remove_retry:
                await services.call(
                    "cameras.ptz.remove_preset",
                    preset_token="cancelled-remove",
                    **common,
                )
            assert remove_retry.value.status_code == 503

        client.portal.call(exercise_cancellation_windows)
        assert set_calls["Cancelled create"] == 1
        assert remove_calls["cancelled-remove"] == 1
