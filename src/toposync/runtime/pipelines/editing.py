from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from pydantic import Field

from ..config_store import Pipeline, PipelineValidationError
from .config_patch import merge_operator_config
from .operator_registry import OperatorRegistry
from .python_dsl import compile_python_source_to_graph


def pipeline_revision(pipeline: Pipeline) -> str:
    content = json.dumps(pipeline.model_dump(mode="json", exclude={"revision"}),
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


class PipelineEditRequest(Pipeline):
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class PipelineRead(Pipeline):
    revision: str

    @classmethod
    def from_pipeline(cls, pipeline: Pipeline) -> PipelineRead:
        return cls(**pipeline.model_dump(), revision=pipeline_revision(pipeline))


def prepare_pipeline_duplicate(pipeline: Pipeline, registry: OperatorRegistry) -> Pipeline:
    candidate = pipeline.model_copy(deep=True)
    # Compile under the destination name before choosing node-specific identities.
    if candidate.editor_mode == "python":
        candidate.graph = compile_python_source_to_graph(python_source=candidate.python_source,
            pipeline_name=candidate.name, registry=registry)
    patches = {}
    for node in candidate.graph.get("nodes", []):
        registered = registry.get(node["operator"])
        if registered is None or registered.duplicate_config is None:
            continue
        patch = registered.duplicate_config(deepcopy(node.get("config", {})))
        if not isinstance(patch, dict):
            raise PipelineValidationError("Operator duplication must return a configuration patch")
        node["config"] = merge_operator_config(node.get("config", {}), patch)
        registry.normalize_config(node["operator"], node["config"])
        patches[node["id"]] = patch
    if not patches:
        return candidate
    candidate.enabled = False
    candidate.graph["uid"] = candidate.name
    if candidate.editor_mode == "python":
        candidate.python_source = candidate.python_source.rstrip() + "\n\n# Review duplicated operator identities and publication before enabling.\n"
        for node_id, patch in patches.items():
            candidate.python_source += f"configure_node({node_id!r}, **{patch!r})\n"
        compiled = compile_python_source_to_graph(python_source=candidate.python_source,
            pipeline_name=candidate.name, registry=registry)
        expected = {node["id"]: registry.normalize_config(node["operator"], node["config"]) for node in candidate.graph["nodes"]}
        if expected != {node["id"]: node["config"] for node in compiled["nodes"]}:
            raise PipelineValidationError("Duplicated Python configuration does not reproduce its graph")
        candidate.graph = compiled
    return candidate
