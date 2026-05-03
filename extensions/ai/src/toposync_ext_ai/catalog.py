from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .constants import DEFAULT_OLLAMA_MODEL


BUILTIN_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "ollama_qwen3_vl_30b",
        "provider": "ollama",
        "model": DEFAULT_OLLAMA_MODEL,
        "name": "Qwen3-VL 30B",
        "recommendation": "best_local_quality",
        "tasks": ["image_region", "image_condition"],
        "capabilities": ["vision", "structured_json", "bbox", "boolean_filter"],
        "input_modalities": ["text", "image"],
        "local": True,
        "estimated_size": "20GB",
        "min_ollama_version": "0.12.7",
        "last_verified_at": "2026-05-02",
        "notes": "Initial high-quality local recommendation for image reasoning through Ollama.",
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
        "notes": "Lighter local fallback when the 30B variant is too heavy for the machine.",
    },
]


def list_builtin_model_catalog() -> list[dict[str, Any]]:
    return deepcopy(BUILTIN_MODEL_CATALOG)


def normalize_model_ref(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
