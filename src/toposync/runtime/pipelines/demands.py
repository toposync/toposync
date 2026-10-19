"""Local consumer leases; authorization and revocation are independent of plan rollback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from collections import deque
import hashlib
import json
from pathlib import Path
import time
from typing import Callable, Literal
import uuid
import weakref

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline, _atomic_write_json

from .result_references import ResultContract, ResultFieldName, ResultOutput, _ancestors
from .runtime import ChannelGetResult, ChannelPutResult, Lifecycle, Packet, QueueOperationStatus
from .runtime import _estimate_packet_artifact_bytes
from .inspection import frame_reference
from .templates import build_pipeline_graph_v2


class DemandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consumer_id: str = Field(min_length=1, max_length=128)
    purpose: Literal["automatic", "manual", "player", "publication", "inspection"]
    result_key: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{1,127}$")
    contract: ResultContract
    production_revision: str = Field(min_length=64, max_length=64)
    lease_seconds: float = Field(default=10, ge=0.1, le=30)
    payload_keys: list[ResultFieldName] = Field(default_factory=list, max_length=64)
    artifacts: list[ResultFieldName] = Field(default_factory=list, max_length=64)
    categories: list[ResultFieldName] = Field(default_factory=list, max_length=32)


@dataclass
class ResourceDemand:
    id: str
    owner: str
    request: DemandRequest
    expires_at: float
    state: str = "active"
    reason: str | None = None
    delivered: int = 0
    received: int = 0
    filtered: int = 0
    latest: Packet | None = None
    latest_expires_at: float = 0
    timer: asyncio.TimerHandle | None = None
    result_timer: asyncio.TimerHandle | None = None
    last_packet_id: str = ""
    consequence: dict | None = None
    resources: list[weakref.ReferenceType] = field(default_factory=list)


class FlowResultBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=128)
    node_uid: str = Field(min_length=1, max_length=256)
    request: DemandRequest


class ResourceDemandRegistry:
    maximum_demands = 128
    maximum_image_bytes = 8 * 1024 * 1024

    def __init__(
        self,
        authorize: Callable[[str, str, str], bool],
        *,
        compiler,
        clock=time.monotonic,
        revocations_path: Path | None = None,
    ):
        self.authorize = authorize
        self.compiler = compiler
        self.clock = clock
        self.demands: dict[str, ResourceDemand] = {}
        self.outputs: dict[str, tuple[Pipeline, ResultOutput, str]] = {}
        self.dependencies: dict[str, set[str]] = {}
        self.blocked: set[tuple[str, str, str]] = set()
        self.revocations_path = revocations_path
        if revocations_path is not None and revocations_path.exists():
            saved = json.loads(revocations_path.read_text())
            if saved.get("version") != 2 or not isinstance(saved.get("blocked"), list):
                raise ValueError("Invalid result revocation ledger")
            for item in saved["blocked"]:
                if (
                    not isinstance(item, list)
                    or len(item) != 3
                    or not all(isinstance(value, str) for value in item)
                ):
                    raise ValueError("Invalid result revocation ledger")
                self.blocked.add(tuple(item))
        self.revision = 0
        self.events = deque(maxlen=128)
        self.pipeline_allowed: Callable[[Pipeline], bool] = lambda _pipeline: True
        self.changed: Callable[[], None] = lambda: None
        self.invalidated: Callable[[ResourceDemand], None] = lambda _demand: None
        self.flow_leases: dict[str, str] = {}
        self.flow_status: dict[str, dict] = {}
        self._failed_flows: dict[str, str] = {}

    def synchronize_flows(self, pipelines: list[Pipeline]) -> list[Pipeline]:
        """Bind saved manual inputs to their own leases without persisting lease identifiers."""
        desired, retained = [], set()
        for pipeline in pipelines:
            raw = pipeline.graph.get("meta", {}).get("result_consumer")
            if raw is None:
                if pipeline.enabled:
                    desired.append(pipeline)
                continue
            name = pipeline.name
            retained.add(name)
            identifier = self.flow_leases.get(name)
            if not pipeline.enabled:
                if identifier:
                    self.release(identifier, "manual_flow_paused")
                    self.flow_leases.pop(name, None)
                self._failed_flows.pop(name, None)
                self.flow_status[name] = {"state": "paused", "reason": "manual_flow_paused"}
                try:
                    binding = FlowResultBinding.model_validate(raw)
                    current = self.outputs.get(binding.request.result_key)
                    if current and current[2] != binding.request.production_revision:
                        self.flow_status[name]["production_revision"] = current[2]
                except ValueError:
                    pass
                continue
            signature = hashlib.sha256(json.dumps(pipeline.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
            try:
                binding, _ = self.validate_flow(pipeline)
                if self._failed_flows.get(name) == signature:
                    continue
                if identifier not in self.demands:
                    demand = self.open(binding.owner, binding.request)
                    identifier = demand.id
                    self.flow_leases[name] = identifier
                else:
                    self.renew(identifier)
                bound = pipeline.model_copy(deep=True)
                for item in bound.graph["nodes"]:
                    if item["uid"] == binding.node_uid:
                        item["config"]["demand_id"] = identifier
                desired.append(bound)
                demand = self.demands[identifier]
                self.flow_status[name] = {
                    "state": "active" if demand.latest is not None and demand.latest.lifecycle != Lifecycle.CLOSE else "waiting",
                    "reason": None, "demand_id": identifier, "received": demand.received,
                }
            except (ValueError, PermissionError, StopIteration, KeyError, TypeError):
                self.reject_flow(pipeline, "result_binding_unavailable")
                # Authorization/revision checks are safe to repeat; activation failures are not.
                self._failed_flows.pop(name, None)
                try:
                    binding = FlowResultBinding.model_validate(raw)
                except ValueError:
                    continue
                key = binding.request.result_key
                current = self.outputs.get(key)
                if not self.pipeline_allowed(pipeline):
                    self.flow_status[name].update(state="blocked", reason="privacy_or_extension")
                elif self.is_blocked(binding.owner, key, "manual") or not self.authorized(binding.owner, key, "manual"):
                    self.flow_status[name].update(state="blocked", reason="result_access_denied")
                elif current and current[2] != binding.request.production_revision:
                    self.flow_status[name].update(reason="result_changed_review_required", production_revision=current[2])
        for name in set(self.flow_leases) - retained:
            self.release(self.flow_leases.pop(name), "manual_flow_removed")
        self._failed_flows = {k: v for k, v in self._failed_flows.items() if k in retained}
        self.flow_status = {k: v for k, v in self.flow_status.items() if k in retained}
        return desired

    def reject_flow(self, pipeline: Pipeline, reason: str) -> None:
        identifier = self.flow_leases.pop(pipeline.name, None)
        if identifier:
            self.release(identifier, "manual_flow_unavailable")
        self._failed_flows[pipeline.name] = hashlib.sha256(
            json.dumps(pipeline.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        self.flow_status[pipeline.name] = {"state": "unavailable", "reason": reason}

    def validate_flow(self, pipeline: Pipeline) -> tuple[FlowResultBinding, str]:
        binding = FlowResultBinding.model_validate(pipeline.graph["meta"]["result_consumer"])
        if binding.request.purpose != "manual" or pipeline.processing_server_id != "local":
            raise ValueError("Manual result flows require a local manual consumer")
        node = next((n for n in pipeline.graph["nodes"] if n["uid"] == binding.node_uid), None)
        if node is None or node["operator"] != "core.result_input" or {
            key: value for key, value in node["config"].items() if key != "demand_id"
        } != {
            "key": binding.request.result_key, "contract": binding.request.contract.model_dump(),
        }:
            raise ValueError("Result input changed; review its binding")
        self.validate_request(binding.owner, binding.request)
        identifier = node["config"].get("demand_id")
        if identifier is not None:
            demand = self.demands.get(identifier)
            if (identifier != self.flow_leases.get(pipeline.name) or demand is None
                or demand.owner != binding.owner or demand.request != binding.request
                or not self.allowed(identifier)):
                raise PermissionError("Manual result lease does not belong to this flow")
        return binding, node["id"]

    def bind(self, catalog: list[Pipeline]) -> None:
        outputs = {}
        dependencies = {}
        for pipeline in catalog:
            if not self.pipeline_allowed(pipeline):
                continue
            for raw in pipeline.graph.get("meta", {}).get("outputs", []):
                output = ResultOutput.model_validate(raw)
                if output.key in outputs:
                    raise ValueError("Duplicate result key")
                root = next(
                    (n["id"] for n in pipeline.graph["nodes"] if n["uid"] == output.node_uid), None
                )
                if root is None:
                    raise ValueError("Missing result endpoint")
                ancestors = _ancestors(pipeline.graph, root)
                dependencies[output.key] = {
                    node["config"]["key"]
                    for node in pipeline.graph["nodes"]
                    if node["id"] in ancestors and node["operator"] == "core.result_input"
                }
                compiled = self.compiler.compile_many([pipeline], catalog=catalog).pipelines[0]
                endpoint = next(node for node in compiled.nodes if node.uid == output.node_uid)
                semantic = {
                    "output": output.model_dump(exclude={"name"}),
                    "producer_signature": endpoint.signature,
                }
                revision = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
                outputs[output.key] = (pipeline, output, revision)
        self.outputs = outputs
        self.dependencies = dependencies
        for demand in list(self.demands.values()):
            current = outputs.get(demand.request.result_key)
            if current is None or current[2] != demand.request.production_revision:
                self.release(demand.id, "incompatible_result_revision")

    def validate_request(self, owner: str, request: DemandRequest) -> None:
        current = self.outputs.get(request.result_key)
        if (
            current is None
            or current[1].contract != request.contract
            or current[2] != request.production_revision
        ):
            raise ValueError("Incompatible result contract or production revision")
        if not set(request.payload_keys) <= set(request.contract.payload_keys) or not set(
            request.artifacts
        ) <= set(request.contract.artifacts):
            raise ValueError("Consumer projection exceeds result contract")
        if self.is_blocked(owner, request.result_key, request.purpose) or not self.authorized(
            owner, request.result_key, request.purpose
        ):
            raise PermissionError("Result access denied")

    def open(self, owner: str, request: DemandRequest) -> ResourceDemand:
        self.expire()
        self.validate_request(owner, request)
        if len(self.demands) >= self.maximum_demands:
            raise ValueError("Demand capacity")
        demand = ResourceDemand(
            uuid.uuid4().hex, owner, request, self.clock() + request.lease_seconds
        )
        self.demands[demand.id] = demand
        self._renew_timer(demand)
        self.revision += 1
        self.changed()
        return demand

    def _renew_timer(self, demand: ResourceDemand) -> None:
        if demand.timer:
            demand.timer.cancel()
        try:
            demand.timer = asyncio.get_running_loop().call_later(
                demand.request.lease_seconds, self.release, demand.id, "lease_expired"
            )
        except RuntimeError:
            pass

    def renew(self, identifier: str) -> None:
        if not self.allowed(identifier):
            raise PermissionError("Demand unavailable")
        demand = self.demands[identifier]
        demand.expires_at = self.clock() + demand.request.lease_seconds
        self._renew_timer(demand)

    def allowed(self, identifier: str) -> bool:
        demand = self.demands.get(identifier)
        if demand is None or demand.state != "active":
            return False
        if demand.expires_at <= self.clock():
            self.release(identifier, "lease_expired")
            return False
        producer = self.outputs.get(demand.request.result_key)
        if producer is not None and not self.pipeline_allowed(producer[0]):
            self.release(identifier, "analysis_blocked")
            return False
        try:
            authorized = self.authorized(
                demand.owner, demand.request.result_key, demand.request.purpose
            )
        except Exception:
            self.release(identifier, "authorization_unavailable")
            return False
        if (
            self.is_blocked(demand.owner, demand.request.result_key, demand.request.purpose)
            or not authorized
        ):
            self.blocked.add((demand.owner, demand.request.result_key, demand.request.purpose))
            self.release(identifier, "access_revoked")
            self._persist_revocations()
            return False
        return True

    def release(self, identifier: str, reason: str = "consumer_released") -> None:
        demand = self.demands.pop(identifier, None)
        if demand is None:
            return
        demand.state, demand.reason = "closed", reason
        if demand.timer:
            demand.timer.cancel()
        if demand.result_timer:
            demand.result_timer.cancel()
        demand.latest = None
        for reference in demand.resources:
            resource = reference()
            if resource is not None:
                resource.revoke()
        demand.resources.clear()
        self.events.append(
            {
                "demand_id": demand.id,
                "owner": demand.owner,
                "purpose": demand.request.purpose,
                "reason": reason,
            }
        )
        self.invalidated(demand)
        self.revision += 1
        self.changed()

    def revoke(self, owner: str, result_key: str) -> None:
        self.blocked.add((owner, result_key, "*"))
        for demand in list(self.demands.values()):
            if self.is_blocked(demand.owner, demand.request.result_key, demand.request.purpose):
                self.release(demand.id, "access_revoked")
        self._persist_revocations()

    def is_blocked(self, owner: str, result_key: str, purpose: str = "*") -> bool:
        seen, pending = set(), [result_key]
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            if any(
                (actor, key, selected) in self.blocked
                for actor in (owner, "*")
                for selected in (purpose, "*")
            ):
                return True
            seen.add(key)
            pending.extend(self.dependencies.get(key, ()))
        return False

    def authorized(self, owner: str, result_key: str, purpose: str) -> bool:
        producer = self.outputs.get(result_key)
        if producer is not None and not self.pipeline_allowed(producer[0]):
            return False
        if purpose == "catalog":
            return any(
                self.authorized(owner, result_key, selected)
                for selected in (
                    "read",
                    "manual",
                    "automatic",
                    "player",
                    "publication",
                    "inspection",
                )
            )
        seen, pending = set(), [result_key]
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            if self.is_blocked(owner, key, purpose) or not self.authorize(owner, key, purpose):
                return False
            seen.add(key)
            pending.extend(self.dependencies.get(key, ()))
        return True

    def _persist_revocations(self) -> None:
        if self.revocations_path is not None:
            _atomic_write_json(
                self.revocations_path, {"version": 2, "blocked": sorted(self.blocked)}
            )

    def pipeline_blocked(self, pipeline: Pipeline) -> bool:
        if not self.pipeline_allowed(pipeline):
            return True
        keys = [item["key"] for item in pipeline.graph.get("meta", {}).get("outputs", [])]
        keys.extend(
            node["config"]["key"]
            for node in pipeline.graph.get("nodes", [])
            if node["operator"] == "core.result_input"
        )
        return any(self.is_blocked("", key) for key in keys)

    def expire(self) -> None:
        for identifier in list(self.demands):
            self.allowed(identifier)

    def pipelines(self) -> list[Pipeline]:
        self.expire()
        pipelines = []
        for demand in self.demands.values():
            if demand.request.purpose == "inspection":
                continue
            graph = build_pipeline_graph_v2(
                graph_uid=f"demand_{demand.id}",
                nodes=[
                    {
                        "id": "result",
                        "operator": "core.result_input",
                        "config": {
                            "key": demand.request.result_key,
                            "contract": demand.request.contract.model_dump(),
                            "demand_id": demand.id,
                        },
                    },
                    {
                        "id": "consumer",
                        "operator": "core.demand_sink",
                        "config": {"demand_id": demand.id},
                    },
                ],
                edges=[
                    {
                        "from": {"node": "result", "port": "out"},
                        "to": {"node": "consumer", "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "drop_oldest",
                    }
                ],
            )
            if demand.consequence is not None:
                durable = demand.consequence["operator"] == "core.record_occurrence"
                graph = build_pipeline_graph_v2(
                    graph_uid=f"demand_{demand.id}",
                    nodes=[*graph["nodes"], {"id": "consequence", **demand.consequence}],
                    edges=[*graph["edges"], {
                        "from": {"node": "result", "port": "out"},
                        "to": {"node": "consequence", "port": "in"},
                        "maxsize": 16, "drop_policy": "drop_oldest",
                        **({"queue": {"max_items": 16, "drop_policy": "block"},
                            "backpressure": {"mode": "block"},
                            "lifecycle": {"compact_updates": False}} if durable else {}),
                    }],
                )
            pipelines.append(Pipeline(name=f"demand_{demand.id}", graph=graph))
        return pipelines

    def project(self, identifier: str, packet: Packet) -> Packet | None:
        if not self.allowed(identifier):
            return None
        demand = self.demands[identifier]
        if packet.lifecycle.value != "close" and (
            not set(demand.request.contract.payload_keys) <= packet.payload.keys()
            or not set(demand.request.contract.artifacts) <= packet.artifacts.keys()
        ):
            demand.reason = "result_contract_violation"
            return None
        categories = demand.request.categories
        subject = packet.payload.get("subject")
        if (
            categories
            and packet.lifecycle.value != "close"
            and (not isinstance(subject, dict) or subject.get("category") not in categories)
        ):
            demand.filtered += 1
            return None
        return replace(
            packet,
            payload={
                key: packet.payload[key]
                for key in demand.request.payload_keys
                if key in packet.payload
            },
            artifacts={
                key: packet.artifacts[key]
                for key in demand.request.artifacts
                if key in packet.artifacts
            },
            metadata={"frame": frame_reference(packet)},
        )

    def receive(self, identifier: str, packet: Packet) -> None:
        if self.allowed(identifier):
            demand = self.demands[identifier]
            if packet.packet_id == demand.last_packet_id or packet.age_ms() >= 1000:
                return
            image_bytes = _estimate_packet_artifact_bytes(packet)
            retained = sum(
                _estimate_packet_artifact_bytes(item.latest)
                for item in self.demands.values()
                if item.latest is not None and item.id != identifier
            )
            if retained + image_bytes > self.maximum_image_bytes:
                demand.latest = None
                demand.reason = "inspection_capacity"
                return
            try:
                if len(json.dumps(packet.payload, allow_nan=False).encode()) > 262144:
                    raise ValueError("Result projection too large")
            except (TypeError, ValueError, RecursionError):
                demand.latest = None
                demand.reason = "result_capacity"
                return
            demand.latest = packet
            demand.last_packet_id = packet.packet_id
            age = packet.age_ms() / 1000
            demand.latest_expires_at = self.clock() + max(0, 1 - age)
            if demand.result_timer:
                demand.result_timer.cancel()
            try:
                demand.result_timer = asyncio.get_running_loop().call_later(
                    max(0, 1 - age), self._expire_result, identifier, packet.packet_id
                )
            except RuntimeError:
                pass

    def _expire_result(self, identifier: str, packet_id: str) -> None:
        demand = self.demands.get(identifier)
        if (
            demand is not None
            and demand.latest is not None
            and demand.latest.packet_id == packet_id
        ):
            demand.latest = None

    def snapshot(self, owner: str) -> list[dict]:
        self.expire()
        return [
            {
                "id": item.id,
                "consumer_id": item.request.consumer_id,
                "purpose": item.request.purpose,
                "result_key": item.request.result_key,
                "remaining_seconds": max(0, item.expires_at - self.clock()),
                "producing": item.request.purpose != "inspection",
                "delivered": item.delivered,
                "received": item.received,
                "filtered": item.filtered,
                "reason": item.reason,
            }
            for item in self.demands.values()
            if item.owner == owner
        ]


class ConsumerChannel:
    def __init__(self, channel, registry: ResourceDemandRegistry, identifier: str):
        self.channel, self.registry, self.identifier = channel, registry, identifier
        resources = registry.demands[identifier].resources
        resources[:] = [reference for reference in resources if reference() is not None]
        if len(resources) >= 32:
            raise ValueError("Consumer resource capacity")
        resources.append(weakref.ref(self))

    def __getattr__(self, name):
        return getattr(self.channel, name)

    async def put(self, packet, **kwargs):
        projected = self.registry.project(self.identifier, packet)
        if projected is None:
            return ChannelPutResult(status=QueueOperationStatus.DROPPED)
        result = await self.channel.put(projected, **kwargs)
        if not self.registry.allowed(self.identifier):
            self.channel.revoke()
            return ChannelPutResult(status=QueueOperationStatus.CANCELED)
        if result.accepted:
            self.registry.demands[self.identifier].delivered += 1
        return result

    async def get(self, **kwargs):
        if not self.registry.allowed(self.identifier):
            self.channel.revoke()
            return ChannelGetResult(status=QueueOperationStatus.CANCELED)
        result = await self.channel.get(**kwargs)
        if not self.registry.allowed(self.identifier):
            self.channel.revoke()
            return ChannelGetResult(status=QueueOperationStatus.CANCELED)
        if result.status == QueueOperationStatus.ACCEPTED:
            self.registry.demands[self.identifier].received += 1
        return result


def register_demand_operators(registry) -> None:
    from .execution import PassThroughRuntime, SinkRuntime
    from .result_references import ResultReference

    class ResultInputDraft(ResultReference):
        # The compiler validates ResultReference before expanding an editor draft.
        key: str = ""
        contract: ResultContract = Field(default_factory=lambda: ResultContract(version=1))

    registry.register_operator(
        operator_id="core.result_input", config_model=ResultInputDraft,
        description="A named result consumed through an independently authorized demand.",
        inputs=[], outputs=[{"name": "out"}], owner="core",
        share_strategy="never",
    )

    class DemandConfig(BaseModel):
        model_config = ConfigDict(extra="forbid")
        demand_id: str = Field(default="", max_length=32)

    class UnboundConfig(BaseModel):
        model_config = ConfigDict(extra="forbid")

    registry.register_operator(
        operator_id="core.demand_required",
        config_model=UnboundConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        share_strategy="never",
        owner="core",
    )

    class DemandSink(SinkRuntime):
        def __init__(self, config, dependencies):
            self.identifier = config["demand_id"]
            self.registry = dependencies.demand_registry
            if self.registry is None or not self.registry.allowed(self.identifier):
                raise PermissionError("Demand unavailable")

        async def prepare(self, context):
            if not self.registry.allowed(self.identifier):
                raise PermissionError("Demand expired during preparation")

        async def process_packet(self, packet, context):
            self.registry.receive(self.identifier, packet)
            return []

    for identifier in ("core.demand_consumer", "core.demand_sink"):
        registry.register_operator(
            operator_id=identifier,
            config_model=DemandConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[] if identifier.endswith("sink") else [{"name": "out"}],
            share_strategy="never",
            owner="core",
            runtime_factory=(lambda config, dependencies: DemandSink(config, dependencies))
            if identifier.endswith("sink")
            else (lambda _config, _dependencies: PassThroughRuntime()),
        )
