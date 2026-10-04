from __future__ import annotations

import asyncio
import json
from pathlib import Path

from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator


def test_runtime_status_does_not_expose_internal_configuration(tmp_path: Path) -> None:
    async def scenario() -> None:
        paths = UserDataPaths(
            data_dir=tmp_path,
            config_path=tmp_path / "config.json",
            files_dir=tmp_path / "files",
        )
        store = ConfigStore(paths=paths)
        registry = OperatorRegistry()
        orchestrator = PipelinesOrchestrator(
            config_store=store,
            operator_registry=registry,
            compiler=PipelineGraphCompiler(registry),
            notifications=NotificationsRuntime(data_dir=tmp_path / "notifications"),
            files_dir=paths.files_dir,
        )
        try:
            for secret in ("synthetic-provider-secret", "rotated-provider-secret"):
                await store.replace_settings(
                    AppSettings(extensions={"test.provider": {"api_key": secret}})
                )
                await orchestrator._reconcile()

                # Reconciliation still tracks changed settings internally.
                assert secret in orchestrator._last_settings_sig
                status = orchestrator.status()
                assert "last_signature" not in status
                assert "last_settings_signature" not in status
                assert secret not in json.dumps(status)
                assert status["pipelines"] == []
                assert status["activation_errors"] == {}
        finally:
            await orchestrator.stop()

    asyncio.run(scenario())
