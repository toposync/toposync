"""Resolve product intentions into the existing pipeline and demand contracts."""

from __future__ import annotations

import time
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from .automatic import AutomaticState
from .config_store import Pipeline, PipelineAlreadyExistsError
from .pipelines.demands import DemandRequest
from .pipelines.runtime import Lifecycle
from .pipelines.occurrence_sink import RecordActivityParameters
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
        self.provider_failures = {}
        self.stale_preparations = set()
        self._preparation_fingerprint = None
        self.demands.pipeline_allowed = self.pipeline_allowed

    async def catalog(self, config=None):
        config = config or self.config
        entries = [
            {
                "id": "core.record",
                "kind": "consequence",
                "extension_id": "core",
                "title": {"en": "Record activity locally", "pt-BR": "Registrar atividade local"},
                "operators": ["core.record_occurrence"],
                "required_consents": [],
                "parameters_schema": RecordActivityParameters.model_json_schema(),
                "fields": [
                    {"name": "retention_days", "type": "number", "default": 1,
                     "minimum": 1 / 24, "maximum": 366,
                     "title": {"en": "Event and text retention (days)", "pt-BR": "Retenção de eventos e texto (dias)"}},
                    {"name": "notice", "type": "select", "default": "none",
                     "title": {"en": "Local notice", "pt-BR": "Aviso local"},
                     "choices": [
                         {"value": "none", "title": {"en": "Record only", "pt-BR": "Somente registrar"}},
                         {"value": "simulated", "title": {"en": "Simulated receipt", "pt-BR": "Recibo simulado"}},
                     ]},
                ],
            }
        ]
        for identifier in self.services.list_ids("automatic.catalog."):
            entries.extend(await self._provider(identifier, config))
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
                found[entry["id"]].update(available=False, reason="duplicate_capability")
                continue
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

    async def _provider(self, identifier, config, **kwargs):
        fingerprint = (
            hashlib.sha256(config.model_dump_json().encode()).hexdigest() if config else ""
        )
        if self.provider_failures.get(identifier) == fingerprint:
            return []
        try:
            entries = await self.services.call(identifier, **kwargs)
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict) or not entry.get("id") for entry in entries
            ):
                raise ValueError("Invalid automatic provider entries")
            if identifier.startswith("automatic.catalog.") and any(
                entry.get("kind") not in {"observation", "consequence"}
                or not isinstance(entry.get("extension_id"), str)
                or not isinstance(entry.get("operators", []), list)
                for entry in entries
            ):
                raise ValueError("Invalid automatic capability metadata")
            if identifier.startswith("automatic.diagnostics.") and any(
                not isinstance(entry.get("diagnostics"), dict) for entry in entries
            ):
                raise ValueError("Invalid automatic diagnostics")
        except Exception:
            # A broken optional provider cannot stop unrelated manual pipelines or retry every poll.
            self.provider_failures[identifier] = fingerprint
            return []
        self.provider_failures.pop(identifier, None)
        return entries

    async def targets(self, config):
        targets = []
        for identifier in self.services.list_ids("automatic.targets."):
            targets.extend(await self._provider(identifier, config, configuration=config))
        return targets

    def extension_dependents(self, config):
        dependencies = {}
        for pipeline in config.pipelines:
            info = pipeline.graph.get("meta", {}).get("automatic", {})
            extensions = set(info.get("extension_ids", []))
            for node in pipeline.graph.get("nodes", []):
                operator = self.operators.get(node["operator"])
                if operator is not None and operator.owner:
                    extensions.add(operator.owner)
            dependencies[pipeline.name] = extensions
        outputs = {output["key"]: pipeline.name for pipeline in config.pipelines
                   for output in pipeline.graph.get("meta", {}).get("outputs", [])}
        dependents = {}
        for pipeline in config.pipelines:
            extensions = set(dependencies[pipeline.name])
            binding = pipeline.graph.get("meta", {}).get("result_consumer", {})
            producer = outputs.get(binding.get("request", {}).get("result_key"))
            extensions.update(dependencies.get(producer, set()))
            for extension in extensions - {"core"}:
                dependents.setdefault(extension, []).append({
                    "id": pipeline.name, "name": pipeline.display_name or pipeline.name,
                })
        return dependents

    async def prepare_config(self, config):
        self.config = config
        self.state = AutomaticState.model_validate(config.settings.core.get("automatic", {}))
        await self.catalog(config)
        validators = {key: value.get("preparation_validator") for key, value in self.capabilities.items()}
        fingerprint = (hashlib.sha256(config.model_dump_json().encode()).hexdigest(), tuple(validators.items()))
        if fingerprint == self._preparation_fingerprint:
            return
        stale = set()
        intents = {item.id: item for item in self.state.intents}
        for pipeline in config.pipelines:
            info = pipeline.graph.get("meta", {}).get("automatic", {})
            intent = intents.get(info.get("intent_id"))
            validator = validators.get(intent.capability) if intent else None
            if not validator:
                continue
            try:
                valid = await self.services.call(validator, intent=intent, configuration=config,
                                                 preparation=info.get("preparation"))
            except Exception:
                valid = False
            if valid is not True:
                stale.add(intent.id)
        self.stale_preparations = stale
        self._preparation_fingerprint = fingerprint

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
            if current.graph.get("meta", {}).get("automatic", {}).get("intent_id") in self.stale_preparations:
                return False
            info = current.graph.get("meta", {}).get("automatic", {})
            if info.get("status", {}).get("reason") == "extension_unavailable":
                return False
            disabled = self.config.settings.core.get("extension_management", {}).get("disabled_extension_ids", [])
            if set(info.get("extension_ids", [])).intersection(disabled):
                return False
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
            if capability.get("kind") != "observation" or consequence.get("kind") != "consequence":
                raise ValueError("Select an observation and a consequence")
            if capability.get("scopes") and original.scope.kind not in capability["scopes"]:
                raise ValueError("This observation requires another scope")
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
                output = next((item for item in graph.get("meta", {}).get("outputs", [])
                               if item["key"] == "automatic." + original.id), None)
                if output is None or "activity_observation" not in output["contract"].get("payload_keys", []):
                    raise ValueError("Recording requires the observation evidence contract")
                effect = {
                    "operator": "core.record_occurrence",
                    "config": {
                        **RecordActivityParameters.model_validate(original.consequence.parameters).model_dump(),
                        "objective_id": original.id,
                        "title": original.name,
                        "kind": original.capability,
                        "scope": {"camera_ids": cameras,
                                  "area_ids": [original.scope.area_id] if original.scope.area_id else [],
                                  "association_scope": (effective.condition.get("association") or {}).get("scope")},
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
                "preparation": plan.get("preparation"),
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
            info["extension_ids"] = sorted({
                self.capabilities[intent.capability]["extension_id"],
                self.capabilities[intent.consequence.capability]["extension_id"],
                *(operator.owner for operator in operators if operator.owner),
            })
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

    async def apply(self, state, *, expected_revision, owner, reviewed_preparations=None):
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
        reviewed_preparations = reviewed_preparations or {}
        previous_plans = {p.graph["meta"]["automatic"]["intent_id"]: p for p in config.pipelines
                          if p.graph.get("meta", {}).get("automatic")}
        retained_plans = []
        for pipeline in plans:
            info = pipeline.graph["meta"]["automatic"]
            identifier, preparation = info["intent_id"], info.get("preparation")
            reviewed = reviewed_preparations.get(identifier)
            if reviewed is not None and reviewed != preparation:
                raise PipelineAlreadyExistsError("Source preparation changed; review the activity again")
            old = previous_plans.get(identifier)
            old_preparation = old.graph["meta"]["automatic"].get("preparation") if old else None
            if preparation and identifier in previous_by_id and old_preparation != preparation and reviewed != preparation:
                # Pause/privacy buttons must not silently approve another activity's changed sources.
                statuses[identifier].update(state="unavailable", reason="source_preparation_changed")
                if old is None:
                    continue
                pipeline = old.model_copy(deep=True)
                pipeline.graph["meta"]["automatic"].update(revision=info["revision"], status=statuses[identifier])
            retained_plans.append(pipeline)
        for intent in state.intents:
            if statuses[intent.id].get("reason") == "extension_unavailable" and intent.id in previous_plans:
                # Keep dependency evidence and the old contract, but never execute this stale plan.
                old = previous_plans[intent.id].model_copy(deep=True)
                old.display_name = intent.name
                old.graph["meta"]["automatic"].update(revision=intent.revision, status=statuses[intent.id])
                retained_plans.append(old)
        plans = retained_plans
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
        await self.prepare_config(config)
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
            elif intent.id in self.stale_preparations:
                status.update(state="unavailable", reason="source_preparation_changed")
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
                            observed = (
                                demand.latest is not None
                                and demand.latest.lifecycle != Lifecycle.CLOSE
                            )
                            status.update(
                                state="active" if observed else "waiting",
                                reason=None if observed else "observation_pending",
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

    async def add_diagnostics(self, snapshot):
        if self._privacy_barrier is not None:
            return
        groups = {}
        for intent in self.state.intents:
            activity = snapshot["activities"].get(intent.id, {})
            capability = self.capabilities.get(intent.capability, {})
            provider = capability.get("diagnostics_provider")
            if provider and capability.get("available") and activity.get("state") in {"active", "waiting"}:
                effective = intent.model_copy(update={"condition": {
                    **self.state.defaults.get(intent.capability, {}), **intent.condition, **intent.customizations,
                }})
                groups.setdefault(provider, []).append(effective)
        for provider, intents in groups.items():
            entries = await self._provider(provider, self.config, intents=intents, configuration=self.config)
            allowed = {intent.id for intent in intents}
            reported = set()
            for entry in entries:
                if entry["id"] in allowed and isinstance(entry.get("diagnostics"), dict):
                    snapshot["activities"][entry["id"]]["diagnostics"] = entry["diagnostics"]
                    reported.add(entry["id"])
            for identifier in allowed - reported:
                snapshot["activities"][identifier]["diagnostics_unavailable"] = True

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
            "provider_errors": sorted(self.provider_failures),
        }
