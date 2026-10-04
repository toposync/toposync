from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .constants import DEFAULT_OLLAMA_MODEL


BUILTIN_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "ollama_local_default",
        "provider": "ollama",
        "model": DEFAULT_OLLAMA_MODEL,
        "name": DEFAULT_OLLAMA_MODEL,
        "recommendation": "local_default",
        "tasks": ["image_region", "image_condition"],
        "capabilities": ["vision", "structured_json", "bbox", "boolean_filter"],
        "input_modalities": ["text", "image"],
        "local": True,
        "notes": "Local default. Check the served model metadata and measure resource use for the intended workload.",
    },
    {
        "id": "ollama_qwen3_vl_8b",
        "provider": "ollama",
        "model": "qwen3-vl:8b",
        "name": "Qwen3-VL 8B",
        "recommendation": "lighter_local",
        "tasks": ["image_region", "image_condition"],
        "capabilities": ["vision", "structured_json", "bbox", "boolean_filter"],
        "input_modalities": ["text", "image"],
        "local": True,
        "estimated_size": "6.1GB",
        "min_ollama_version": "0.12.7",
        "last_verified_at": "2026-05-02",
        "notes": "Smaller vision candidate. Compare quality and resource use for the intended workload.",
    },
]


def list_builtin_model_catalog() -> list[dict[str, Any]]:
    return deepcopy(BUILTIN_MODEL_CATALOG)


def normalize_model_ref(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
