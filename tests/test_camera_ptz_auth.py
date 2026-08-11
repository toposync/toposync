from __future__ import annotations

from toposync.runtime.auth import AuthRuntime


def test_camera_permissions_are_explicit_and_not_granted_to_members_by_default() -> None:
    assert AuthRuntime.configurable_actions["core:camera"] == [
        "core:camera:read",
        "core:camera:control",
        "core:camera:configure",
    ]
    assert "core:camera:read" not in AuthRuntime.role_defaults["member"]
    assert "core:camera:control" not in AuthRuntime.role_defaults["member"]
    assert "core:camera:configure" not in AuthRuntime.role_defaults["member"]
