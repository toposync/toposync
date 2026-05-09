from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator, model_validator


OPERATOR_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
CONTRACT_ITEM_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
EXPRESSION_HINT_PATH_RE = re.compile(r"^(payload|metadata)(?:\.[a-z][a-z0-9_]{0,127}|\[[0-9]+\])*$")
EXECUTION_MODE = Literal["in_event_loop", "thread_pool", "process_pool", "external"]
OPERATOR_DIAGNOSTIC_SEVERITY = Literal["error", "warning", "info"]


class OperatorRegistrationError(ValueError):
    pass


class OperatorConfigValidationError(ValueError):
    pass


class OperatorDiagnostic(BaseModel):
    severity: OPERATOR_DIAGNOSTIC_SEVERITY = "warning"
    code: str
    message: str
    suggestion: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", "message", "suggestion")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return str(value or "").strip()


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


class ExpressionHint(BaseModel):
    kind: Literal["payload_path", "metadata_path", "artifact_name"]
    path: str | None = None
    value: str | None = None
    type: str = ""
    description: str = ""
    examples: list[str] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)

    @field_validator("path", "value")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("type", "description")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("examples", "enum_values")
    @classmethod
    def _normalize_text_list(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            out.append(text)
            seen.add(text)
        return out

    @model_validator(mode="after")
    def _validate_shape(self) -> ExpressionHint:
        if self.kind in {"payload_path", "metadata_path"}:
            if not self.path:
                raise ValueError(f"{self.kind} requires a non-empty path")
            if not EXPRESSION_HINT_PATH_RE.match(self.path):
                raise ValueError(
                    "Expression hint path must start with payload or metadata and use dotted identifiers / numeric indexes"
                )
            expected_root = "payload" if self.kind == "payload_path" else "metadata"
            if not self.path.startswith(expected_root):
                raise ValueError(f"{self.kind} path must start with '{expected_root}'")
            if self.value is not None:
                raise ValueError(f"{self.kind} does not accept a value field")
            return self

        if not self.value:
            raise ValueError("artifact_name requires a non-empty value")
        if self.path is not None:
            raise ValueError("artifact_name does not accept a path field")
        return self


def payload_path_hint(
    path: str,
    *,
    value_type: str = "",
    description: str = "",
    examples: list[str] | None = None,
    enum_values: list[str] | None = None,
) -> ExpressionHint:
    return ExpressionHint(
        kind="payload_path",
        path=path,
        type=value_type,
        description=description,
        examples=list(examples or []),
        enum_values=list(enum_values or []),
    )


def metadata_path_hint(
    path: str,
    *,
    value_type: str = "",
    description: str = "",
    examples: list[str] | None = None,
    enum_values: list[str] | None = None,
) -> ExpressionHint:
    return ExpressionHint(
        kind="metadata_path",
        path=path,
        type=value_type,
        description=description,
        examples=list(examples or []),
        enum_values=list(enum_values or []),
    )


def artifact_name_hint(
    value: str,
    *,
    description: str = "",
    examples: list[str] | None = None,
    enum_values: list[str] | None = None,
) -> ExpressionHint:
    return ExpressionHint(
        kind="artifact_name",
        value=value,
        description=description,
        examples=list(examples or []),
        enum_values=list(enum_values or []),
    )


class OperatorDefinition(BaseModel):
    id: str
    description: str = ""
    inputs: list[OperatorPort] = Field(default_factory=list)
    outputs: list[OperatorPort] = Field(default_factory=lambda: [OperatorPort(name="out")])
    capabilities: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    share_strategy: Literal["by_signature", "never"] = "by_signature"
    execution_mode: EXECUTION_MODE = "in_event_loop"
    max_concurrency: int | None = Field(default=None, ge=1, le=1024)
    requires_payload_keys: list[str] = Field(default_factory=list)
    requires_artifacts: list[str] = Field(default_factory=list)
    requires_source_fields: list[str] = Field(default_factory=list)
    requires_media_fields: list[str] = Field(default_factory=list)
    produces_payload_keys: list[str] = Field(default_factory=list)
    produces_artifacts: list[str] = Field(default_factory=list)
    produces_source_fields: list[str] = Field(default_factory=list)
    produces_media_fields: list[str] = Field(default_factory=list)
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    expression_hints: list[ExpressionHint] = Field(default_factory=list)

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

    @field_validator("expression_hints")
    @classmethod
    def _dedupe_expression_hints(cls, values: list[ExpressionHint]) -> list[ExpressionHint]:
        out: list[ExpressionHint] = []
        seen: set[tuple[str, str, str]] = set()
        for item in values:
            key = (item.kind, str(item.path or ""), str(item.value or ""))
            if key in seen:
                continue
            out.append(item)
            seen.add(key)
        return out

    @field_validator(
        "requires_payload_keys",
        "requires_artifacts",
        "requires_source_fields",
        "requires_media_fields",
        "produces_payload_keys",
        "produces_artifacts",
        "produces_source_fields",
        "produces_media_fields",
        "input_modalities",
        "output_modalities",
    )
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
    diagnostics_factory: Callable[[dict[str, Any], dict[str, Any]], list[OperatorDiagnostic | dict[str, Any]]] | None = None


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
        execution_mode: EXECUTION_MODE = "in_event_loop",
        max_concurrency: int | None = None,
        requires_payload_keys: list[str] | None = None,
        requires_artifacts: list[str] | None = None,
        requires_source_fields: list[str] | None = None,
        requires_media_fields: list[str] | None = None,
        produces_payload_keys: list[str] | None = None,
        produces_artifacts: list[str] | None = None,
        produces_source_fields: list[str] | None = None,
        produces_media_fields: list[str] | None = None,
        input_modalities: list[str] | None = None,
        output_modalities: list[str] | None = None,
        expression_hints: list[ExpressionHint | dict[str, Any]] | None = None,
        owner: str | None = None,
        runtime_factory: Callable[[dict[str, Any], Any], Any] | None = None,
        diagnostics_factory: Callable[[dict[str, Any], dict[str, Any]], list[OperatorDiagnostic | dict[str, Any]]] | None = None,
    ) -> OperatorDefinition:
        normalized_operator_id = str(operator_id or "").strip()
        if normalized_operator_id in self._items:
            raise OperatorRegistrationError(f"Operator id already registered: {normalized_operator_id}")

        cfg_model = _ensure_config_model(config_model)
        parsed_inputs = [i if isinstance(i, OperatorPort) else OperatorPort.model_validate(i) for i in (inputs or [])]
        parsed_outputs = [o if isinstance(o, OperatorPort) else OperatorPort.model_validate(o) for o in outputs] if outputs is not None else []
        parsed_expression_hints = [
            item if isinstance(item, ExpressionHint) else ExpressionHint.model_validate(item)
            for item in (expression_hints or [])
        ]
        if outputs is None and not parsed_outputs:
            parsed_outputs = [OperatorPort(name="out")]

        capability_values = list(capabilities or [])
        if share_strategy == "by_signature":
            capability_values = [item for item in capability_values if str(item or "").strip().lower() != "side_effect"]
            capability_values.append("pure")
        else:
            capability_values = [item for item in capability_values if str(item or "").strip().lower() != "pure"]
            capability_values.append("side_effect")

        default_values = dict(defaults or {})
        normalized_defaults = _validate_with_model(cfg_model, default_values)
        config_schema = cfg_model.model_json_schema()
        config_signature = _hash_payload(config_schema)

        definition = OperatorDefinition(
            id=normalized_operator_id,
            description=str(description or "").strip(),
            inputs=parsed_inputs,
            outputs=parsed_outputs,
            capabilities=capability_values,
            defaults=normalized_defaults,
            config_schema=config_schema,
            share_strategy=share_strategy,
            execution_mode=execution_mode,
            max_concurrency=max_concurrency,
            requires_payload_keys=list(requires_payload_keys or []),
            requires_artifacts=list(requires_artifacts or []),
            requires_source_fields=list(requires_source_fields or []),
            requires_media_fields=list(requires_media_fields or []),
            produces_payload_keys=list(produces_payload_keys or []),
            produces_artifacts=list(produces_artifacts or []),
            produces_source_fields=list(produces_source_fields or []),
            produces_media_fields=list(produces_media_fields or []),
            input_modalities=list(input_modalities or []),
            output_modalities=list(output_modalities or []),
            expression_hints=parsed_expression_hints,
        )

        self._items[definition.id] = RegisteredOperator(
            definition=definition,
            config_model=cfg_model,
            config_signature=config_signature,
            owner=str(owner or "").strip() or None,
            runtime_factory=runtime_factory,
            diagnostics_factory=diagnostics_factory,
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

    def collect_diagnostics(
        self,
        operator_id: str,
        config: dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> list[OperatorDiagnostic]:
        registered = self._items.get(operator_id)
        if registered is None or registered.diagnostics_factory is None:
            return []
        try:
            raw_items = registered.diagnostics_factory(dict(config or {}), context if context is not None else {})
        except Exception as exc:  # noqa: BLE001
            return [
                OperatorDiagnostic(
                    severity="warning",
                    code="operator_diagnostics_failed",
                    message=f"Could not check operator diagnostics: {exc}",
                    details={"error": str(exc)},
                )
            ]

        diagnostics: list[OperatorDiagnostic] = []
        for item in raw_items or []:
            try:
                diagnostic = item if isinstance(item, OperatorDiagnostic) else OperatorDiagnostic.model_validate(item)
            except Exception:
                continue
            if not diagnostic.code or not diagnostic.message:
                continue
            diagnostics.append(diagnostic)
        return diagnostics


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
