from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator


OPERATOR_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
CONTRACT_ITEM_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class OperatorRegistrationError(ValueError):
    pass


class OperatorConfigValidationError(ValueError):
    pass


class OperatorPort(BaseModel):
    name: str
    required: bool = False
    description: str = ""

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not PORT_NAME_RE.match(name):
            raise ValueError("Port name must match ^[a-z][a-z0-9_]{0,63}$")
        return name


class OperatorDefinition(BaseModel):
    id: str
    description: str = ""
    inputs: list[OperatorPort] = Field(default_factory=list)
    outputs: list[OperatorPort] = Field(default_factory=lambda: [OperatorPort(name="out")])
    capabilities: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    share_strategy: Literal["by_signature", "never"] = "by_signature"
    requires_payload_keys: list[str] = Field(default_factory=list)
    requires_artifacts: list[str] = Field(default_factory=list)
    produces_payload_keys: list[str] = Field(default_factory=list)
    produces_artifacts: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_operator_id(cls, value: str) -> str:
        operator_id = str(value or "").strip()
        if not OPERATOR_ID_RE.match(operator_id):
            raise ValueError("Operator id must match ^[a-z][a-z0-9_.-]{1,127}$")
        return operator_id

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            name = str(item or "").strip().lower()
            if not name:
                continue
            if not CAPABILITY_NAME_RE.match(name):
                raise ValueError(f"Invalid capability name: {name}")
            if name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out

    @field_validator("inputs", "outputs")
    @classmethod
    def _validate_unique_ports(cls, ports: list[OperatorPort]) -> list[OperatorPort]:
        names: set[str] = set()
        for port in ports:
            if port.name in names:
                raise ValueError(f"Duplicate port name: {port.name}")
            names.add(port.name)
        return ports

    @field_validator("requires_payload_keys", "requires_artifacts", "produces_payload_keys", "produces_artifacts")
    @classmethod
    def _validate_contract_items(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            name = str(item or "").strip()
            if not name:
                continue
            if not CONTRACT_ITEM_RE.match(name):
                raise ValueError(f"Invalid contract item: {name}")
            if name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out


class _LooseConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True, slots=True)
class RegisteredOperator:
    definition: OperatorDefinition
    config_model: type[BaseModel]
    config_signature: str
    owner: str | None
    runtime_factory: Callable[[dict[str, Any], Any], Any] | None = None


class OperatorRegistry:
    def __init__(self) -> None:
        self._items: dict[str, RegisteredOperator] = {}

    def register_operator(
        self,
        *,
        operator_id: str,
        config_model: type[BaseModel] | None = None,
        inputs: list[OperatorPort | dict[str, Any]] | None = None,
        outputs: list[OperatorPort | dict[str, Any]] | None = None,
        capabilities: list[str] | None = None,
        defaults: dict[str, Any] | None = None,
        description: str = "",
        share_strategy: Literal["by_signature", "never"] = "by_signature",
        requires_payload_keys: list[str] | None = None,
        requires_artifacts: list[str] | None = None,
        produces_payload_keys: list[str] | None = None,
        produces_artifacts: list[str] | None = None,
        owner: str | None = None,
        runtime_factory: Callable[[dict[str, Any], Any], Any] | None = None,
    ) -> OperatorDefinition:
        normalized_operator_id = str(operator_id or "").strip()
        if normalized_operator_id in self._items:
            raise OperatorRegistrationError(f"Operator id already registered: {normalized_operator_id}")

        cfg_model = _ensure_config_model(config_model)
        parsed_inputs = [i if isinstance(i, OperatorPort) else OperatorPort.model_validate(i) for i in (inputs or [])]
        parsed_outputs = [o if isinstance(o, OperatorPort) else OperatorPort.model_validate(o) for o in (outputs or [])]
        if not parsed_outputs:
            parsed_outputs = [OperatorPort(name="out")]

        default_values = dict(defaults or {})
        normalized_defaults = _validate_with_model(cfg_model, default_values)
        config_schema = cfg_model.model_json_schema()
        config_signature = _hash_payload(config_schema)

        definition = OperatorDefinition(
            id=normalized_operator_id,
            description=str(description or "").strip(),
            inputs=parsed_inputs,
            outputs=parsed_outputs,
            capabilities=list(capabilities or []),
            defaults=normalized_defaults,
            config_schema=config_schema,
            share_strategy=share_strategy,
            requires_payload_keys=list(requires_payload_keys or []),
            requires_artifacts=list(requires_artifacts or []),
            produces_payload_keys=list(produces_payload_keys or []),
            produces_artifacts=list(produces_artifacts or []),
        )

        self._items[definition.id] = RegisteredOperator(
            definition=definition,
            config_model=cfg_model,
            config_signature=config_signature,
            owner=str(owner or "").strip() or None,
            runtime_factory=runtime_factory,
        )
        return definition

    def get(self, operator_id: str) -> RegisteredOperator | None:
        return self._items.get(operator_id)

    def list_operators(self) -> list[OperatorDefinition]:
        return [item.definition for _, item in sorted(self._items.items(), key=lambda pair: pair[0])]

    def normalize_config(self, operator_id: str, raw_config: dict[str, Any] | None) -> dict[str, Any]:
        registered = self._items.get(operator_id)
        if registered is None:
            raise OperatorConfigValidationError(f"Unknown operator: {operator_id}")
        merged = dict(registered.definition.defaults)
        merged.update(dict(raw_config or {}))
        return _validate_with_model(registered.config_model, merged)


def _ensure_config_model(model_type: type[BaseModel] | None) -> type[BaseModel]:
    if model_type is None:
        return _LooseConfigModel
    if isinstance(model_type, type) and issubclass(model_type, BaseModel):
        return model_type
    raise TypeError("config_model must be a pydantic BaseModel class")


def _validate_with_model(model_type: type[BaseModel], payload: dict[str, Any]) -> dict[str, Any]:
    try:
        model = model_type.model_validate(payload)
    except ValidationError as exc:
        raise OperatorConfigValidationError(str(exc)) from exc
    return model.model_dump(mode="json")


def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_config_model(
    *,
    name: str,
    fields: dict[str, tuple[type[Any], Any]],
    extra: Literal["allow", "ignore", "forbid"] = "forbid",
) -> type[BaseModel]:
    model = create_model(name, **fields)  # type: ignore[arg-type]
    model.model_config = ConfigDict(extra=extra)
    return model
