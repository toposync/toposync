from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from typing import Callable

from pydantic import ValidationError

from toposync.runtime.config_store import ConfigStore, Pipeline, PipelineValidationError

from .store import ReferenceConflict, VisualReferenceStore


def _configurations(pipelines: list[Pipeline]) -> dict[str, dict]:
    configurations = {}
    for pipeline in pipelines:
        nodes = pipeline.graph.get("nodes", [])
        for index, node in enumerate(nodes if isinstance(nodes, (list, tuple)) else []):
            if not isinstance(node, dict) or node.get("operator") != "vision.visual_state":
                continue
            # Position protects references even when a manually edited graph duplicates node identifiers.
            identity = json.dumps([pipeline.name, node.get("uid"), node.get("id"), index], separators=(",", ":"))
            owner = "pipeline_" + hashlib.sha256(identity.encode()).hexdigest()
            raw = node.get("config", {})
            if not isinstance(raw, dict):
                raw = {}
            configurations[owner] = raw
    return configurations


async def register_visual_reference_persistence(
    config_store: ConfigStore, get_store: Callable[[], VisualReferenceStore],
) -> None:
    async def reserve(previous: list[Pipeline] | None, proposed: list[Pipeline]) -> None:
        configurations = _configurations(proposed)
        if not configurations:
            return
        before = _configurations(previous) if previous is not None else configurations
        changed = {owner for owner, configuration in configurations.items() if before.get(owner) != configuration}
        try:
            await asyncio.to_thread(
                lambda: get_store().reserve_pipeline_configurations(configurations, validate_owners=changed),
            )
        except (ValidationError, ReferenceConflict, OSError, sqlite3.Error) as error:
            raise PipelineValidationError(
                "Visual sensor references cannot be saved; restore or recapture missing or incompatible images"
            ) from error

    await config_store.register_pipeline_resource_guard("com.toposync.vision", reserve)
