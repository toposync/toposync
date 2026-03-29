from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .custom_onnx import (
    _safe_filename,
    _slug_token,
    default_custom_onnx_staging_dir,
    import_custom_onnx_model,
    inspect_custom_onnx_artifact,
)
from .manifests import ModelRegistryError


SUPPORTED_HUGGINGFACE_TASKS = {"classification", "detection"}
PIPELINE_TAG_TO_TASK = {
    "image-classification": "classification",
    "object-detection": "detection",
}


def _import_huggingface_hub():
    try:
        from huggingface_hub import HfApi, hf_hub_download  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Hugging Face origin support requires huggingface_hub. Install the first-party vision runtime dependencies."
        ) from exc
    return HfApi, hf_hub_download


def _fetch_huggingface_model_info(*, repo_id: str, revision: str = "") -> Any:
    HfApi, _hf_hub_download = _import_huggingface_hub()
    api = HfApi()
    return api.model_info(repo_id=repo_id, revision=revision or None, files_metadata=True)


def _download_huggingface_file(*, repo_id: str, filename: str, revision: str = "") -> Path:
    _HfApi, hf_hub_download = _import_huggingface_hub()
    return Path(hf_hub_download(repo_id=repo_id, filename=filename, revision=revision or None))


def _normalize_card_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _safe_json_from_repo(*, repo_id: str, revision: str, filename: str) -> dict[str, Any]:
    try:
        path = _download_huggingface_file(repo_id=repo_id, filename=filename, revision=revision)
    except Exception:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def normalize_huggingface_repo_input(repo: str) -> dict[str, str]:
    raw = str(repo or "").strip()
    if not raw:
        raise ModelRegistryError("huggingface repo is required")

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        host = str(parsed.netloc or "").strip().lower()
        if host not in {"huggingface.co", "www.huggingface.co"}:
            raise ModelRegistryError("Only Hugging Face Hub URLs are supported in this phase")
        parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
        if not parts:
            raise ModelRegistryError("Invalid Hugging Face Hub URL")
        if parts[0] == "models":
            parts = parts[1:]
        if not parts:
            raise ModelRegistryError("Invalid Hugging Face Hub URL")
        if len(parts) >= 2:
            repo_id = f"{parts[0]}/{parts[1]}"
            tail = parts[2:]
        else:
            repo_id = parts[0]
            tail = parts[1:]
        url_revision = ""
        if len(tail) >= 2 and tail[0] in {"tree", "blob", "resolve"}:
            url_revision = tail[1]
        return {
            "repo_id": repo_id.strip(),
            "url_revision": url_revision.strip(),
            "source_url": f"https://huggingface.co/{repo_id.strip()}",
        }

    return {
        "repo_id": raw,
        "url_revision": "",
        "source_url": f"https://huggingface.co/{raw}",
    }


def _normalize_huggingface_labels(config_payload: dict[str, Any]) -> list[str]:
    id2label = config_payload.get("id2label")
    if isinstance(id2label, dict):
        ordered: list[tuple[int, str]] = []
        for key, value in id2label.items():
            try:
                idx = int(str(key))
            except Exception:
                continue
            label = str(value or "").strip()
            if not label:
                continue
            ordered.append((idx, label))
        ordered.sort(key=lambda item: item[0])
        return [label for _idx, label in ordered]
    if isinstance(id2label, list):
        return [str(item or "").strip() for item in id2label if str(item or "").strip()]
    return []


def _normalize_huggingface_preprocess(config_payload: dict[str, Any]) -> dict[str, Any]:
    size = config_payload.get("size")
    width = 640
    height = 640
    if isinstance(size, int):
        width = int(size)
        height = int(size)
    elif isinstance(size, dict):
        width = int(size.get("width") or size.get("shortest_edge") or size.get("longest_edge") or 640)
        height = int(size.get("height") or size.get("shortest_edge") or size.get("longest_edge") or width)
    mean = config_payload.get("image_mean") or config_payload.get("mean") or [0.0, 0.0, 0.0]
    std = config_payload.get("image_std") or config_payload.get("std") or [1.0, 1.0, 1.0]
    rescale_factor = config_payload.get("rescale_factor")
    if rescale_factor is None and bool(config_payload.get("do_rescale")):
        rescale_factor = 1.0 / 255.0
    try:
        normalized_rescale = float(rescale_factor if rescale_factor is not None else 1.0)
    except Exception:
        normalized_rescale = 1.0
    return {
        "width": max(1, width),
        "height": max(1, height),
        "color_order": "rgb",
        "resize_mode": "stretch",
        "rescale_factor": normalized_rescale,
        "normalization_mean": [float(item) for item in list(mean or []) if item is not None],
        "normalization_std": [float(item) for item in list(std or []) if item is not None],
    }


def _extract_huggingface_onnx_candidates(siblings: list[Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for sibling in siblings:
        path = str(getattr(sibling, "rfilename", "") or "").strip()
        if not path or not path.lower().endswith(".onnx"):
            continue
        score = 0
        lower_path = path.lower()
        if lower_path.startswith("onnx/"):
            score += 50
        if lower_path.endswith("/model.onnx") or lower_path == "model.onnx":
            score += 25
        if "quant" not in lower_path and "int8" not in lower_path:
            score += 10
        candidates.append(
            {
                "path": path,
                "label": Path(path).name,
                "size_bytes": int(getattr(sibling, "size", 0) or 0),
                "score": score,
            }
        )
    candidates.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("path", "")).lower()))
    for item in candidates:
        item.pop("score", None)
    return candidates


def probe_huggingface_repo(*, repo: str, revision: str = "") -> dict[str, Any]:
    normalized = normalize_huggingface_repo_input(repo)
    requested_revision = str(revision or normalized["url_revision"] or "").strip()
    repo_id = normalized["repo_id"]
    source_url = normalized["source_url"]

    try:
        info = _fetch_huggingface_model_info(repo_id=repo_id, revision=requested_revision)
    except Exception as exc:  # noqa: BLE001
        raise ModelRegistryError(f"Failed to inspect Hugging Face repo '{repo_id}': {exc}") from exc

    siblings = list(getattr(info, "siblings", []) or [])
    card_data = _normalize_card_data(getattr(info, "cardData", None))
    resolved_revision = str(requested_revision or getattr(info, "sha", "") or "").strip()
    pipeline_tag = str(getattr(info, "pipeline_tag", "") or card_data.get("pipeline_tag") or "").strip().lower()
    detected_task = PIPELINE_TAG_TO_TASK.get(pipeline_tag, "")
    declared_license = str(card_data.get("license") or "").strip()
    labels = _normalize_huggingface_labels(_safe_json_from_repo(repo_id=repo_id, revision=resolved_revision, filename="config.json"))
    preprocess_defaults = _normalize_huggingface_preprocess(
        _safe_json_from_repo(repo_id=repo_id, revision=resolved_revision, filename="preprocessor_config.json")
    )
    onnx_candidates = _extract_huggingface_onnx_candidates(siblings)
    repo_tail = repo_id.split("/")[-1].replace("_", " ").replace("-", " ").strip().title() or repo_id
    display_name = str(getattr(info, "id", "") or "").strip() or repo_tail

    if onnx_candidates and detected_task in SUPPORTED_HUGGINGFACE_TASKS:
        download_supported = True
        download_reason = "onnx_ready"
    elif not onnx_candidates:
        download_supported = False
        download_reason = "onnx_missing"
    elif detected_task:
        download_supported = False
        download_reason = "task_unsupported"
    else:
        download_supported = False
        download_reason = "task_unknown"

    return {
        "repo_id": repo_id,
        "source_url": source_url,
        "requested_revision": requested_revision,
        "resolved_revision": resolved_revision,
        "pipeline_tag": pipeline_tag,
        "detected_task": detected_task,
        "declared_license": declared_license,
        "onnx_candidates": onnx_candidates,
        "download_supported": download_supported,
        "download_reason": download_reason,
        "labels": labels,
        "preprocess_defaults": preprocess_defaults,
        "suggested_display_name": repo_tail if repo_tail else display_name,
    }


def _stage_huggingface_onnx_download(
    *,
    repo_id: str,
    onnx_filename: str,
    revision: str,
    data_dir: str | Path | None = None,
) -> Path:
    downloaded = _download_huggingface_file(repo_id=repo_id, filename=onnx_filename, revision=revision)
    staging_dir = default_custom_onnx_staging_dir(data_dir=data_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(Path(onnx_filename).name, fallback="model.onnx")
    staged_path = staging_dir / f"hf_{uuid.uuid4().hex}_{safe_name}"
    shutil.copy2(downloaded, staged_path)
    return staged_path


def inspect_huggingface_onnx(
    *,
    repo: str,
    revision: str = "",
    onnx_filename: str,
    task: str = "",
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    probe = probe_huggingface_repo(repo=repo, revision=revision)
    repo_id = str(probe["repo_id"])
    resolved_revision = str(probe["resolved_revision"])
    selected_file = str(onnx_filename or "").strip()
    if not selected_file:
        raise ModelRegistryError("onnx_filename is required")
    candidate_paths = {str(item.get("path") or "").strip() for item in list(probe.get("onnx_candidates") or [])}
    if selected_file not in candidate_paths:
        raise ModelRegistryError(f"Selected ONNX file '{selected_file}' was not found in repo '{repo_id}'")

    staged_path = _stage_huggingface_onnx_download(
        repo_id=repo_id,
        onnx_filename=selected_file,
        revision=resolved_revision,
        data_dir=data_dir,
    )
    inspected = inspect_custom_onnx_artifact(
        artifact_path=str(staged_path),
        uploaded_filename=Path(selected_file).name,
    )

    requested_task = str(task or probe.get("detected_task") or "").strip().lower()
    preprocess_defaults = dict(probe.get("preprocess_defaults") or {})
    labels = list(probe.get("labels") or [])
    task_suggestions: list[dict[str, Any]] = []
    for suggestion in list(inspected.get("task_suggestions") or []):
        item = dict(suggestion)
        defaults = dict(item.get("defaults") or {})
        if requested_task and item.get("task") == requested_task:
            defaults.update({key: value for key, value in preprocess_defaults.items() if value not in (None, "", [], {})})
            if labels:
                defaults["labels_count_hint"] = len(labels)
            item["confidence"] = "high"
        item["defaults"] = defaults
        task_suggestions.append(item)

    inspected["task_suggestions"] = task_suggestions
    inspected["repo_id"] = repo_id
    inspected["source_url"] = probe["source_url"]
    inspected["resolved_revision"] = resolved_revision
    inspected["declared_license"] = probe["declared_license"]
    inspected["pipeline_tag"] = probe["pipeline_tag"]
    inspected["detected_task"] = probe["detected_task"]
    inspected["labels"] = labels
    inspected["preprocess_defaults"] = preprocess_defaults
    inspected["source_origin"] = "huggingface_hub"
    inspected["suggested_display_name"] = str(probe.get("suggested_display_name") or inspected.get("suggested_display_name") or "")
    return inspected


def import_huggingface_onnx_model(
    *,
    artifact_path: str,
    repo_id: str,
    resolved_revision: str,
    onnx_filename: str,
    display_name: str,
    task: str,
    adapter_family: str,
    uploaded_filename: str = "",
    tensor_name: str = "",
    width: int = 640,
    height: int = 640,
    layout: str = "nchw",
    color_order: str = "rgb",
    resize_mode: str = "stretch",
    rescale_factor: float = 1.0,
    normalization_mean: list[float] | None = None,
    normalization_std: list[float] | None = None,
    output_name: str = "",
    box_format: str = "xyxy01",
    class_labels: list[str] | None = None,
    replace_existing: bool = False,
    imported_by: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    probe = probe_huggingface_repo(repo=repo_id, revision=resolved_revision)
    source_url = f"https://huggingface.co/{repo_id}"
    model_id = f"hf_{str(task or '').strip().lower()}_{_slug_token(repo_id)}_{_slug_token(Path(onnx_filename).stem)}"
    declared_license = str(probe.get("declared_license") or "").strip()
    notes = [
        f"Downloaded from Hugging Face Hub repo '{repo_id}' at revision '{resolved_revision}'.",
        "Imported through the TopoSync Hugging Face origin.",
    ]
    return import_custom_onnx_model(
        artifact_path=artifact_path,
        display_name=display_name,
        task=str(task or "").strip().lower(),
        adapter_family=adapter_family,
        model_id=model_id,
        uploaded_filename=uploaded_filename or Path(onnx_filename).name,
        tensor_name=tensor_name,
        width=width,
        height=height,
        layout=layout,
        color_order=color_order,
        resize_mode=resize_mode,
        rescale_factor=rescale_factor,
        normalization_mean=normalization_mean,
        normalization_std=normalization_std,
        output_name=output_name,
        box_format=box_format,
        class_labels=class_labels,
        source_url=source_url,
        acquisition_mode="auto_download",
        provenance_origin="huggingface_hub",
        provenance_source_ref=resolved_revision,
        code_license=declared_license,
        weights_license=declared_license,
        commercial_use_status="declared_by_upstream",
        redistribution_allowed=False,
        official_build_allowed=False,
        notes=notes,
        replace_existing=replace_existing,
        imported_by=imported_by,
        data_dir=data_dir,
    )
