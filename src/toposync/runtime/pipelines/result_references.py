"""Stable packet results expand into the existing DAG before execution."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline
from .images import MAIN_ARTIFACT_NAME, normalize_artifact_name

ResultFieldName = Annotated[str, Field(min_length=1, max_length=128)]


class ResultContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["packet"] = "packet"
    version: int = Field(ge=1)
    payload_keys: list[ResultFieldName] = Field(default_factory=list, max_length=64)
    artifacts: list[ResultFieldName] = Field(default_factory=list, max_length=64)


class ResultOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{1,127}$")
    name: str = Field(default="", max_length=160)
    node_uid: str = Field(min_length=1, max_length=256)
    port: str = "out"
    contract: ResultContract
    production_scope: str = Field(min_length=1, max_length=256)
    state_owner: str | None = Field(default=None, min_length=1, max_length=128)
    state_partition: str | None = Field(default=None, min_length=1, max_length=128)


class ResultReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{1,127}$")
    contract: ResultContract
    demand_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def _ancestors(graph: dict, root: str) -> set[str]:
    incoming: dict[str, list[str]] = {}
    for edge in graph.get("edges", []):
        incoming.setdefault(edge["to"]["node"], []).append(edge["from"]["node"])
    selected, pending = set(), [root]
    while pending:
        node = pending.pop()
        if node in selected:
            continue
        selected.add(node)
        pending.extend(incoming.get(node, []))
    return selected


def _validate_contract(graph: dict, node_id: str, output: ResultOutput, registry) -> None:
    from .compiler import PipelineGraphCompiler

    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="result_contract", graph=graph)
    )
    incoming: dict[str, list] = {}
    for edge in compiled.edges:
        incoming.setdefault(edge.target_node_id, []).append(edge)
    guarantees = {}
    for node in compiled.nodes:
        definition = registry.get(node.operator_id).definition
        paths = incoming.get(node.node_id, [])
        primary = [edge for edge in paths if edge.target_port == "in"]
        paths = primary or paths
        payload = [guarantees[(edge.source_node_id, edge.source_port)][0] for edge in paths]
        artifacts = [guarantees[(edge.source_node_id, edge.source_port)][1] for edge in paths]
        common_payload = set.intersection(*payload) if payload else set()
        common_artifacts = set.intersection(*artifacts) if artifacts else set()
        for port in definition.outputs:
            keys = set(common_payload) if port.preserves_input_contract else set()
            names = set(common_artifacts) if port.preserves_input_contract else set()
            if port.payload_keys_allowlist_field:
                keys.intersection_update(
                    node.normalized_config.get(port.payload_keys_allowlist_field) or []
                )
            if port.artifact_names_allowlist_field:
                names.intersection_update(
                    node.normalized_config.get(port.artifact_names_allowlist_field) or []
                )
            keys.update(definition.produces_payload_keys)
            produced = set(definition.produces_artifacts)
            artifact_name = normalize_artifact_name(
                node.normalized_config.get("output_artifact_name"), default=""
            )
            if artifact_name and MAIN_ARTIFACT_NAME in produced:
                produced.remove(MAIN_ARTIFACT_NAME)
                produced.add(artifact_name)
            names.update(produced)
            guarantees[(node.node_id, port.name)] = (keys, names)
    actual = guarantees.get((node_id, output.port))
    if (
        actual is None
        or not set(output.contract.payload_keys) <= actual[0]
        or not set(output.contract.artifacts) <= actual[1]
    ):
        raise ValueError(f"Result {output.key} contract is not guaranteed by its output")


def expand_result_references(
    pipelines: list[Pipeline], catalog: list[Pipeline], registry
) -> list[Pipeline]:
    outputs = {}
    by_pipeline = {}
    for pipeline in catalog:
        declarations = [
            ResultOutput.model_validate(item)
            for item in pipeline.graph.get("meta", {}).get("outputs", [])
        ]
        by_pipeline[pipeline.name] = declarations
        for output in declarations:
            if output.key in outputs:
                raise ValueError(f"Duplicate result key: {output.key}")
            outputs[output.key] = (pipeline, output)

    def expand(
        pipeline: Pipeline, selected: set[str] | None = None, trail: tuple[str, ...] = ()
    ) -> dict:
        graph = deepcopy(pipeline.graph)
        if selected is not None:
            graph["nodes"] = [node for node in graph["nodes"] if node["id"] in selected]
            graph["edges"] = [
                edge
                for edge in graph["edges"]
                if edge["from"]["node"] in selected and edge["to"]["node"] in selected
            ]
        reference_scopes = {}
        for output in by_pipeline.get(pipeline.name, []):
            root = next(
                (node["id"] for node in graph["nodes"] if node["uid"] == output.node_uid), None
            )
            if root is None:
                continue
            ancestors = _ancestors(graph, root)
            for node in graph["nodes"]:
                if node["id"] not in ancestors:
                    continue
                if node["operator"] == "core.result_input":
                    if (
                        reference_scopes.get(node["id"], output.production_scope)
                        != output.production_scope
                    ):
                        raise ValueError("Conflicting dependency production scopes")
                    reference_scopes[node["id"]] = output.production_scope
                    continue
                operator = registry.get(node["operator"])
                if operator is None:
                    raise ValueError(f"Unknown result operator: {node['operator']}")
                if operator.definition.state_kind == "external_side_effect":
                    raise ValueError("An effect cannot be a reusable result dependency")
                state = node.setdefault("state", {})
                if (
                    state.get("production_scope", output.production_scope)
                    != output.production_scope
                ):
                    raise ValueError("Conflicting production scopes for a result dependency")
                state["production_scope"] = output.production_scope
                if operator.definition.state_kind.startswith("stateful"):
                    if not output.state_owner or not output.state_partition:
                        raise ValueError("Stateful result requires explicit owner and partition")
                    state.update(
                        resource_owner=output.state_owner, resource_partition=output.state_partition
                    )

        for node in list(graph["nodes"]):
            if node["operator"] != "core.result_input":
                continue
            reference = ResultReference.model_validate(node.get("config", {}))
            if reference.key in trail or len(trail) >= 128:
                raise ValueError("Cycle in reusable result dependencies")
            if reference.key not in outputs:
                raise ValueError(f"Unknown result key: {reference.key}")
            producer, output = outputs[reference.key]
            if (
                node["id"] in reference_scopes
                and reference_scopes[node["id"]] != output.production_scope
            ):
                raise ValueError("Incompatible dependency production scope")
            if producer.processing_server_id != pipeline.processing_server_id:
                raise ValueError("Remote result dependencies are not supported")
            if reference.contract != output.contract:
                raise ValueError(f"Incompatible result contract/version: {reference.key}")
            root = next(
                (n["id"] for n in producer.graph["nodes"] if n["uid"] == output.node_uid), None
            )
            if root is None:
                raise ValueError(f"Missing result endpoint: {reference.key}")
            dependency = expand(producer, _ancestors(producer.graph, root), (*trail, reference.key))
            _validate_contract(dependency, root, output, registry)
            prefix = "result_" + hashlib.sha256(reference.key.encode()).hexdigest()[:16] + "_"
            existing = {n["id"] for n in graph["nodes"]}
            for imported in dependency["nodes"]:
                imported.setdefault("state", {}).setdefault(
                    "result_origin", {"pipeline": producer.name, "node": imported["id"]}
                )
                imported = {
                    **imported,
                    "id": prefix + imported["id"],
                    "uid": prefix + imported["uid"],
                }
                if imported["id"] not in existing:
                    graph["nodes"].append(imported)
            edge_ids = {edge["uid"] for edge in graph["edges"]}
            for imported in dependency["edges"]:
                imported = {
                    **imported,
                    "uid": prefix + imported["uid"],
                    "from": {**imported["from"], "node": prefix + imported["from"]["node"]},
                    "to": {**imported["to"], "node": prefix + imported["to"]["node"]},
                }
                if imported["uid"] not in edge_ids:
                    graph["edges"].append(imported)
            node.update(
                operator="core.passthrough"
                if node["id"] in reference_scopes
                else "core.demand_consumer"
                if reference.demand_id
                else "core.demand_required",
                config={"demand_id": reference.demand_id}
                if reference.demand_id and node["id"] not in reference_scopes
                else {},
            )
            graph["edges"].append(
                {
                    "uid": f"result:{node['uid']}",
                    "from": {"node": prefix + root, "port": output.port},
                    "to": {"node": node["id"], "port": "in"},
                }
            )
        return graph

    return [pipeline.model_copy(update={"graph": expand(pipeline)}) for pipeline in pipelines]
