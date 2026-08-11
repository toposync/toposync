from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod


def _create_client_with_cameras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

    monkeypatch.setattr(
        ext_manager_mod,
        "_iter_entry_points",
        lambda _group: [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            ),
        ],
    )
    return TestClient(create_app())


def test_normalize_onvif_camera_keeps_control_and_sources() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "devices": [
                {
                    "id": "front",
                    "name": "Front",
                    "control": {"type": "onvif"},
                    "onvif": {
                        "xaddr": "192.168.0.10",
                        "username": "camera-user",
                        "password": "camera-pass",
                    },
                    "sources": [
                        {
                            "id": "main",
                            "kind": "video",
                            "is_default": True,
                            "origin": {
                                "type": "onvif_profile",
                                "rtsp_url": "rtsp://192.168.0.10/main",
                                "profile_token": "profile-main",
                            },
                        }
                    ],
                }
            ]
        }
    )

    device = normalized["devices"][0]
    assert device["control"]["type"] == "onvif"
    assert "ptz_device_id" not in device["control"]
    assert device["control"]["automation_exclusive_control_confirmed"] is False
    assert device["onvif"]["username"] == "camera-user"
    assert device["onvif"]["password"] == "camera-pass"
    source = device["sources"][0]
    assert source["origin"]["type"] == "onvif_profile"
    assert source["origin"]["profile_token"] == "profile-main"


def test_camera_control_rejects_free_form_ptz_device_identifier() -> None:
    from toposync_ext_cameras.settings import CameraDeviceSettings

    with pytest.raises(ValidationError, match="ptz_device_id"):
        CameraDeviceSettings.model_validate(
            {
                "id": "front-zoom",
                "control": {
                    "type": "onvif",
                    "ptz_device_id": "front-trackmix-head",
                    "automation_exclusive_control_confirmed": True,
                },
            }
        )


def test_camera_settings_authorization_selectors_only_include_changed_camera_records() -> None:
    from toposync_ext_cameras.settings import camera_settings_authorization_selectors

    current = {
        "schema_version": 4,
        "devices": [
            {"id": "front", "name": "Front", "enabled": True},
            {"id": "back", "name": "Back", "enabled": True},
        ],
    }
    proposed = {
        "schema_version": 4,
        "devices": [
            {"id": "front", "name": "Front door", "enabled": True},
            {"id": "back", "name": "Back", "enabled": True},
        ],
    }

    assert camera_settings_authorization_selectors(current, proposed) == ["front"]
    assert camera_settings_authorization_selectors(
        current,
        {"schema_version": 4, "devices": [current["devices"][0]]},
    ) == ["back"]
    assert camera_settings_authorization_selectors(
        {"schema_version": 4, "devices": []},
        {"schema_version": 5, "devices": []},
    ) == ["*"]
    assert camera_settings_authorization_selectors(
        current,
        {"schema_version": 4, "devices": "invalid"},
    ) == ["*"]


def test_camera_catalog_exposes_only_safe_ptz_control_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 4,
                "devices": [
                    {
                        "id": "front",
                        "name": "Front",
                        "control": {
                            "type": "onvif",
                            "automation_exclusive_control_confirmed": True,
                        },
                        "onvif": {
                            "xaddr": "http://camera/onvif/device_service",
                            "username": "camera-user",
                            "password": "camera-secret",
                        },
                        "sources": [
                            {
                                "id": "zoom",
                                "name": "Zoom",
                                "kind": "video",
                                "enabled": True,
                                "is_default": True,
                                "role": "zoom",
                                "origin": {
                                    "type": "onvif_profile",
                                    "profile_token": "profile-zoom",
                                    "has_ptz": True,
                                },
                            }
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text

        async def catalog() -> dict[str, object]:
            return await client.app.state.services.call("cameras.catalog.list")

        result = client.portal.call(catalog)

    camera = result["cameras"][0]
    assert camera["control"] == {
        "type": "onvif",
        "has_ptz": True,
        "ptz_device_id": "front",
        "automation_exclusive_control_confirmed": True,
    }
    assert camera["sources"][0]["has_ptz"] is True
    assert "onvif" not in camera
    assert "camera-secret" not in str(result)


def test_camera_catalog_does_not_publish_an_actuator_for_fixed_or_unmarked_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 4,
                "devices": [
                    {
                        "id": "fixed",
                        "control": {"type": "none"},
                        "sources": [
                            {
                                "id": "main",
                                "kind": "video",
                                "enabled": True,
                                "is_default": True,
                                "origin": {"type": "rtsp", "has_ptz": False},
                                "metadata": {"has_ptz": True},
                            }
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text

        async def catalog() -> dict[str, object]:
            return await client.app.state.services.call("cameras.catalog.list")

        result = client.portal.call(catalog)

    camera = result["cameras"][0]
    assert camera["control"] == {
        "type": "none",
        "has_ptz": False,
        "automation_exclusive_control_confirmed": False,
    }
    assert camera["sources"][0]["has_ptz"] is False
    assert "ptz_device_id" not in str(camera)


def test_camera_index_is_allowlisted_and_does_not_expose_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 4,
                "devices": [
                    {
                        "id": "front",
                        "name": "Front",
                        "enabled": True,
                        "control": {
                            "type": "onvif",
                            "automation_exclusive_control_confirmed": True,
                        },
                        "onvif": {
                            "xaddr": "http://camera/onvif/device_service",
                            "username": "onvif-user-secret",
                            "password": "onvif-password-secret",
                        },
                        "sources": [
                            {
                                "id": "zoom",
                                "name": "Zoom",
                                "kind": "video",
                                "enabled": True,
                                "is_default": True,
                                "role": "zoom",
                                "view_id": "zoom-view",
                                "origin": {
                                    "type": "onvif_profile",
                                    "rtsp_url": "rtsp://camera/credential-in-url",
                                    "profile_token": "profile-token-secret",
                                    "profile_name": "Zoom profile",
                                    "stream_username": "stream-user-secret",
                                    "stream_password": "stream-password-secret",
                                    "has_ptz": True,
                                },
                                "video": {"width": 1920, "height": 1080, "fps": 15},
                                "ingest": {
                                    "mode": "centralized",
                                    "host_server_id": "local",
                                },
                                "metadata": {"private_note": "metadata-secret"},
                            }
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text

        response = client.get("/api/cameras/index")
        assert response.status_code == 200, response.text
        payload = response.json()

    camera = payload["cameras"][0]
    assert camera["enabled"] is True
    assert camera["control"] == {
        "type": "onvif",
        "has_ptz": True,
        "automation_exclusive_control_confirmed": True,
        "ptz_device_id": "front",
    }
    source = camera["sources"][0]
    assert source["origin"] == {
        "type": "onvif_profile",
        "has_ptz": True,
        "profile_name": "Zoom profile",
    }
    assert source["video"] == {"width": 1920, "height": 1080, "fps": 15.0}
    assert source["ingest"] == {"mode": "centralized", "host_server_id": "local"}
    serialized = response.text
    for secret in (
        "credential-in-url",
        "profile-token-secret",
        "stream-user-secret",
        "stream-password-secret",
        "onvif-user-secret",
        "onvif-password-secret",
        "metadata-secret",
    ):
        assert secret not in serialized


def test_camera_plugin_registers_only_leased_mutating_ptz_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        service_ids = set(client.app.state.services._services)  # noqa: SLF001

    assert {
        "cameras.control.acquire",
        "cameras.control.renew",
        "cameras.control.submit",
        "cameras.control.release",
        "cameras.control.snapshot",
        "cameras.control.emergency_stop",
        "cameras.views.resolve_target",
        "cameras.ptz.list_presets",
        "cameras.ptz.get_status",
    } <= service_ids
    assert {
        "cameras.ptz.goto_preset",
        "cameras.ptz.absolute_move",
        "cameras.ptz.continuous_move",
        "cameras.ptz.stop",
    }.isdisjoint(service_ids)


def test_camera_app_shutdown_clears_ptz_leases_and_watchdogs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = None
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 4,
                "devices": [
                    {
                        "id": "front",
                        "enabled": True,
                        "control": {"type": "onvif"},
                        "onvif": {"xaddr": "http://camera/onvif/device_service"},
                        "sources": [
                            {
                                "id": "zoom",
                                "kind": "video",
                                "enabled": True,
                                "is_default": True,
                                "origin": {
                                    "type": "onvif_profile",
                                    "profile_token": "profile-zoom",
                                    "has_ptz": True,
                                },
                            }
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        controller = client.app.state.camera_ptz_controller

        async def fake_execute_command(**_kwargs: object) -> dict[str, object]:
            return {"ok": True}

        controller._execute_command = fake_execute_command  # noqa: SLF001

        async def start_motion() -> None:
            lease = await client.app.state.services.call(
                "cameras.control.acquire",
                camera_id="front",
                camera_source_id="zoom",
                owner_kind="manual",
                owner_id="manual:test",
                ttl_s=30,
            )
            await client.app.state.services.call(
                "cameras.control.submit",
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="continuous-before-app-shutdown",
                command={"kind": "continuous_move", "pan": 0.2, "timeout_s": 30},
            )

        client.portal.call(start_motion)
        runtime = controller._devices["front"]  # noqa: SLF001
        assert runtime.lease is not None
        assert runtime.lease_expiry_task is not None
        assert runtime.continuous_watchdog_task is not None

    assert controller is not None
    assert controller._shutdown_complete is True  # noqa: SLF001
    runtime = controller._devices["front"]  # noqa: SLF001
    assert runtime.lease is None
    assert runtime.lease_expiry_task is None
    assert runtime.continuous_watchdog_task is None
    assert not any(not task.done() for task in controller._background_tasks)  # noqa: SLF001


def test_ptz_lease_requires_enabled_camera_and_explicit_ptz_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.ptz_controller import PtzControlError

    def settings_payload(*, camera_enabled: bool, source_enabled: bool, has_ptz: bool) -> dict:
        return {
            "schema_version": 4,
            "devices": [
                {
                    "id": "front",
                    "enabled": camera_enabled,
                    "control": {"type": "onvif"},
                    "onvif": {"xaddr": "http://camera/onvif/device_service"},
                    "sources": [
                        {
                            "id": "zoom",
                            "kind": "video",
                            "enabled": source_enabled,
                            "is_default": True,
                            "role": "zoom",
                            "origin": {
                                "type": "onvif_profile",
                                "profile_token": "profile-zoom",
                                "has_ptz": has_ptz,
                            },
                        }
                    ],
                }
            ],
        }

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:

        async def acquire() -> dict[str, object]:
            return await client.app.state.services.call(
                "cameras.control.acquire",
                camera_id="front",
                camera_source_id="zoom",
                owner_kind="manual",
                owner_id="manual:test",
            )

        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload(camera_enabled=False, source_enabled=True, has_ptz=True),
        )
        assert response.status_code == 200
        with pytest.raises(PtzControlError, match="Camera is disabled"):
            client.portal.call(acquire)

        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload(camera_enabled=True, source_enabled=True, has_ptz=False),
        )
        assert response.status_code == 200
        with pytest.raises(PtzControlError, match="not marked as PTZ-capable"):
            client.portal.call(acquire)

        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload(camera_enabled=True, source_enabled=False, has_ptz=True),
        )
        assert response.status_code == 200
        with pytest.raises(PtzControlError, match="Unknown or disabled camera source"):
            client.portal.call(acquire)

        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload(camera_enabled=True, source_enabled=True, has_ptz=True),
        )
        assert response.status_code == 200
        lease = client.portal.call(acquire)

    assert lease["ptz_device_id"] == "front"


@pytest.mark.parametrize(
    ("camera_enabled_after_acquire", "source_enabled_after_acquire"),
    [(False, True), (True, False)],
)
def test_emergency_stop_keeps_known_disabled_camera_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    camera_enabled_after_acquire: bool,
    source_enabled_after_acquire: bool,
) -> None:
    from toposync_ext_cameras.onvif import OnvifClient

    physical_stops: list[dict[str, object]] = []

    async def stop(
        _self: OnvifClient,
        ptz_xaddr: str,
        *,
        profile_token: str,
        pan_tilt: bool = True,
        zoom: bool = True,
    ) -> None:
        physical_stops.append(
            {
                "ptz_xaddr": ptz_xaddr,
                "profile_token": profile_token,
                "pan_tilt": pan_tilt,
                "zoom": zoom,
            }
        )

    monkeypatch.setattr(OnvifClient, "stop", stop)

    def settings_payload(*, camera_enabled: bool, source_enabled: bool) -> dict:
        return {
            "schema_version": 4,
            "devices": [
                {
                    "id": "front",
                    "enabled": camera_enabled,
                    "control": {"type": "onvif"},
                    "onvif": {
                        "xaddr": "http://camera/onvif/device_service",
                        "ptz_xaddr": "http://camera/onvif/ptz",
                        "media_xaddr": "http://camera/onvif/media",
                    },
                    "sources": [
                        {
                            "id": "zoom",
                            "kind": "video",
                            "enabled": source_enabled,
                            "is_default": True,
                            "origin": {
                                "type": "onvif_profile",
                                "profile_token": "profile-zoom",
                                "has_ptz": True,
                            },
                        }
                    ],
                }
            ],
        }

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        configured = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload(camera_enabled=True, source_enabled=True),
        )
        assert configured.status_code == 200, configured.text

        async def acquire() -> dict[str, object]:
            return await client.app.state.services.call(
                "cameras.control.acquire",
                camera_id="front",
                camera_source_id="zoom",
                owner_kind="manual",
                owner_id="manual:test",
                ttl_s=30,
            )

        lease = client.portal.call(acquire)
        assert lease["ptz_device_id"] == "front"

        disabled = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload(
                camera_enabled=camera_enabled_after_acquire,
                source_enabled=source_enabled_after_acquire,
            ),
        )
        assert disabled.status_code == 200, disabled.text

        async def emergency_stop() -> dict[str, object]:
            return await client.app.state.services.call(
                "cameras.control.emergency_stop",
                camera_id="front",
                camera_source_id="zoom",
            )

        stopped = client.portal.call(emergency_stop)
        assert physical_stops == [
            {
                "ptz_xaddr": "http://camera/onvif/ptz",
                "profile_token": "profile-zoom",
                "pan_tilt": True,
                "zoom": True,
            }
        ]

    assert stopped["state_published"] is True
    assert physical_stops[0] == {
        "ptz_xaddr": "http://camera/onvif/ptz",
        "profile_token": "profile-zoom",
        "pan_tilt": True,
        "zoom": True,
    }


def test_ptz_lease_pins_onvif_transport_across_settings_hot_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif import OnvifClient
    from toposync_ext_cameras.ptz_controller import PtzControlError

    physical_calls: list[dict[str, object]] = []

    async def continuous_move(
        self: OnvifClient,
        ptz_xaddr: str,
        *,
        profile_token: str,
        pan: float,
        tilt: float,
        zoom: float,
        timeout_s: float | None = None,
    ) -> None:
        physical_calls.append(
            {
                "kind": "continuous_move",
                "xaddr": self.xaddr,
                "ptz_xaddr": ptz_xaddr,
                "profile_token": profile_token,
                "password": self.password,
                "pan": pan,
                "tilt": tilt,
                "zoom": zoom,
                "timeout_s": timeout_s,
            }
        )

    async def goto_preset(
        self: OnvifClient,
        ptz_xaddr: str,
        *,
        profile_token: str,
        preset_token: str,
    ) -> None:
        physical_calls.append(
            {
                "kind": "goto_preset",
                "xaddr": self.xaddr,
                "ptz_xaddr": ptz_xaddr,
                "profile_token": profile_token,
                "password": self.password,
                "preset_token": preset_token,
            }
        )

    async def stop(
        self: OnvifClient,
        ptz_xaddr: str,
        *,
        profile_token: str,
        pan_tilt: bool = True,
        zoom: bool = True,
    ) -> None:
        physical_calls.append(
            {
                "kind": "stop",
                "xaddr": self.xaddr,
                "ptz_xaddr": ptz_xaddr,
                "profile_token": profile_token,
                "password": self.password,
                "pan_tilt": pan_tilt,
                "zoom": zoom,
            }
        )

    monkeypatch.setattr(OnvifClient, "continuous_move", continuous_move)
    monkeypatch.setattr(OnvifClient, "goto_preset", goto_preset)
    monkeypatch.setattr(OnvifClient, "stop", stop)

    def settings_payload(device: str) -> dict[str, object]:
        return {
            "schema_version": 4,
            "devices": [
                {
                    "id": "front",
                    "enabled": True,
                    "control": {"type": "onvif"},
                    "onvif": {
                        "xaddr": f"http://device-{device}/onvif/device_service",
                        "ptz_xaddr": f"http://device-{device}/onvif/ptz",
                        "media_xaddr": f"http://device-{device}/onvif/media",
                        "username": f"user-{device}",
                        "password": f"secret-{device}",
                    },
                    "sources": [
                        {
                            "id": "zoom",
                            "kind": "video",
                            "enabled": True,
                            "is_default": True,
                            "origin": {
                                "type": "onvif_profile",
                                "profile_token": f"profile-{device}",
                                "has_ptz": True,
                            },
                        }
                    ],
                }
            ],
        }

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        configured_a = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload("A"),
        )
        assert configured_a.status_code == 200, configured_a.text

        async def acquire(owner_id: str) -> dict[str, object]:
            return await client.app.state.services.call(
                "cameras.control.acquire",
                camera_id="front",
                camera_source_id="zoom",
                owner_kind="manual",
                owner_id=owner_id,
                ttl_s=30,
            )

        async def submit(
            lease: dict[str, object],
            command_id: str,
            command: dict[str, object],
        ) -> dict[str, object]:
            return await client.app.state.services.call(
                "cameras.control.submit",
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id=command_id,
                command=command,
            )

        lease_a = client.portal.call(acquire, "manual:A")
        client.portal.call(
            submit,
            lease_a,
            "continuous-a",
            {"kind": "continuous_move", "pan": 0.25, "timeout_s": 2.0},
        )
        configured_b = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=settings_payload("B"),
        )
        assert configured_b.status_code == 200, configured_b.text

        with pytest.raises(PtzControlError, match="transport configuration changed"):
            client.portal.call(
                submit,
                lease_a,
                "blocked-goto-b",
                {"kind": "goto_preset", "preset_token": "door"},
            )
        assert [(call["kind"], call["xaddr"]) for call in physical_calls] == [
            ("continuous_move", "http://device-A/onvif/device_service")
        ]

        client.portal.call(
            submit,
            lease_a,
            "stop-a",
            {"kind": "stop", "pan_tilt": True, "zoom": True},
        )

        async def release(lease: dict[str, object]) -> dict[str, object]:
            return await client.app.state.services.call(
                "cameras.control.release",
                lease_id=lease["lease_id"],
                fence=lease["fence"],
            )

        client.portal.call(release, lease_a)
        lease_b = client.portal.call(acquire, "manual:B")
        client.portal.call(
            submit,
            lease_b,
            "goto-b",
            {"kind": "goto_preset", "preset_token": "door"},
        )

        assert [
            (call["kind"], call["xaddr"], call["ptz_xaddr"], call["profile_token"])
            for call in physical_calls
        ] == [
            (
                "continuous_move",
                "http://device-A/onvif/device_service",
                "http://device-A/onvif/ptz",
                "profile-A",
            ),
            (
                "stop",
                "http://device-A/onvif/device_service",
                "http://device-A/onvif/ptz",
                "profile-A",
            ),
            (
                "goto_preset",
                "http://device-B/onvif/device_service",
                "http://device-B/onvif/ptz",
                "profile-B",
            ),
        ]

        async def snapshot() -> dict[str, object]:
            return await client.app.state.services.call(
                "cameras.control.snapshot",
                ptz_device_id="front",
                include_readiness=False,
            )

        public_snapshot = client.portal.call(snapshot)
        persisted_state = client.app.state.camera_ptz_controller._state_path.read_text(
            encoding="utf-8"
        )  # noqa: SLF001
        public_payload = str(public_snapshot)
        for secret in ("secret-A", "secret-B", "user-A", "user-B"):
            assert secret not in public_payload
            assert secret not in persisted_state


def test_normalize_manual_camera_keeps_rtsp_source_credentials() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "devices": [
                {
                    "id": "front",
                    "name": "Front",
                    "control": {"type": "none"},
                    "sources": [
                        {
                            "id": "main",
                            "kind": "video",
                            "is_default": True,
                            "origin": {
                                "type": "rtsp",
                                "rtsp_url": "rtsp://127.0.0.1:8554/front",
                                "stream_username": "stream-user",
                                "stream_password": "stream-pass",
                            },
                        }
                    ],
                }
            ]
        }
    )

    source = normalized["devices"][0]["sources"][0]
    assert source["origin"]["type"] == "rtsp"
    assert source["origin"]["stream_username"] == "stream-user"
    assert source["origin"]["stream_password"] == "stream-pass"


def test_onvif_inspect_falls_back_to_common_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toposync_ext_cameras.onvif import OnvifError, OnvifProfile
    import toposync_ext_cameras.plugin as cameras_plugin

    attempts: list[str] = []

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
            self.xaddr = xaddr
            self.username = username
            self.password = password
            self.timeout_s = timeout_s
            self.auth_mode = auth_mode

        async def get_capabilities(self) -> tuple[str | None, str | None]:
            attempts.append(self.xaddr)
            if ":2020/" not in self.xaddr:
                raise OnvifError("<urlopen error [Errno 111] Connection refused>")
            return (
                "http://192.168.0.10:2020/onvif/service",
                "http://192.168.0.10:2020/onvif/service",
            )

        async def get_profiles(self, media_xaddr: str) -> list[OnvifProfile]:
            assert media_xaddr == "http://192.168.0.10:2020/onvif/service"
            return [
                OnvifProfile(
                    token="profile_1",
                    name="mainStream",
                    encoding="H264",
                    width=1920,
                    height=1080,
                    fps=25,
                )
            ]

        async def get_stream_uri(self, media_xaddr: str, *, profile_token: str) -> str:
            assert media_xaddr == "http://192.168.0.10:2020/onvif/service"
            assert profile_token == "profile_1"
            return "rtsp://192.168.0.10/stream1"

    monkeypatch.setattr(cameras_plugin, "OnvifClient", FakeOnvifClient)
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/cameras/onvif/inspect",
            json={
                "xaddr": "192.168.0.10",
                "username": "camera",
                "password": "secret",
                "timeout_ms": 500,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert attempts[:2] == [
        "http://192.168.0.10/onvif/device_service",
        "http://192.168.0.10:2020/onvif/device_service",
    ]
    assert body["xaddr"] == "http://192.168.0.10:2020/onvif/device_service"
    assert body["media_xaddr"] == "http://192.168.0.10:2020/onvif/service"
    assert body["profiles"][0]["stream_uri"] == "rtsp://192.168.0.10/stream1"


def test_normalize_ignores_legacy_cameras_key() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "schema_version": 2,
            "cameras": [{"id": "deleted", "name": "Deleted legacy camera"}],
            "devices": [
                {
                    "id": "kept",
                    "name": "Kept camera",
                    "control": {"type": "none"},
                    "sources": [
                        {
                            "id": "main",
                            "kind": "video",
                            "is_default": True,
                            "origin": {"type": "rtsp", "rtsp_url": "rtsp://127.0.0.1:8554/kept"},
                        }
                    ],
                }
            ],
        }
    )

    assert [item["id"] for item in normalized["devices"]] == ["kept"]


def test_camera_index_uses_devices_after_deleting_legacy_camera(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 4,
                "devices": [
                    {
                        "id": "back",
                        "name": "Back",
                        "kind": "camera",
                        "control": {"type": "none"},
                        "sources": [
                            {
                                "id": "main",
                                "name": "Main",
                                "kind": "video",
                                "enabled": True,
                                "is_default": True,
                                "role": "main",
                                "view_id": "main",
                                "origin": {"type": "rtsp", "rtsp_url": ""},
                                "video": {"fps": 5},
                                "ingest": {"mode": "centralized", "host_server_id": "local"},
                                "metadata": {},
                            }
                        ],
                        "metadata": {},
                    }
                ],
            },
        )
        assert res.status_code == 200

        res = client.get("/api/cameras/index")
        assert res.status_code == 200
        assert [item["id"] for item in res.json()["cameras"]] == ["back"]

        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"schema_version": 4, "devices": []},
        )
        assert res.status_code == 200

        res = client.get("/api/cameras/index")
        assert res.status_code == 200
        assert res.json()["cameras"] == []
