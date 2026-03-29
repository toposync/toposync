from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .local_build import (
    _preferred_host_python,
    _python_version_text,
    _python_version_tuple,
    _run_logged_command,
    _venv_marker_path,
    _venv_python_path,
)
from .manifests import ModelRegistryError

_DEFAULT_OPTIMUM_PIP_SPECS = [
    "optimum[onnx]>=1.27,<2",
    "transformers>=4.48,<5",
    "huggingface_hub>=0.34,<1",
    "torch>=2.4,<3",
]

_DEFAULT_EXPORT_GUIDE_URL = "https://huggingface.co/docs/optimum-onnx/onnx/usage_guides/export_a_model"

_HF_OPTIMUM_EXPORT_SCRIPT = """\
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download
from optimum.exporters.onnx import main_export


def _pick_exported_onnx(output_dir: Path) -> Path:
    candidates = [path for path in output_dir.rglob("*.onnx") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No ONNX file was generated in {output_dir}")

    def _score(path: Path) -> tuple[int, str]:
        lower = path.as_posix().lower()
        score = 0
        if lower.endswith("/model.onnx") or lower.endswith("model.onnx"):
            score += 50
        if "/onnx/" in lower:
            score += 20
        if "quant" not in lower and "int8" not in lower:
            score += 10
        return (-score, lower)

    candidates.sort(key=_score)
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--revision", default="")
    parser.add_argument("--task", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--metadata-path", required=True)
    parser.add_argument("--recipe-id", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = Path(args.metadata_path)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)

    snapshot_path = snapshot_download(
        repo_id=args.repo_id,
        revision=args.revision or None,
    )
    main_export(
        str(snapshot_path),
        output=output_dir,
        task=args.task,
        trust_remote_code=False,
        no_post_process=True,
    )

    exported = _pick_exported_onnx(output_dir)
    shutil.copy2(exported, output_path)
    metadata_path.write_text(
        json.dumps(
            {
                "repo_id": args.repo_id,
                "revision": args.revision,
                "task": args.task,
                "recipe_id": args.recipe_id,
                "snapshot_path": str(snapshot_path),
                "exported_source_path": str(exported),
                "exported_file": exported.name,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


@dataclass(frozen=True, slots=True)
class HuggingFaceExportRecipe:
    id: str
    label: str
    task: str
    export_task: str
    runtime_label: str
    guide_url: str = _DEFAULT_EXPORT_GUIDE_URL


_IMAGE_CLASSIFICATION_RECIPE = HuggingFaceExportRecipe(
    id="hf_optimum_image_classification",
    label="Optimum image classification export",
    task="classification",
    export_task="image-classification",
    runtime_label="Python + Optimum",
)

_SUPPORTED_IMAGE_CLASSIFICATION_MODEL_TYPES = {
    "beit",
    "convnext",
    "convnextv2",
    "deit",
    "dinat",
    "efficientformer",
    "efficientnet",
    "levit",
    "mobilenet_v1",
    "mobilenet_v2",
    "mobilevit",
    "poolformer",
    "regnet",
    "resnet",
    "swin",
    "vit",
    "vit_mae",
    "vit_msn",
}


def _builder_root(data_dir: str | Path | None = None) -> Path:
    if data_dir is not None:
        base = Path(data_dir).expanduser().resolve()
    else:
        raw = str(os.getenv("TOPOSYNC_DATA_DIR") or "").strip()
        if raw:
            base = Path(raw).expanduser().resolve()
        else:
            base = (Path.cwd() / ".toposync-data").resolve()
    return base / "vision-huggingface-builds"


def _optimum_pip_specs() -> list[str]:
    raw = str(os.getenv("TOPOSYNC_VISION_HF_EXPORT_PIP_SPECS") or "").strip()
    if not raw:
        return list(_DEFAULT_OPTIMUM_PIP_SPECS)
    items = [str(item or "").strip() for item in raw.split(",")]
    return [item for item in items if item] or list(_DEFAULT_OPTIMUM_PIP_SPECS)


def _env_token() -> str:
    token = "__".join(spec.replace("[", "_").replace("]", "_").replace("=", "_").replace("/", "_") for spec in _optimum_pip_specs())
    return token or "optimum"


def _env_dir(base_dir: Path) -> Path:
    return base_dir / "_builder_envs" / f"hf-optimum-py{sys.version_info.major}{sys.version_info.minor}-{_env_token()}"


def _context_dir(base_dir: Path) -> Path:
    return base_dir / "_builder_context"


def _workspace_dir(base_dir: Path, *, repo_id: str, job_id: str) -> Path:
    repo_token = str(repo_id or "").strip().lower().replace("/", "__")
    return base_dir / repo_token / job_id


def _build_log_path(base_dir: Path, job_id: str) -> Path:
    return base_dir / "logs" / job_id / "builder.log"


def _export_log_path(base_dir: Path, job_id: str) -> Path:
    return base_dir / "logs" / job_id / "export.log"


def _write_builder_context(context_dir: Path) -> Path:
    context_dir.mkdir(parents=True, exist_ok=True)
    script_path = context_dir / "huggingface_optimum_export.py"
    script_path.write_text(_HF_OPTIMUM_EXPORT_SCRIPT, encoding="utf-8")
    return script_path


def _supports_image_classification_recipe(config_payload: dict[str, Any]) -> tuple[bool, str]:
    auto_map = config_payload.get("auto_map")
    if isinstance(auto_map, dict) and auto_map:
        return False, "remote_code_required"
    model_type = str(config_payload.get("model_type") or "").strip().lower()
    if model_type and model_type in _SUPPORTED_IMAGE_CLASSIFICATION_MODEL_TYPES:
        return True, "ok"
    architectures = [str(item or "").strip() for item in list(config_payload.get("architectures") or []) if str(item or "").strip()]
    if any(text.endswith("ForImageClassification") for text in architectures):
        return True, "ok"
    return False, "architecture_unsupported"


def resolve_huggingface_export_recipe(
    *,
    pipeline_tag: str,
    detected_task: str,
    onnx_candidates: list[dict[str, Any]] | None = None,
    config_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_task = str(detected_task or "").strip().lower()
    clean_pipeline_tag = str(pipeline_tag or "").strip().lower()
    if onnx_candidates:
        return {
            "export_supported": False,
            "export_reason": "onnx_preferred",
            "recipe_id": "",
            "recipe_label": "",
            "runtime_label": "",
            "guide_url": "",
        }
    if clean_task != "classification" or clean_pipeline_tag != "image-classification":
        return {
            "export_supported": False,
            "export_reason": "task_unsupported",
            "recipe_id": "",
            "recipe_label": "",
            "runtime_label": "",
            "guide_url": "",
        }
    supported, reason = _supports_image_classification_recipe(dict(config_payload or {}))
    if not supported:
        return {
            "export_supported": False,
            "export_reason": reason,
            "recipe_id": "",
            "recipe_label": "",
            "runtime_label": "",
            "guide_url": "",
        }
    host_python_path, _host_python_name = _preferred_host_python()
    if not host_python_path:
        return {
            "export_supported": False,
            "export_reason": "python_runtime_missing",
            "recipe_id": _IMAGE_CLASSIFICATION_RECIPE.id,
            "recipe_label": _IMAGE_CLASSIFICATION_RECIPE.label,
            "runtime_label": _IMAGE_CLASSIFICATION_RECIPE.runtime_label,
            "guide_url": _IMAGE_CLASSIFICATION_RECIPE.guide_url,
        }
    version_tuple = _python_version_tuple(host_python_path)
    if version_tuple is None or version_tuple < (3, 10, 0):
        return {
            "export_supported": False,
            "export_reason": "python_version_unsupported",
            "recipe_id": _IMAGE_CLASSIFICATION_RECIPE.id,
            "recipe_label": _IMAGE_CLASSIFICATION_RECIPE.label,
            "runtime_label": _IMAGE_CLASSIFICATION_RECIPE.runtime_label,
            "guide_url": _IMAGE_CLASSIFICATION_RECIPE.guide_url,
        }
    return {
        "export_supported": True,
        "export_reason": "recipe_ready",
        "recipe_id": _IMAGE_CLASSIFICATION_RECIPE.id,
        "recipe_label": _IMAGE_CLASSIFICATION_RECIPE.label,
        "runtime_label": _IMAGE_CLASSIFICATION_RECIPE.runtime_label,
        "guide_url": _IMAGE_CLASSIFICATION_RECIPE.guide_url,
        "host_python_path": host_python_path,
        "python_version": _python_version_text(host_python_path),
    }


def _ensure_optimum_builder_env(*, python_executable: str, env_dir: Path, log_path: Path) -> str:
    marker_path = _venv_marker_path(env_dir)
    venv_python = _venv_python_path(env_dir)
    pip_specs = _optimum_pip_specs()
    current_python = _python_version_text(python_executable)
    if venv_python.is_file() and marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if (
                isinstance(marker, dict)
                and list(marker.get("pip_specs") or []) == pip_specs
                and str(marker.get("python") or "").strip() == current_python
            ):
                return str(venv_python)
        except Exception:
            pass

    if env_dir.exists():
        shutil.rmtree(env_dir, ignore_errors=True)
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_logged_command([python_executable, "-m", "venv", str(env_dir)], cwd=None, log_path=log_path)
    venv_python = _venv_python_path(env_dir)
    if not venv_python.is_file():
        raise FileNotFoundError(f"Hugging Face builder virtualenv python not found: {venv_python}")
    _run_logged_command(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools<81", "wheel"],
        cwd=None,
        log_path=log_path,
    )
    _run_logged_command([str(venv_python), "-m", "pip", "install", *pip_specs], cwd=None, log_path=log_path)
    marker_path.write_text(
        json.dumps(
            {
                "created_at": float(time.time()),
                "pip_specs": pip_specs,
                "python": current_python,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return str(venv_python)


def export_huggingface_recipe_model(
    *,
    repo_id: str,
    resolved_revision: str,
    recipe_id: str,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    if recipe_id != _IMAGE_CLASSIFICATION_RECIPE.id:
        raise ModelRegistryError(f"Unsupported Hugging Face export recipe: {recipe_id}")

    host_python_path, _host_python_name = _preferred_host_python()
    if not host_python_path:
        raise ModelRegistryError("No compatible Python runtime was found for Hugging Face local export")
    version_tuple = _python_version_tuple(host_python_path)
    if version_tuple is None or version_tuple < (3, 10, 0):
        raise ModelRegistryError("Hugging Face local export requires Python 3.10 or newer")

    base_dir = _builder_root(data_dir=data_dir)
    job_id = uuid.uuid4().hex
    workspace_dir = _workspace_dir(base_dir, repo_id=repo_id, job_id=job_id)
    output_dir = workspace_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / _safe_filename(f"{repo_id.replace('/', '_')}.onnx", fallback="hf-model.onnx")
    metadata_path = output_dir / "builder-metadata.json"
    build_log_path = _build_log_path(base_dir, job_id)
    export_log_path = _export_log_path(base_dir, job_id)
    script_path = _write_builder_context(_context_dir(base_dir))
    venv_python = _ensure_optimum_builder_env(
        python_executable=host_python_path,
        env_dir=_env_dir(base_dir),
        log_path=build_log_path,
    )
    _run_logged_command(
        [
            venv_python,
            str(script_path),
            "--repo-id",
            repo_id,
            "--revision",
            resolved_revision,
            "--task",
            _IMAGE_CLASSIFICATION_RECIPE.export_task,
            "--output-dir",
            str(output_dir),
            "--output-path",
            str(output_path),
            "--metadata-path",
            str(metadata_path),
            "--recipe-id",
            recipe_id,
        ],
        cwd=workspace_dir,
        log_path=export_log_path,
    )
    if not output_path.is_file():
        raise FileNotFoundError(f"Hugging Face local export did not produce ONNX output: {output_path}")
    metadata: dict[str, Any] = {}
    if metadata_path.is_file():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metadata = dict(loaded)
        except Exception:
            metadata = {}
    return {
        "artifact_path": str(output_path),
        "uploaded_filename": output_path.name,
        "recipe_id": recipe_id,
        "recipe_label": _IMAGE_CLASSIFICATION_RECIPE.label,
        "builder_runtime": f"{_IMAGE_CLASSIFICATION_RECIPE.runtime_label} ({_python_version_text(host_python_path) or 'python'})",
        "build_log_path": str(export_log_path),
        "builder_metadata_path": str(metadata_path),
        "builder_metadata": metadata,
        "guide_url": _IMAGE_CLASSIFICATION_RECIPE.guide_url,
    }
