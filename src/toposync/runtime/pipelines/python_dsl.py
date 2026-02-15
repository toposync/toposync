from __future__ import annotations

import builtins
import keyword
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, MutableMapping

from .operator_registry import OperatorRegistry
from .runtime import DropPolicy


NODE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class PythonDslCompileError(ValueError):
    pass


def _slugify_operator_id(operator_id: str) -> str:
    raw = str(operator_id or "").strip()
    if not raw:
        return "step"
    tail = raw.split(".")[-1] or raw
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", tail).strip("_").lower()
    if not slug:
        return "step"
    if slug[0].isdigit():
        slug = f"_{slug}"
    return slug[:64]


def _next_unique_node_id(base: str, used: set[str]) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(base or "").strip()).strip("_")
    if not normalized:
        normalized = "step"
    if normalized[0].isdigit():
        normalized = f"_{normalized}"
    normalized = normalized[:64]

    if normalized not in used:
        used.add(normalized)
        return normalized

    index = 2
    while True:
        suffix = f"_{index}"
        root = normalized
        if len(root) + len(suffix) > 64:
            root = root[: 64 - len(suffix)]
        candidate = f"{root}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def _validate_node_id(value: str) -> str:
    node_id = str(value or "").strip()
    if not node_id:
        raise PythonDslCompileError("Node id is required")
    if not NODE_ID_RE.match(node_id):
        raise PythonDslCompileError(f"Invalid node id: {node_id!r}")
    if keyword.iskeyword(node_id):
        raise PythonDslCompileError(f"Node id cannot be a Python keyword: {node_id!r}")
    return node_id


def _drop_policy_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, DropPolicy):
        return value.value
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    allowed = {item.value for item in DropPolicy}
    if raw not in allowed:
        raise PythonDslCompileError(f"Invalid drop_policy: {raw!r} (allowed: {', '.join(sorted(allowed))})")
    return raw


@dataclass(frozen=True, slots=True)
class GraphEndpoint:
    node: str
    port: str = "out"


class DslGraphBuilder:
    def __init__(self, registry: OperatorRegistry, *, schema_version: int = 1) -> None:
        self._registry = registry
        self._schema_version = int(schema_version)
        self._used_node_ids: set[str] = set()
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: list[dict[str, Any]] = []
        self._target_ports_seen: set[tuple[str, str]] = set()

    @property
    def registry(self) -> OperatorRegistry:
        return self._registry

    def _get_caps(self, operator_id: str) -> set[str]:
        operator = self._registry.get(operator_id)
        if operator is None:
            return set()
        return {str(item).strip().lower() for item in operator.definition.capabilities if str(item).strip()}

    def allocate_node_id(self, operator_id: str, requested: str | None = None) -> str:
        if requested:
            node_id = _validate_node_id(requested)
            if node_id in self._used_node_ids:
                raise PythonDslCompileError(f"Duplicate node id: {node_id!r}")
            self._used_node_ids.add(node_id)
            return node_id
        return _next_unique_node_id(_slugify_operator_id(operator_id), self._used_node_ids)

    def ensure_node(self, *, operator_id: str, config: Mapping[str, Any], node_id: str | None) -> str:
        if self._registry.get(operator_id) is None:
            raise PythonDslCompileError(f"Unknown operator id: {operator_id!r}")
        if node_id:
            resolved_id = _validate_node_id(node_id)
            existing = self._nodes.get(resolved_id)
            if existing is not None:
                existing_op = str(existing.get("operator") or "")
                if existing_op != operator_id:
                    raise PythonDslCompileError(
                        f"Node id {resolved_id!r} already exists with operator {existing_op!r}",
                    )
                return resolved_id
            if resolved_id not in self._used_node_ids:
                self._used_node_ids.add(resolved_id)
        else:
            resolved_id = self.allocate_node_id(operator_id, requested=None)
        normalized_config = self._registry.normalize_config(operator_id, dict(config))
        self._nodes[resolved_id] = {
            "id": resolved_id,
            "operator": operator_id,
            "config": normalized_config,
        }
        return resolved_id

    def default_input_port(self, operator_id: str) -> str:
        operator = self._registry.get(operator_id)
        if operator is None:
            raise PythonDslCompileError(f"Unknown operator id: {operator_id!r}")
        ports = [port.name for port in operator.definition.inputs if str(port.name).strip()]
        if "in" in ports:
            return "in"
        if len(ports) == 1:
            return ports[0]
        if not ports:
            raise PythonDslCompileError(f"Operator {operator_id!r} has no input ports")
        raise PythonDslCompileError(
            f"Operator {operator_id!r} has multiple input ports; set _input_port explicitly",
        )

    def default_edge_policy(self, source_operator_id: str, target_operator_id: str) -> tuple[int, str]:
        source_caps = self._get_caps(source_operator_id)
        target_caps = self._get_caps(target_operator_id)

        if "heavy_compute" in target_caps:
            return 1, DropPolicy.LATEST_ONLY.value
        if "source" in source_caps:
            return 1, DropPolicy.LATEST_ONLY.value
        if "sink" in target_caps or "origin_only" in target_caps:
            return 128, DropPolicy.DROP_OLDEST.value
        if "split_stream" in source_caps:
            return 64, DropPolicy.DROP_OLDEST.value
        return 32, DropPolicy.DROP_OLDEST.value

    def connect(
        self,
        *,
        source: GraphEndpoint,
        target: GraphEndpoint,
        maxsize: int | None,
        drop_policy: str | None,
    ) -> None:
        target_key = (target.node, target.port)
        if target_key in self._target_ports_seen:
            raise PythonDslCompileError(
                f"Node {target.node!r} already has an incoming edge for port {target.port!r}",
            )
        self._target_ports_seen.add(target_key)

        edge: dict[str, Any] = {
            "from": {"node": source.node, "port": source.port},
            "to": {"node": target.node, "port": target.port},
        }
        if maxsize is not None:
            edge["maxsize"] = int(maxsize)
        if drop_policy is not None:
            edge["drop_policy"] = str(drop_policy)
        self._edges.append(edge)

    def to_graph_spec(self) -> dict[str, Any]:
        nodes = sorted(self._nodes.values(), key=lambda item: str(item.get("id", "")))

        def edge_sort_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
            src = edge.get("from") or {}
            dst = edge.get("to") or {}
            return (
                str((src.get("node") or "")),
                str((src.get("port") or "")),
                str((dst.get("node") or "")),
                str((dst.get("port") or "")),
            )

        edges = sorted(self._edges, key=edge_sort_key)
        return {
            "schema_version": int(self._schema_version),
            "nodes": nodes,
            "edges": edges,
        }


class StreamExpr:
    def __init__(self, builder: DslGraphBuilder, endpoint: GraphEndpoint) -> None:
        self._builder = builder
        self._endpoint = endpoint

    @property
    def endpoint(self) -> GraphEndpoint:
        return self._endpoint

    def port(self, name: str) -> "StreamExpr":
        port = str(name or "").strip()
        if not port:
            raise PythonDslCompileError("Port name is required")
        return StreamExpr(self._builder, GraphEndpoint(node=self._endpoint.node, port=port))

    def __or__(self, other: Any) -> "StreamExpr":
        if isinstance(other, OperatorExpr):
            return other._pipe_from(self)  # noqa: SLF001
        if isinstance(other, NodeExpr):
            return other._pipe_from(self)  # noqa: SLF001
        raise PythonDslCompileError(f"Unsupported pipe target: {type(other).__name__}")


class OperatorExpr:
    def __init__(
        self,
        builder: DslGraphBuilder,
        *,
        operator_id: str,
        config: Mapping[str, Any],
        node_id: str | None = None,
        maxsize: int | None = None,
        drop_policy: str | None = None,
        input_port: str | None = None,
    ) -> None:
        self._builder = builder
        self.operator_id = str(operator_id or "").strip()
        self.config = dict(config)
        self.node_id = node_id
        self.maxsize = maxsize
        self.drop_policy = drop_policy
        self.input_port = input_port

    def with_id(self, node_id: str) -> "OperatorExpr":
        return OperatorExpr(
            self._builder,
            operator_id=self.operator_id,
            config=self.config,
            node_id=str(node_id or "").strip(),
            maxsize=self.maxsize,
            drop_policy=self.drop_policy,
            input_port=self.input_port,
        )

    def with_channel(self, *, maxsize: int | None = None, drop_policy: Any | None = None) -> "OperatorExpr":
        return OperatorExpr(
            self._builder,
            operator_id=self.operator_id,
            config=self.config,
            node_id=self.node_id,
            maxsize=self.maxsize if maxsize is None else int(maxsize),
            drop_policy=self.drop_policy if drop_policy is None else _drop_policy_value(drop_policy),
            input_port=self.input_port,
        )

    def with_input_port(self, port: str) -> "OperatorExpr":
        return OperatorExpr(
            self._builder,
            operator_id=self.operator_id,
            config=self.config,
            node_id=self.node_id,
            maxsize=self.maxsize,
            drop_policy=self.drop_policy,
            input_port=str(port or "").strip() or None,
        )

    def _pipe_from(self, upstream: StreamExpr) -> StreamExpr:
        source_node_id = upstream.endpoint.node
        source_node = self._builder._nodes.get(source_node_id)  # noqa: SLF001
        if not source_node:
            raise PythonDslCompileError(f"Unknown upstream node: {source_node_id!r}")

        target_node_id = self._builder.ensure_node(
            operator_id=self.operator_id,
            config=self.config,
            node_id=self.node_id,
        )

        input_port = self.input_port or self._builder.default_input_port(self.operator_id)

        default_maxsize, default_drop_policy = self._builder.default_edge_policy(
            str(source_node.get("operator") or ""),
            self.operator_id,
        )
        maxsize = default_maxsize if self.maxsize is None else int(self.maxsize)
        drop_policy = default_drop_policy if self.drop_policy is None else str(self.drop_policy)

        self._builder.connect(
            source=upstream.endpoint,
            target=GraphEndpoint(node=target_node_id, port=input_port),
            maxsize=maxsize,
            drop_policy=drop_policy,
        )
        return StreamExpr(self._builder, GraphEndpoint(node=target_node_id, port="out"))


class NodeExpr:
    def __init__(
        self,
        builder: DslGraphBuilder,
        *,
        operator_id: str,
        config: Mapping[str, Any],
        node_id: str | None = None,
        maxsize: int | None = None,
        drop_policy: str | None = None,
        input_port: str | None = None,
    ) -> None:
        self._builder = builder
        self.operator_id = str(operator_id or "").strip()
        self.config = dict(config)
        self._requested_node_id = node_id
        self._stream: StreamExpr | None = None
        self.maxsize = maxsize
        self.drop_policy = drop_policy
        self.input_port = input_port

    def as_stream(self) -> StreamExpr:
        if self._stream is not None:
            return self._stream
        if not self._requested_node_id:
            self._requested_node_id = self._builder.allocate_node_id(self.operator_id, requested=None)
        node_id = self._builder.ensure_node(
            operator_id=self.operator_id,
            config=self.config,
            node_id=self._requested_node_id,
        )
        self._stream = StreamExpr(self._builder, GraphEndpoint(node=node_id, port="out"))
        return self._stream

    def with_channel(self, *, maxsize: int | None = None, drop_policy: Any | None = None) -> "NodeExpr":
        if not self._requested_node_id:
            self._requested_node_id = self._builder.allocate_node_id(self.operator_id, requested=None)
        return NodeExpr(
            self._builder,
            operator_id=self.operator_id,
            config=self.config,
            node_id=self._requested_node_id,
            maxsize=self.maxsize if maxsize is None else int(maxsize),
            drop_policy=self.drop_policy if drop_policy is None else _drop_policy_value(drop_policy),
            input_port=self.input_port,
        )

    def with_input_port(self, port: str) -> "NodeExpr":
        if not self._requested_node_id:
            self._requested_node_id = self._builder.allocate_node_id(self.operator_id, requested=None)
        return NodeExpr(
            self._builder,
            operator_id=self.operator_id,
            config=self.config,
            node_id=self._requested_node_id,
            maxsize=self.maxsize,
            drop_policy=self.drop_policy,
            input_port=str(port or "").strip() or None,
        )

    def __or__(self, other: Any) -> StreamExpr:
        return self.as_stream().__or__(other)

    def _pipe_from(self, upstream: StreamExpr) -> StreamExpr:
        node_stream = self.as_stream()
        source_node_id = upstream.endpoint.node
        source_node = self._builder._nodes.get(source_node_id)  # noqa: SLF001
        if not source_node:
            raise PythonDslCompileError(f"Unknown upstream node: {source_node_id!r}")

        input_port = self.input_port or self._builder.default_input_port(self.operator_id)
        default_maxsize, default_drop_policy = self._builder.default_edge_policy(
            str(source_node.get("operator") or ""),
            self.operator_id,
        )
        maxsize = default_maxsize if self.maxsize is None else int(self.maxsize)
        drop_policy = default_drop_policy if self.drop_policy is None else str(self.drop_policy)

        self._builder.connect(
            source=upstream.endpoint,
            target=GraphEndpoint(node=node_stream.endpoint.node, port=input_port),
            maxsize=maxsize,
            drop_policy=drop_policy,
        )
        return node_stream


class OperatorFactory:
    def __init__(self, builder: DslGraphBuilder, registry: OperatorRegistry, operator_id: str) -> None:
        self._builder = builder
        self._registry = registry
        self.operator_id = str(operator_id or "").strip()

    def __call__(self, **kwargs: Any) -> OperatorExpr | NodeExpr:
        config = dict(kwargs)
        node_id = config.pop("_id", None)
        maxsize = config.pop("_maxsize", None)
        drop_policy = _drop_policy_value(config.pop("_drop_policy", None))
        input_port = config.pop("_input_port", None)

        operator = self._registry.get(self.operator_id)
        if operator is None:
            raise PythonDslCompileError(f"Unknown operator id: {self.operator_id!r}")

        required_inputs = [port for port in operator.definition.inputs if port.required]
        normalized_config = self._registry.normalize_config(self.operator_id, config)

        common = dict(
            builder=self._builder,
            operator_id=self.operator_id,
            config=normalized_config,
            node_id=str(node_id or "").strip() or None,
            maxsize=int(maxsize) if maxsize is not None else None,
            drop_policy=drop_policy,
            input_port=str(input_port or "").strip() or None,
        )

        if not required_inputs:
            return NodeExpr(**common)
        return OperatorExpr(**common)


class OperatorNamespace:
    def __init__(self, builder: DslGraphBuilder, registry: OperatorRegistry, prefix: str) -> None:
        self._builder = builder
        self._registry = registry
        self._prefix = str(prefix or "").strip()

    def __getattr__(self, name: str) -> Any:
        token = str(name or "").strip()
        if not token or token.startswith("_"):
            raise AttributeError(name)
        operator_id = f"{self._prefix}.{token}"
        if self._registry.get(operator_id) is not None:
            return OperatorFactory(self._builder, self._registry, operator_id)
        # Allow deeper namespaces (e.g. dist.foo.bar)
        prefix = f"{operator_id}."
        if any(item.id.startswith(prefix) for item in self._registry.list_operators()):
            return OperatorNamespace(self._builder, self._registry, operator_id)
        raise AttributeError(name)


def _make_safe_builtins() -> dict[str, Any]:
    allowed = {
        "True": True,
        "False": False,
        "None": None,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "Exception": Exception,
        "ValueError": ValueError,
        "TypeError": TypeError,
    }
    # Keep a tiny subset of builtins for ergonomics while avoiding IO/imports by default.
    return dict(allowed)


def compile_python_source_to_graph(
    *,
    python_source: str,
    pipeline_name: str,
    registry: OperatorRegistry,
    filename: str = "<pipeline>",
) -> dict[str, Any]:
    builder = DslGraphBuilder(registry)

    globals_dict: dict[str, Any] = {
        "__builtins__": _make_safe_builtins(),
        "DropPolicy": DropPolicy,
        "op": lambda operator_id, **cfg: OperatorFactory(builder, registry, str(operator_id))(**cfg),
        "PIPELINE_NAME": str(pipeline_name),
    }

    # Expose namespaces for all operator prefixes (camera.*, core.*, vision.*, dist.*, ...).
    prefixes: set[str] = set()
    for definition in registry.list_operators():
        first = str(definition.id).split(".")[0]
        if first:
            prefixes.add(first)
    for prefix in sorted(prefixes):
        globals_dict[prefix] = OperatorNamespace(builder, registry, prefix)

    try:
        code = builtins.compile(str(python_source or ""), filename, "exec")
        exec(code, globals_dict, globals_dict)  # noqa: S102
    except PythonDslCompileError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PythonDslCompileError(str(exc)) from exc

    root = globals_dict.get("PIPELINE")
    if root is None:
        root = globals_dict.get(str(pipeline_name))
    if root is None:
        root = globals_dict.get("pipeline")

    if isinstance(root, NodeExpr):
        root.as_stream()
    elif isinstance(root, StreamExpr):
        pass
    else:
        raise PythonDslCompileError(
            "Python source must define PIPELINE (a stream expression built with the DSL).",
        )

    return builder.to_graph_spec()
