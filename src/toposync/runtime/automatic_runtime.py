"""Resolve product intentions into the existing pipeline and demand contracts."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .automatic import AutomaticState
from .config_store import Pipeline, PipelineAlreadyExistsError
from .pipelines.demands import DemandRequest
from .pipelines.operators_gates import (
    ScheduleGateConfig,
    _normalize_weekdays,
    evaluate_schedule_gate,
)


class AutomaticController:
    def __init__(self, *, store, services, operators, demands, compiler):
        self.store, self.services, self.operators = store, services, operators
        self.demands, self.compiler = demands, compiler
        self.config = None
        self.state = AutomaticState()
        self.capabilities = {}
        self.leases = {}
        self.statuses = {}
        self.last_results = {}
        self._privacy_barrier = None
        self.revoke = None
        self.failed_demands = {}
        self.demands.pipeline_allowed = self.pipeline_allowed

    async def catalog(self, config=None):
        config = config or self.config
        entries = [
            {
                "id": "core.record",
                "kind": "consequence",
                "extension_id": "core",
                "title": {"en": "Record activity locally", "pt-BR": "Registrar atividade local"},
                "operators": ["core.notify"],
                "required_consents": [],
                "fields": [],
            }
        ]
        for identifier in self.services.list_ids("automatic.catalog."):
            entries.extend(await self.services.call(identifier))
        disabled = (
            set(
                config.settings.core.get("extension_management", {}).get(
                    "disabled_extension_ids", []
                )
            )
            if config
            else set()
        )
        found = {}
        for entry in entries:
            if entry["id"] in found:
                raise ValueError("Duplicate automatic capability")
            missing = [
                operator
                for operator in entry.get("operators", [])
                if self.operators.get(operator) is None
                or self.operators.get(operator).owner in disabled
            ]
            found[entry["id"]] = {
                **entry,
                "available": not missing and entry["extension_id"] not in disabled,
                "missing_operators": missing,
            }
        self.capabilities = found
        return list(found.values())

    def prepare_config(self, config):
        self.config = config
        self.state = AutomaticState.model_validate(config.settings.core.get("automatic", {}))

    def pipeline_allowed(self, pipeline):
        if self.config is None:
            return True
        outputs = {
            output["key"]: item
            for item in self.config.pipelines
            for output in item.graph.get("meta", {}).get("outputs", [])
        }
        nodes, pending, seen = [], [pipeline], set()
        while pending:
            current = pending.pop()
            if current.name in seen:
                continue
            seen.add(current.name)
            nodes.extend(current.graph.get("nodes", []))
            for node in current.graph.get("nodes", []):
                if node["operator"] == "core.result_input":
                    producer = outputs.get(node["config"]["key"])
                    if producer is None:
                        return False
                    pending.append(producer)
        policy = self._privacy_barrier or self.state
        cameras = {
            str(node.get("config", {}).get("camera_id"))
            for node in nodes
            if node.get("config", {}).get("camera_id")
        }
        disabled = set(
            self.config.settings.core.get("extension_management", {}).get(
                "disabled_extension_ids", []
            )
        )
        features = set()
        for node in nodes:
            # Result references are expanded by the compiler, not registered as operators.
            if node["operator"] == "core.result_input":
                continue
            operator = self.operators.get(node["operator"])
            if operator is None or operator.owner in disabled:
                return False
            features.update(operator.definition.capabilities)
        if features.intersection(
            {"vision", "tracking", "ai", "audio", "motion"}
        ) and cameras.intersection(policy.blocked_camera_ids):
            return False
        return all(
            policy.permits(feature, list(cameras))
            for feature in ("tracking", "recognition", "audio", "external", "actuation")
            if feature in features
        )

    async def preview(self, state: AutomaticState, config):
        await self.catalog(config)
        plans, statuses = [], {}
        for original in state.intents:
            capability = self.capabilities.get(original.capability)
            consequence = self.capabilities.get(original.consequence.capability)
            if (
                capability is None
                or not capability["available"]
                or consequence is None
                or not consequence["available"]
            ):
                statuses[original.id] = {
                    "state": "unavailable",
                    "reason": "extension_unavailable",
                    "sources": [],
                }
                continue
            required = set(capability.get("required_consents", [])) | set(
                consequence.get("required_consents", [])
            )
            if not required <= set(original.consents):
                raise ValueError(
                    "Specific consent required: "
                    + ", ".join(sorted(required - set(original.consents)))
                )
            defaults = state.defaults.get(original.capability, {})
            if not isinstance(defaults, dict):
                raise ValueError("Capability defaults must be an object")
            effective = original.model_copy(
                update={"condition": {**defaults, **original.condition, **original.customizations}}
            )
            plan = await self.services.call(
                capability["planner"], intent=effective, configuration=config
            )
            sources = plan.get("sources", [])
            cameras = [source["camera_id"] for source in sources]
            denied = [
                feature
                for feature in capability.get("required_features", []) + list(required)
                if not state.permits(feature, cameras)
            ]
            if denied or set(cameras).intersection(state.blocked_camera_ids):
                statuses[original.id] = {
                    "state": "blocked",
                    "reason": "privacy_policy",
                    "sources": sources,
                }
                continue
            statuses[original.id] = {
                "state": plan.get("state", "preparing"),
                "reason": plan.get("reason"),
                "sources": sources,
                "limitations": plan.get("limitations", []),
            }
            graph = plan.get("graph")
            if graph is None:
                continue
            if original.consequence.capability == "core.record":
                effect = {
                    "operator": "core.notify",
                    "config": {
                        "notification_type": "pipelines.automatic",
                        "title": original.name,
                        "description": "{{subject.category}}",
                        "dedupe_by_occurrence": True,
                        "dedupe_key_template": "{{subject.id}}",
                    },
                }
            else:
                effect = await self.services.call(
                    consequence["planner"], consequence=original.consequence, configuration=config
                )
            effect_operator = self.operators.get(effect["operator"])
            if effect_operator is None:
                raise ValueError("Consequence operator unavailable")
            effect["config"] = effect_operator.config_model.model_validate(
                effect["config"]
            ).model_dump(mode="json")
            graph.setdefault("meta", {})["automatic"] = {
                "intent_id": original.id,
                "revision": original.revision,
                "sources": sources,
                "consequence": effect,
                "capability": original.capability,
                "status": statuses[original.id],
            }
            pipeline = Pipeline(
                name="automatic_" + original.id,
                display_name=original.name,
                enabled=False,
                editor_mode="interactive",
                graph=graph,
            )
            plans.append(pipeline)
        previous_ids = {
            item.id
            for item in AutomaticState.model_validate(
                config.settings.core.get("automatic", {})
            ).intents
        }
        manual = [
            p
            for p in config.pipelines
            if p.graph.get("meta", {}).get("automatic", {}).get("intent_id") not in previous_ids
        ]
        for pipeline in plans:
            compiled = self.compiler.compile_many([pipeline], catalog=[*manual, *plans]).pipelines[
                0
            ]
            info = pipeline.graph["meta"]["automatic"]
            intent = next(item for item in state.intents if item.id == info["intent_id"])
            operators = [self.operators.get(node.operator_id) for node in compiled.nodes]
            operators.append(self.operators.get(info["consequence"]["operator"]))
            features = {
                feature for operator in operators for feature in operator.definition.capabilities
            }
            sensitive = features.intersection({"recognition", "audio", "external", "actuation"})
            if not sensitive <= set(intent.consents):
                raise ValueError(
                    "Specific consent required: "
                    + ", ".join(sorted(sensitive - set(intent.consents)))
                )
            cameras = [source["camera_id"] for source in info["sources"]]
            if any(
                not state.permits(feature, cameras)
                for feature in sensitive | (features & {"tracking"})
            ):
                raise ValueError("Activity conflicts with the privacy policy")
        return plans, statuses

    async def apply(self, state, *, expected_revision, owner):
        config = await self.store.load()
        previous = AutomaticState.model_validate(config.settings.core.get("automatic", {}))
        if previous.revision != expected_revision:
            raise PipelineAlreadyExistsError("Automatic revision changed; refresh before saving")
        previous_by_id = {intent.id: intent for intent in previous.intents}
        intents = []
        for intent in state.intents:
            old = previous_by_id.get(intent.id)
            intent = intent.model_copy(update={"owner_id": old.owner_id if old else owner})
            revision = (
                1
                if old is None
                else old.revision
                + (intent.model_dump(exclude={"revision"}) != old.model_dump(exclude={"revision"}))
            )
            intents.append(intent.model_copy(update={"revision": revision}))
        state = state.model_copy(update={"revision": previous.revision + 1, "intents": intents})
        # The in-memory privacy barrier precedes preparation, persistence and ordinary plan swaps.
        restrictive = bool(set(state.blocked_camera_ids) - set(previous.blocked_camera_ids)) or any(
            set(features) - set(previous.denied_features.get(scope, []))
            for scope, features in state.denied_features.items()
        )
        if restrictive:
            self.config = config
            policies = [previous, state, self._privacy_barrier or previous]
            self._privacy_barrier = state.model_copy(
                update={
                    "blocked_camera_ids": sorted(
                        {camera for policy in policies for camera in policy.blocked_camera_ids}
                    ),
                    "denied_features": {
                        scope: sorted(
                            {
                                feature
                                for policy in policies
                                for feature in policy.denied_features.get(scope, [])
                            }
                        )
                        for scope in {
                            scope for policy in policies for scope in policy.denied_features
                        }
                    },
                }
            )
            self.demands.expire()
            if self.revoke is not None:
                await self.revoke()
        plans, statuses = await self.preview(state, config)
        await self.store.commit_automatic(state, expected_config=config, derived_pipelines=plans)
        self.config = await self.store.load()
        self.state, self.statuses = state, statuses
        self._privacy_barrier = None
        return state

    def _scheduled(self, intent):
        condition = {
            **self.state.defaults.get(intent.capability, {}),
            **intent.condition,
            **intent.customizations,
        }
        raw = condition.get("schedule")
        if raw is None:
            return True
        schedule = ScheduleGateConfig.model_validate(raw)
        if not schedule.enabled:
            return True
        timezone = schedule.timezone or self.config.settings.core.get("timezone")
        if not timezone:
            raise ValueError("Installation timezone is required for a schedule")
        now = datetime.now(ZoneInfo(timezone))
        return evaluate_schedule_gate(
            now=now,
            weekdays=set(_normalize_weekdays(schedule.weekdays)),
            start_time=schedule.start_time,
            end_time=schedule.end_time,
        ).is_open

    async def synchronize(self, config):
        self.prepare_config(config)
        await self.catalog()
        active = set()
        by_intent = {
            p.graph["meta"]["automatic"]["intent_id"]: p
            for p in config.pipelines
            if p.graph.get("meta", {}).get("automatic")
        }
        for intent in self.state.intents:
            pipeline = by_intent.get(intent.id)
            info = pipeline.graph["meta"]["automatic"] if pipeline else {}
            status = dict(
                info.get("status")
                or self.statuses.get(intent.id)
                or {"state": "unavailable", "reason": "preparation_required", "sources": []}
            )
            if not self.state.enabled or not intent.requested:
                status.update(
                    state="paused",
                    reason="automatic_paused" if not self.state.enabled else "activity_paused",
                )
            elif any(
                not self.capabilities.get(identifier, {}).get("available", False)
                for identifier in (intent.capability, intent.consequence.capability)
            ):
                status.update(state="unavailable", reason="extension_unavailable")
            elif pipeline is not None and not self.pipeline_allowed(pipeline):
                status.update(state="blocked", reason="privacy_or_extension")
            elif pipeline is not None:
                try:
                    scheduled = self._scheduled(intent)
                except (ValueError, KeyError):
                    status.update(state="unavailable", reason="schedule_invalid")
                    scheduled = None
                if scheduled is False:
                    status.update(state="outside_schedule", reason="schedule_closed")
                elif scheduled is True:
                    output_key = pipeline.graph["meta"]["outputs"][0]["key"]
                    current = self.demands.outputs.get(output_key)
                    if current is None:
                        status.update(state="unavailable", reason="result_unavailable")
                    else:
                        identifier = self.leases.get(intent.id)
                        signature = (self.state.revision, current[2])
                        try:
                            if self.failed_demands.get(intent.id) == signature:
                                raise PermissionError(
                                    "Review the unavailable demand before retrying"
                                )
                            if identifier not in self.demands.demands:
                                _, output, revision = current
                                demand = self.demands.open(
                                    intent.owner_id,
                                    DemandRequest(
                                        consumer_id="automatic:" + intent.id,
                                        purpose="automatic",
                                        result_key=output_key,
                                        contract=output.contract,
                                        production_revision=revision,
                                        lease_seconds=30,
                                        payload_keys=output.contract.payload_keys,
                                        artifacts=output.contract.artifacts,
                                    ),
                                )
                                identifier = demand.id
                                self.leases[intent.id] = identifier
                            else:
                                self.demands.renew(identifier)
                            demand = self.demands.demands[identifier]
                            if demand.consequence != info["consequence"]:
                                demand.consequence = info["consequence"]
                                self.demands.revision += 1
                                self.demands.changed()
                            active.add(intent.id)
                            status.update(
                                state="active" if demand.received else "waiting",
                                reason=None if demand.received else "observation_pending",
                            )
                            if demand.latest is not None:
                                self.last_results[intent.id] = {
                                    "at": demand.latest.created_at,
                                    "received": demand.received,
                                }
                        except (ValueError, PermissionError):
                            self.failed_demands[intent.id] = signature
                            status.update(state="blocked", reason="demand_unavailable")
            status.update(requested=intent.requested, last_result=self.last_results.get(intent.id))
            self.statuses[intent.id] = status
        for intent_id, identifier in list(self.leases.items()):
            if intent_id not in active:
                self.demands.release(identifier, "automatic_paused")
                del self.leases[intent_id]
        retained = {intent.id for intent in self.state.intents}
        self.statuses = {key: value for key, value in self.statuses.items() if key in retained}
        self.last_results = {
            key: value for key, value in self.last_results.items() if key in retained
        }
        self.failed_demands = {
            key: value for key, value in self.failed_demands.items() if key in retained
        }

    def snapshot(self, activation_errors=None, runtime_error=None):
        activities = {key: dict(value) for key, value in self.statuses.items()}
        for intent_id, identifier in self.leases.items():
            reason = (activation_errors or {}).get("demand_" + identifier) or runtime_error
            if reason and intent_id in activities:
                activities[intent_id].update(state="unavailable", reason="activation_failed")
        return {
            "configuration": self.state.model_dump(mode="json"),
            "activities": activities,
            "pending_privacy_restriction": self._privacy_barrier is not None,
            "captured_at": time.time(),
        }
