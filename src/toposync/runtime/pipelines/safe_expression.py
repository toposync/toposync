from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any


class SafeExpressionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        lineno: int | None = None,
        col_offset: int | None = None,
        end_lineno: int | None = None,
        end_col_offset: int | None = None,
    ) -> None:
        super().__init__(message)
        self.lineno = int(lineno) if lineno is not None else None
        self.col_offset = int(col_offset) if col_offset is not None else None
        self.end_lineno = int(end_lineno) if end_lineno is not None else None
        self.end_col_offset = int(end_col_offset) if end_col_offset is not None else None


_MAX_SOURCE_LEN = 2048


@dataclass(frozen=True, slots=True)
class SafeExpression:
    source: str
    code: Any | None

    @classmethod
    def compile(cls, source: str) -> "SafeExpression":
        text = str(source or "").strip()
        if not text:
            return cls(source="", code=None)
        if len(text) > _MAX_SOURCE_LEN:
            raise SafeExpressionError(f"Expression too long (max {_MAX_SOURCE_LEN} chars)")

        try:
            parsed = ast.parse(text, mode="eval")
        except SyntaxError as exc:  # pragma: no cover - depends on CPython messages
            offset = getattr(exc, "offset", None)
            end_offset = getattr(exc, "end_offset", None)
            raise SafeExpressionError(
                f"Invalid expression syntax: {exc}",
                lineno=getattr(exc, "lineno", None),
                col_offset=(max(0, int(offset) - 1) if offset is not None else None),
                end_lineno=getattr(exc, "end_lineno", None),
                end_col_offset=(max(0, int(end_offset) - 1) if end_offset is not None else None),
            ) from exc

        _SafeAstValidator(text).visit(parsed)
        compiled = compile(parsed, filename="<core.filter>", mode="eval")
        return cls(source=text, code=compiled)

    def evaluate(self, *, payload: Any, metadata: Any, stream_id: str, lifecycle: str, artifacts: set[str]) -> bool:
        if self.code is None:
            return True

        ctx = {
            "payload": DataView(payload),
            "metadata": DataView(metadata),
            "stream_id": str(stream_id or ""),
            "lifecycle": str(lifecycle or ""),
            "artifacts": set(artifacts or set()),
        }
        try:
            value = eval(self.code, {"__builtins__": {}}, ctx)  # noqa: S307 - validated AST + no builtins
        except Exception as exc:  # noqa: BLE001
            raise SafeExpressionError(f"Failed to evaluate expression: {exc}") from exc
        return bool(value)


class OpaqueValue:
    __slots__ = ("type_name",)

    def __init__(self, value: Any) -> None:
        self.type_name = type(value).__name__

    def __repr__(self) -> str:
        return f"<opaque {self.type_name}>"

    def __bool__(self) -> bool:
        return True


class DataView:
    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def _wrap(self, value: Any) -> Any:
        if isinstance(value, dict):
            return DataView(value)
        if isinstance(value, (list, tuple)):
            return DataView(list(value))
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        return OpaqueValue(value)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if not isinstance(self._value, dict):
            raise AttributeError(name)
        return self._wrap(self._value.get(name))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(self._value, dict):
            if isinstance(key, str):
                if key.startswith("_"):
                    return None
                return self._wrap(self._value.get(key))
            return None
        if isinstance(self._value, list):
            if isinstance(key, int):
                if 0 <= key < len(self._value):
                    return self._wrap(self._value[key])
                return None
            return None
        return None

    def __contains__(self, item: Any) -> bool:
        if isinstance(self._value, dict):
            try:
                return item in self._value
            except Exception:
                return False
        if isinstance(self._value, list):
            try:
                return item in self._value
            except Exception:
                return False
        return False

    def __bool__(self) -> bool:
        if self._value is None:
            return False
        if isinstance(self._value, bool):
            return self._value
        if isinstance(self._value, (int, float, str)):
            return bool(self._value)
        if isinstance(self._value, (dict, list)):
            return bool(self._value)
        return True

    def __len__(self) -> int:
        if isinstance(self._value, (dict, list, str)):
            return len(self._value)
        return 0

    def __repr__(self) -> str:
        t = type(self._value).__name__
        if isinstance(self._value, dict):
            return f"<DataView dict keys={len(self._value)}>"
        if isinstance(self._value, list):
            return f"<DataView list len={len(self._value)}>"
        return f"<DataView {t}>"


def _is_rooted_in_payload_or_metadata(node: ast.AST) -> bool:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, ast.Name):
            return cur.id in {"payload", "metadata"}
        if isinstance(cur, ast.Attribute):
            cur = cur.value
            continue
        if isinstance(cur, ast.Subscript):
            cur = cur.value
            continue
        return False
    return False


class _SafeAstValidator(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self._source = source

    def _raise(self, message: str, node: ast.AST | None = None) -> None:
        if node is None:
            raise SafeExpressionError(message)
        raise SafeExpressionError(
            message,
            lineno=getattr(node, "lineno", None),
            col_offset=getattr(node, "col_offset", None),
            end_lineno=getattr(node, "end_lineno", None),
            end_col_offset=getattr(node, "end_col_offset", None),
        )

    def generic_visit(self, node: ast.AST) -> None:  # noqa: D401 - clear for safety
        self._raise(f"Unsupported syntax: {type(node).__name__}", node)

    def visit_Expression(self, node: ast.Expression) -> None:
        self.visit(node.body)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id not in {"payload", "metadata", "stream_id", "lifecycle", "artifacts"}:
            self._raise(f"Unknown name: {node.id!r}", node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if node.value is None:
            return
        if isinstance(node.value, (bool, int, float, str)):
            return
        self._raise("Only None/bool/int/float/str constants are allowed", node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if not isinstance(node.op, (ast.And, ast.Or)):
            self._raise("Only 'and'/'or' boolean ops are allowed", node)
        for value in node.values:
            self.visit(value)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if not isinstance(node.op, ast.Not):
            self._raise("Only 'not' unary op is allowed", node)
        self.visit(node.operand)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)):
            self._raise("Unsupported binary operator", node)
        self.visit(node.left)
        self.visit(node.right)

    def visit_Compare(self, node: ast.Compare) -> None:
        self.visit(node.left)
        for op in node.ops:
            if not isinstance(
                op,
                (
                    ast.Eq,
                    ast.NotEq,
                    ast.Lt,
                    ast.LtE,
                    ast.Gt,
                    ast.GtE,
                    ast.In,
                    ast.NotIn,
                    ast.Is,
                    ast.IsNot,
                ),
            ):
                self._raise("Unsupported comparison operator", node)
        for comparator in node.comparators:
            self.visit(comparator)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._raise("Conditional expressions are not allowed", node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            self._raise("Attribute access starting with '_' is not allowed", node)
        if not _is_rooted_in_payload_or_metadata(node):
            self._raise("Attribute access is only allowed on payload/metadata", node)
        self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if not _is_rooted_in_payload_or_metadata(node):
            self._raise("Subscript access is only allowed on payload/metadata", node)

        self.visit(node.value)
        index_node = node.slice
        if isinstance(index_node, ast.Slice):
            self._raise("Slices are not allowed", node)
        if isinstance(index_node, ast.Constant) and isinstance(index_node.value, (str, int)):
            if isinstance(index_node.value, str) and str(index_node.value).startswith("_"):
                self._raise("Keys starting with '_' are not allowed", index_node)
            return
        self._raise("Only constant string/int subscripts are allowed", index_node)

    def visit_List(self, node: ast.List) -> None:
        for item in node.elts:
            self.visit(item)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        for item in node.elts:
            self.visit(item)

    def visit_Set(self, node: ast.Set) -> None:
        for item in node.elts:
            self.visit(item)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if key is not None:
                self.visit(key)
        for value in node.values:
            self.visit(value)
