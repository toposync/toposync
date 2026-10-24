from __future__ import annotations

from copy import deepcopy
from typing import Any


def merge_operator_config(original: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge explicit configuration changes; lists and scalar values replace."""
    result = deepcopy(original)
    for key, value in patch.items():
        result[key] = (merge_operator_config(result[key], value)
                       if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value))
    return result
