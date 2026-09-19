"""Private, persisted panoramic photographs belonging to one camera source.

This resource intentionally does not activate composition geometry. Device effects
are confined to the injected scanner; reconstruction uses a disposable process.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
import hashlib
import inspect
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable, Literal
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .panorama_reference import PanoramaReferenceCoordinator
from .panorama_region import region_policy, region_progress, region_resume_error, required_region_captures
from .settings import get_camera_device, get_camera_source
from .onvif.client import NORMALIZED_PAN_TILT_SPACES, NORMALIZED_ZOOM_POSITION_SPACE

_EXTENSION = "com.toposync.cameras"
_SOURCE = "/api/cameras/cameras/{camera_id}/sources/{source_id}/panorama"
_JOBS = "/api/cameras/panorama-jobs"
_ARTIFACTS = "/api/cameras/panorama-artifacts"
_IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
_ACTIVE = {"queued", "preparing", "exploring", "capturing", "returning", "processing", "stopping"}
_PHYSICAL = {"unknown", "stopped", "restored", "returning", "stop_unconfirmed", "ownership_lost"}
_FILE_TYPES = {
    "panorama": ("image/png", ".png"),
    "thumbnail": ("image/jpeg", ".jpg"),
    "coverage": ("image/png", ".png"),
    "source_indices": ("application/octet-stream", ".npy"),
    "model": ("application/json", ".json"),
    "report": ("application/json", ".json"),
}
_JOB_PUBLIC = {
    "operation",
    "capture_goal",
    "id",
    "camera_id",
    "source_id",
    "status",
    "phase",
    "captures_accepted",
    "planned_captures",
    "estimated_remaining_seconds",
    "error",
    "physical_state",
    "artifact_id",
    "preview_url",
    "can_resume",
    "created_at",
    "updated_at",
    "issues",
    "outcomes",
}
_SECRET_KEYS = {"password", "username", "credentials", "authorization", "rtsp_url", "url"}
_MAX_JOB_BYTES = 2 * 1024**3
_MAX_GLOBAL_BYTES = 8 * 1024**3
_MIN_FREE_BYTES = 1024**3
_MAX_CAPTURES = 256  # Acquisition and reconstruction share this fixed product budget.
_MAX_JOB_SECONDS = 20 * 60  # Acquisition time remains consumed across resumed runs.
_MAX_GRID_PILOT_CYCLES_PER_AXIS = 3
_PROMOTION_RATIO_EPSILON = 1e-9
_REQUIRED_ARTIFACT_FILES = frozenset({"panorama", "thumbnail", "coverage", "model", "report"})
_COVERAGE_EDGE_STATES = frozenset({"unconfirmed", "limit", "loop"})
_CANDIDATE_REASONS = {
    "quality_not_approved",
    "comparison_incompatible",
    "acquisition_completeness_regressed",
    "acquisition_progress_regressed",
    "acquisition_topology_regressed",
    "coverage_evidence_missing",
    "coverage_regressed",
    "coverage_alignment_unverified",
}


def _absolute_preplan_resume_error(checkpoint: dict[str, Any]) -> str | None:
    """Mirror the scanner's durable pilot states without issuing a command."""
    if checkpoint.get("mode") != "absolute" or "plan" in checkpoint:
        return None
    attempts = checkpoint.get("pilot_attempts")
    if attempts is None:
        attempts = []
    budget = checkpoint.get("grid_pilot_budget")
    if (
        not attempts
        and budget is None
        and "absolute_pilot_limits" not in checkpoint
    ):
        captures_without_intent = checkpoint.get("captures")
        return (
            "pilot_resume_unavailable"
            if isinstance(captures_without_intent, list) and captures_without_intent
            else None
        )
    if not isinstance(attempts, list):
        return "pilot_resume_unavailable"
    if (attempts or budget is not None) and (
        not isinstance(budget, dict)
        or budget.get("maximum_cycles_per_axis") != _MAX_GRID_PILOT_CYCLES_PER_AXIS
        or budget.get("captures_per_cycle") != 2
        or type(budget.get("cycles_planned")) is not int
        or budget["cycles_planned"] != len(attempts)
    ):
        return "pilot_resume_unavailable"
    observations = checkpoint.get("pilot_observations")
    if observations is None:
        observations = {}
    if not isinstance(observations, dict):
        return "pilot_resume_unavailable"
    from .panorama_capture import PanoramaCaptureError
    from .panorama_scan import (
        _absolute_grid_plan,
        _axis_overlap_geometry,
        _canonical_absolute_grid_axis_samples,
        _canonical_absolute_pilot_limits,
    )

    try:
        pilot_limits = _canonical_absolute_pilot_limits(
            checkpoint.get("absolute_pilot_limits")
        )
    except PanoramaCaptureError:
        return "pilot_resume_unavailable"
    captures = checkpoint.get("captures")
    if not isinstance(captures, list):
        return "pilot_resume_unavailable"
    resolved_samples: dict[str, list[dict[str, Any]]] = {}
    for axis in ("pan", "tilt"):
        span = pilot_limits[axis]["max"] - pilot_limits[axis]["min"]
        axis_attempts = [
            attempt
            for attempt in attempts
            if isinstance(attempt, dict) and attempt.get("axis") == axis
        ]
        if len(axis_attempts) > _MAX_GRID_PILOT_CYCLES_PER_AXIS:
            return "pilot_resume_unavailable"
        if not axis_attempts:
            if span == 0:
                resolved_samples[axis] = []
            elif _MAX_CAPTURES - len(captures) < 2:
                return "pilot_resume_unavailable"
            continue
        if any(
            attempt.get("state") not in {"cycle_closed", "stationary_no_op"}
            for attempt in axis_attempts
        ):
            return "pilot_resume_unavailable"
        closed_attempts = [
            attempt for attempt in axis_attempts if attempt.get("state") == "cycle_closed"
        ]
        if not closed_attempts:
            return "pilot_resume_unavailable"
        if any(
            not isinstance(attempt.get("cycle_id"), str)
            or not attempt["cycle_id"].startswith(f"{axis}:")
            for attempt in axis_attempts
        ):
            return "pilot_resume_unavailable"
        samples_value = observations.get(axis, [])
        if (
            not isinstance(samples_value, list)
            or len(samples_value) > 2 * _MAX_GRID_PILOT_CYCLES_PER_AXIS
        ):
            return "pilot_resume_unavailable"
        samples = copy.deepcopy(samples_value)
        if any(
            not isinstance(sample, dict) or sample.get("cycle_closed") is not True
            for sample in samples
        ):
            return "pilot_resume_unavailable"
        for attempt in closed_attempts:
            cycle_id = attempt.get("cycle_id")
            for key, kind in (
                ("outward_observation", "pilot_outward"),
                ("return_observation", "pilot_return"),
            ):
                nested = attempt.get(key)
                if nested is None:
                    continue
                if not isinstance(nested, dict) or nested.get("kind") != kind:
                    return "pilot_resume_unavailable"
                try:
                    canonical_nested = _canonical_absolute_grid_axis_samples(
                        [{**nested, "pilot_cycle_id": cycle_id, "cycle_closed": True}],
                        qualified_only=False,
                    )
                except PanoramaCaptureError:
                    return "pilot_resume_unavailable"
                if len(canonical_nested) != 1:
                    return "pilot_resume_unavailable"
                existing = next(
                    (
                        sample
                        for sample in samples
                        if isinstance(sample, dict)
                        and sample.get("pilot_cycle_id") == cycle_id
                        and sample.get("kind") == kind
                    ),
                    None,
                )
                enriched = {**nested, "pilot_cycle_id": cycle_id, "cycle_closed": True}
                if existing is None:
                    samples.append(enriched)
                    del samples[: -2 * _MAX_GRID_PILOT_CYCLES_PER_AXIS]
                else:
                    try:
                        canonical_existing = _canonical_absolute_grid_axis_samples(
                            [existing], qualified_only=False
                        )
                    except PanoramaCaptureError:
                        return "pilot_resume_unavailable"
                    if canonical_existing != canonical_nested:
                        return "pilot_resume_unavailable"
        try:
            if span <= 0:
                return "pilot_resume_unavailable"
            geometry = _axis_overlap_geometry(
                span=span,
                optical_samples=samples,
                maximum_count=_MAX_CAPTURES,
            )
            canonical_samples = _canonical_absolute_grid_axis_samples(samples)
            canonical_all_samples = _canonical_absolute_grid_axis_samples(
                samples, qualified_only=False
            )
        except PanoramaCaptureError:
            return "pilot_resume_unavailable"
        if len(canonical_samples) != geometry["optical_sample_count"]:
            return "pilot_resume_unavailable"
        closed_cycle_ids = {attempt["cycle_id"] for attempt in closed_attempts}
        sample_keys = [
            (sample["cycle_id"], sample["kind"])
            for sample in canonical_all_samples
        ]
        if (
            len(set(sample_keys)) != len(sample_keys)
            or any(
                cycle_id not in closed_cycle_ids or not cycle_id.startswith(f"{axis}:")
                for cycle_id, _kind in sample_keys
            )
        ):
            return "pilot_resume_unavailable"
        resolved_samples[axis] = samples
    if any(
        not isinstance(attempt, dict) or attempt.get("axis") not in {"pan", "tilt"}
        for attempt in attempts
    ):
        return "pilot_resume_unavailable"
    if resolved_samples.keys() == {"pan", "tilt"}:
        try:
            _absolute_grid_plan(
                limits=pilot_limits,
                axis_samples=resolved_samples,
                captures_used=len(captures),
                maximum_captures=_MAX_CAPTURES,
            )
        except PanoramaCaptureError:
            return "pilot_resume_unavailable"
    return None


_ISSUE_MESSAGES = {
    "region_policy_incompatible": "Esta captura usa outra versão do percurso inicial. Gere uma nova panorâmica.",
    "region_resume_unavailable": "Não foi possível confirmar um ponto seguro para continuar a região inicial. As fotografias guardadas continuam disponíveis para montagem.",
    "region_axes_unavailable": "Esta captura precisa de movimentos horizontal e vertical confirmados pela câmera.",
    "region_motion_unavailable": "Esta câmera não oferece o modo de movimento usado na captura inicial.",
    "region_progress_unverified": "Não foi possível confirmar uma nova parte da região dentro do limite desta captura.",
    "region_budget_exhausted": "A região inicial atingiu o limite de tempo, movimentos ou fotografias.",
    "region_direction_unverified": "O movimento observado não seguiu o sentido da faixa. As fotografias foram preservadas.",
    "region_transverse_support_unverified": "A conexão entre as duas faixas ainda precisa de mais suporte visual. As fotografias foram preservadas.",
    "return_failed": "O retorno da câmera falhou. Os arquivos já guardados foram preservados.",
    "capture_identity_unverified": "Não foi possível confirmar a origem da fotografia selecionada.",
    "selected_capture_frame_unavailable": "A fotografia selecionada deixou de estar disponível antes de ser guardada.",
    "required_region_missing": "A montagem ainda não contém todas as vistas da região inicial.",
    "reconstruction_queue_timeout": "A montagem está ocupada. As fotografias foram guardadas para tentar novamente.",
    "capture_queue_timeout": "A captura está ocupada. Tente novamente quando o trabalho em andamento terminar.",
    "horizontal_coverage_partial": "Parte de um lado ficou sem confirmação; a captura prosseguiu pelo outro lado.",
    "motion_not_observed": "Não foi possível confirmar visualmente um dos deslocamentos da câmera.",
    "reference_view_unobservable": "Não foi possível encontrar um enquadramento com detalhes suficientes para iniciar a panorâmica.",
    "horizontal_movement_unavailable": "Esta câmera não disponibiliza movimento horizontal para a panorâmica.",
    "axis_movement_unavailable": "A câmera não permite um dos movimentos necessários para esta etapa.",
    "vertical_branch_budget_exhausted": "Parte da área vertical ficou pendente para respeitar o limite desta captura.",
    "continuous_resume_unavailable": "Esta captura usa um percurso anterior. Gere uma nova panorâmica para usar a retomada atual.",
    "capture_interrupted": "A captura foi interrompida; as imagens aproveitadas foram guardadas.",
    "return_framing_unconfirmed": "Não conseguimos confirmar o enquadramento inicial da câmera.",
    "return_reference_unavailable": "A referência do enquadramento inicial não está disponível.",
    "vertical_coverage_partial": "Parte da área acima ou abaixo não pôde ser registrada.",
    "coverage_connection_unverified": "A ligação entre algumas fotografias não pôde ser confirmada.",
    "vertical_connection_unverified": "A ligação entre duas alturas não pôde ser confirmada.",
    "band_origin_route_unavailable": "As fotografias desta faixa não contêm um caminho confirmado de volta ao ponto central.",
    "band_origin_return_unverified": "A câmera se moveu, mas a imagem não confirmou o próximo ponto fotografado. Nenhum outro movimento foi iniciado.",
    "pan_initial_limit_unconfirmed": "O limite inicial de movimento horizontal não foi confirmado.",
    "pan_row_limit_unconfirmed": "Um limite de movimento horizontal não foi confirmado.",
    "tilt_initial_limit_unconfirmed": "O limite inicial de movimento vertical não foi confirmado.",
    "tilt_terminal_limit_unconfirmed": "Um limite de movimento vertical não foi confirmado.",
    "scan_budget_exhausted": "A captura atingiu seu limite de tempo ou de imagens.",
    "coverage_exceeds_capture_budget": "O alcance observado precisa de mais imagens que o limite desta captura.",
    "stability_timeout": "Não foi possível confirmar a estabilidade de algumas imagens a tempo.",
    "frame_acquisition_timeout": "Uma imagem não pôde ser confirmada dentro do tempo disponível.",
    "capture_not_stable": "Uma imagem não passou pela verificação de estabilidade.",
    "stop_unconfirmed": "Não conseguimos confirmar a parada da câmera.",
    "stop_observation_unconfirmed": "As imagens não permitiram confirmar a parada da câmera.",
    "control_lost": "Não foi possível manter o controle da câmera durante a captura.",
    "capture_write_failed": "Não foi possível salvar uma imagem da captura.",
    "checkpoint_write_failed": "Não foi possível salvar todo o progresso da captura.",
    "diagnostic_write_failed": "Não foi possível salvar todos os detalhes da verificação.",
    "acquisition_failed": "Não foi possível concluir uma etapa da captura.",
    "pilot_geometry_unconfirmed": "As imagens iniciais não permitiram planejar os próximos movimentos.",
    "pilot_axis_unconfirmed": "Não foi possível confirmar a resposta da câmera ao movimento inicial.",
    "pilot_cycle_unconfirmed": "A câmera não voltou de forma consistente durante os movimentos iniciais. Confira a imagem ao vivo e gere uma nova panorâmica.",
    "pilot_resume_unavailable": "Um movimento inicial ficou sem resultado confirmado. Confira a imagem ao vivo e gere uma nova panorâmica.",
    "absolute_grid_checkpoint_incompatible": "O plano de movimentos guardado não corresponde mais aos recursos desta câmera. Gere uma nova panorâmica.",
    "control_verification_resume_unavailable": "A verificação de movimento guardada não pode ser retomada com segurança. Inicie uma nova verificação.",
    "camera_movement_unsupported": "Esta câmera não disponibiliza os movimentos necessários para a captura automática.",
    "normalized_motion_unavailable": "Esta câmera não disponibiliza os movimentos necessários para esta etapa.",
    "optical_state_changed": "O campo de visão mudou durante a captura. Gere uma nova panorâmica.",
    "source_geometry_changed": "O formato ou a orientação da imagem mudou durante a captura. Gere uma nova panorâmica.",
    "checkpoint_source_changed": "A transmissão mudou desde a captura anterior. Gere uma nova panorâmica.",
    "correction_precondition_changed": "O enquadramento mudou antes do ajuste fino; nenhum novo movimento foi iniciado.",
    "visual_control_resolution_unverified": "O controle disponível não confirmou a precisão necessária para este ajuste. Confira a imagem ao vivo antes de mover a câmera novamente.",
    "visual_response_unavailable": "Não foi possível medir a resposta da câmera ao movimento. Confira a imagem ao vivo antes de tentar novamente.",
    "relocalization_required": "Não foi possível confirmar o enquadramento do trecho pendente. As fotografias foram preservadas; você pode gerar uma nova panorâmica.",
    "invalid_checkpoint_file": "Uma imagem necessária para retomar a captura não está disponível.",
    "invalid_checkpoint_image": "Uma imagem guardada não pôde ser lida para retomar a captura.",
    "camera_unavailable": "Esta câmera não está disponível para a captura.",
    "camera_discovery_failed": "Não foi possível identificar os recursos disponíveis desta câmera.",
    "camera_configuration_changed": "A configuração da câmera mudou durante a captura.",
    "source_binding_unverified": "Não foi possível confirmar que esta transmissão pertence à câmera selecionada.",
    "direct_source_binding_unverified": "Não foi possível confirmar uma conexão alternativa para esta mesma transmissão.",
    "direct_source_unavailable": "A conexão alternativa com esta transmissão não está disponível.",
    "external_automation_active": "Uma automação da câmera está ativa. Interrompa essa automação antes de gerar a panorâmica.",
    "control_unavailable": "Não foi possível obter o controle da câmera para esta captura.",
    "position_unavailable": "A posição atual da câmera não pôde ser consultada.",
    "absolute_position_unavailable": "A câmera não disponibiliza a posição necessária para este movimento.",
    "movement_unconfirmed": "O movimento solicitado à câmera não pôde ser confirmado.",
    "outside_camera_limits": "Um movimento necessário ultrapassaria os limites informados pela câmera.",
    "invalid_velocity": "Não foi possível preparar um movimento válido para esta câmera.",
    "invalid_movement_duration": "Não foi possível preparar a duração de um movimento desta câmera.",
    "video_unavailable": "Não foi possível abrir as imagens desta transmissão.",
    "invalid_frame_timeout": "Não foi possível preparar a verificação de uma imagem.",
    "invalid_frame": "Uma imagem recebida não pôde ser utilizada.",
    "fresh_frame_unavailable": "Não foi possível confirmar uma imagem nova desta transmissão.",
    "return_unavailable": "O retorno ao enquadramento inicial não está disponível.",
    "return_capacity_unavailable": "A câmera não tem uma posição guardada disponível para o retorno desta captura.",
    "return_coarse_observation_unconfirmed": "A câmera se moveu em direção ao enquadramento inicial, mas as imagens não confirmaram onde ela parou. Confira a imagem ao vivo antes de movê-la novamente.",
    "return_correction_budget_exhausted": "O retorno automático atingiu o limite seguro de ajustes. Use a imagem ao vivo para posicionar a câmera, se necessário.",
    "original_reference_changed": "O enquadramento inicial mudou enquanto sua referência era guardada. Confira a imagem ao vivo e gere uma nova panorâmica.",
    "working_reference_changed": "O enquadramento guardado para continuar a captura mudou. Confira a imagem ao vivo e gere uma nova panorâmica.",
    "working_reference_unavailable": "O enquadramento necessário para continuar a captura não está disponível. Gere uma nova panorâmica.",
    "return_owner_mismatch": "Não foi possível confirmar que a referência de retorno pertence a esta captura.",
    "return_preset_unverified": "A posição guardada para o retorno não pôde ser confirmada.",
    "return_preset_changed": "A posição guardada para o retorno foi alterada.",
    "return_cleanup_unconfirmed": "Não foi possível confirmar a remoção da posição temporária usada no retorno.",
    "job_storage_exceeded": "A captura atingiu o limite de armazenamento.",
    "insufficient_disk_space": "O espaço disponível terminou durante a captura.",
    "partial_preserved_previous_complete": "A nova captura é parcial; a panorâmica completa anterior foi preservada.",
    "active_panorama_preserved": "A panorâmica atual foi preservada porque a nova captura não confirmou que mantém as mesmas regiões cobertas.",
    "scan_completeness_unverified": "A captura anterior não contém uma confirmação de cobertura completa.",
    "scene_texture_insufficient": "Não foi possível reconhecer detalhes suficientes em algumas imagens observadas.",
    "rejected_observation_write_failed": "Não foi possível guardar uma imagem usada na verificação.",
}


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CreateSourcePanorama(_Input):
    idempotency_key: str = Field(min_length=8, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")
    operation: Literal["capture", "verify_control"] = "capture"


class PanoramaCrop(_Input):
    u_start: float = Field(ge=0, lt=1)
    u_width: float = Field(gt=0, le=1)
    v_start: float = Field(ge=0, lt=1)
    v_height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def bounded_vertical_interval(self) -> PanoramaCrop:
        if self.v_start + self.v_height > 1 + 1e-9:
            raise ValueError("The vertical crop must remain inside the panorama")
        return self


class SavePanoramaCrop(_Input):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_revision: int = Field(ge=1)
    crop: PanoramaCrop


def _error(code: str, message: str, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _safe(value: Any, depth: int = 0) -> Any:
    """Defence in depth for diagnostic payloads, never serialize raw exceptions."""
    if depth > 20:
        return None
    if isinstance(value, dict):
        return {
            str(key): _safe(item, depth + 1)
            for key, item in value.items()
            if str(key).lower() not in _SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value[:2048]]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        # These exact identifiers describe coordinate systems, never endpoints.
        # Redacting them corrupts persisted return destinations and blocks recall.
        if value in NORMALIZED_PAN_TILT_SPACES or value == NORMALIZED_ZOOM_POSITION_SPACE:
            return value
        if re.search(r"(?:rtsp|rtsps|https?)://", value, re.IGNORECASE):
            return "[private transport]"
        return value[:4096]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _is_quality_approved(quality: Any) -> bool:
    """Accept only an explicit successful reconstruction quality verdict."""
    return (
        isinstance(quality, dict)
        and quality.get("status") == "ready"
        and isinstance(quality.get("reasons"), list)
        and not quality["reasons"]
    )


def _artifact_quality_approved(artifact: Any) -> bool:
    """Accept a modern approval or a legacy artifact with the same explicit verdict."""
    return (
        isinstance(artifact, dict)
        and _is_quality_approved(artifact.get("quality"))
        and (
            "quality_approved" not in artifact
            or artifact.get("quality_approved") is True
        )
    )


def _artifact_coverage(artifact: dict[str, Any]) -> dict[str, Any]:
    coverage = artifact.get("coverage")
    return coverage if isinstance(coverage, dict) else {}


def _semantic_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and normalized.lower() != "unknown" else None


def _coverage_ratio(
    artifact: dict[str, Any], name: str
) -> tuple[bool, float | None]:
    coverage = _artifact_coverage(artifact)
    candidates: list[Any] = []
    present = name in coverage
    if present:
        value = coverage.get(name)
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and 0 <= value <= 1
        ):
            return True, float(value)
        return True, None
    if name == "pixel_ratio":
        for container, key in (
            (artifact, "coverage_ratio"),
            (coverage, "ratio"),
            (coverage, "coverage_ratio"),
        ):
            if key in container:
                present = True
                candidates.append(container.get(key))
    for value in candidates:
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and 0 <= value <= 1
        ):
            return present, float(value)
    return present, None


def _acquisition_progress(artifact: dict[str, Any]) -> dict[str, Any]:
    acquisition = _artifact_coverage(artifact).get("acquisition")
    if not isinstance(acquisition, dict):
        acquisition = artifact.get("_scan_coverage")
    progress = acquisition.get("progress") if isinstance(acquisition, dict) else None
    return progress if isinstance(progress, dict) else {}


def _acquisition_complete(artifact: dict[str, Any]) -> bool:
    coverage = _artifact_coverage(artifact)
    value = coverage.get("acquisition_complete")
    return value if type(value) is bool else artifact.get("status") == "ready"


def _optional_acquisition_evidence_is_well_formed(artifact: dict[str, Any]) -> bool:
    coverage = _artifact_coverage(artifact)
    if "acquisition" not in coverage:
        return True
    acquisition = coverage.get("acquisition")
    if not isinstance(acquisition, dict):
        return False
    if "progress" in acquisition:
        progress = acquisition.get("progress")
        if not isinstance(progress, dict):
            return False
        if "primary_complete" in progress and type(progress.get("primary_complete")) is not bool:
            return False
        bands_completed = progress.get("bands_completed")
        if "bands_completed" in progress and (
            type(bands_completed) is not int or bands_completed < 0
        ):
            return False
    boundaries = acquisition.get("boundaries")
    if boundaries is not None and (
        not isinstance(boundaries, dict)
        or any(
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(boundary, dict)
            or (
                "confirmed" in boundary
                and type(boundary.get("confirmed")) is not bool
            )
            for identifier, boundary in boundaries.items()
        )
    ):
        return False
    bands = acquisition.get("bands")
    if bands is not None and (
        not isinstance(bands, dict)
        or any(
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(band, dict)
            or type(band.get("complete")) is not bool
            or not isinstance(band.get("edges"), dict)
            or any(
                not isinstance(edge_id, str)
                or not edge_id
                or edge_status not in _COVERAGE_EDGE_STATES
                for edge_id, edge_status in band.get("edges", {}).items()
            )
            for identifier, band in bands.items()
        )
    ):
        return False
    progress = acquisition.get("progress")
    if isinstance(progress, dict) and isinstance(bands, dict) and bands:
        if "0" not in bands:
            return False
        if (
            "bands_completed" in progress
            and progress["bands_completed"]
            != sum(band["complete"] is True for band in bands.values())
        ):
            return False
        if (
            "primary_complete" in progress
            and progress["primary_complete"] is not bands["0"]["complete"]
        ):
            return False
    acquisition_complete = coverage.get("acquisition_complete")
    if acquisition_complete is True and isinstance(progress, dict):
        if progress.get("primary_complete") is not True or progress.get("bands_completed", 0) < 1:
            return False
    return True


def _active_reference_is_valid(reference: Any, artifact: dict[str, Any]) -> bool:
    if (
        not isinstance(reference, dict)
        or reference.get("artifact_id") != artifact.get("id")
        or type(reference.get("crop_revision")) is not int
        or reference["crop_revision"] < 1
    ):
        return False
    try:
        PanoramaCrop.model_validate(reference.get("crop"))
    except (TypeError, ValueError):
        return False
    return True


def _artifact_files_are_readable_in_directory(
    artifact: dict[str, Any], directory: Path
) -> bool:
    files = artifact.get("_files")
    if (
        not isinstance(files, dict)
        or not _REQUIRED_ARTIFACT_FILES <= files.keys()
    ):
        return False
    try:
        directory = directory.resolve()
        for file_id in _REQUIRED_ARTIFACT_FILES:
            filename = files.get(file_id)
            if not isinstance(filename, str) or not filename:
                return False
            path = (directory / filename).resolve()
            if (
                not path.is_relative_to(directory)
                or path.suffix.lower() != _FILE_TYPES[file_id][1]
                or not path.is_file()
            ):
                return False
            with path.open("rb") as stream:
                if not stream.read(1):
                    return False
    except (OSError, RuntimeError):
        return False
    return True


def _artifact_files_are_readable(artifact: dict[str, Any], artifacts_root: Path) -> bool:
    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, str) or not _IDENTIFIER.fullmatch(artifact_id):
        return False
    try:
        root = artifacts_root.resolve()
        directory = (root / artifact_id).resolve()
        if not directory.is_relative_to(root):
            return False
    except (OSError, RuntimeError):
        return False
    return _artifact_files_are_readable_in_directory(artifact, directory)


def _promotion_eligibility_reason(artifact: dict[str, Any]) -> str | None:
    """Require comparable evidence even when this would be the first active artifact."""
    if not _artifact_quality_approved(artifact):
        return "quality_not_approved"
    if artifact.get("status") not in {"ready", "partial"}:
        return "coverage_evidence_missing"
    algorithm = _semantic_token(artifact.get("algorithm_version"))
    provenance = _semantic_token(_artifact_coverage(artifact).get("provenance"))
    if algorithm is None or provenance is None:
        return "comparison_incompatible"
    coverage = _artifact_coverage(artifact)
    for name in ("pixel_ratio", "solid_angle_ratio"):
        value = coverage.get(name)
        if (
            name not in coverage
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            return "coverage_evidence_missing"
    acquisition_complete = coverage.get("acquisition_complete")
    if (
        type(acquisition_complete) is not bool
        or acquisition_complete != (artifact["status"] == "ready")
    ):
        return "coverage_evidence_missing"
    acquisition = coverage.get("acquisition")
    progress = acquisition.get("progress") if isinstance(acquisition, dict) else None
    if (
        not isinstance(progress, dict)
        or type(progress.get("primary_complete")) is not bool
        or type(progress.get("bands_completed")) is not int
        or progress["bands_completed"] < 0
        or not _optional_acquisition_evidence_is_well_formed(artifact)
    ):
        return "coverage_evidence_missing"
    return None


def _active_artifact_is_current(
    artifact: Any,
    *,
    reference: Any,
    artifacts_root: Path,
    camera_id: str,
    source_id: str,
    current_identity: str,
) -> bool:
    if not isinstance(artifact, dict) or artifact.get("status") not in {"ready", "partial"}:
        return False
    coverage = _artifact_coverage(artifact)
    explicit_complete = coverage.get("acquisition_complete")
    if type(explicit_complete) is not bool:
        return False
    if explicit_complete != (artifact["status"] == "ready"):
        return False
    for name in ("pixel_ratio", "solid_angle_ratio"):
        value = coverage.get(name)
        if (
            name not in coverage
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            return False
    progress = _acquisition_progress(artifact)
    return (
        artifact.get("camera_id") == camera_id
        and artifact.get("source_id") == source_id
        and artifact.get("_identity") == current_identity
        and _artifact_quality_approved(artifact)
        and _semantic_token(artifact.get("algorithm_version")) is not None
        and _semantic_token(coverage.get("provenance")) is not None
        and type(progress.get("primary_complete")) is bool
        and type(progress.get("bands_completed")) is int
        and progress["bands_completed"] >= 0
        and _active_reference_is_valid(reference, artifact)
        and _optional_acquisition_evidence_is_well_formed(artifact)
        and _artifact_files_are_readable(artifact, artifacts_root)
    )


def _promotion_candidate_reason(
    artifact: dict[str, Any],
    active: dict[str, Any] | None,
    *,
    active_is_current: bool,
) -> str | None:
    """Return a fixed public reason when automatic promotion is not proven safe."""
    eligibility_reason = _promotion_eligibility_reason(artifact)
    if eligibility_reason is not None:
        return eligibility_reason
    if not active_is_current:
        return None
    assert active is not None
    active_algorithm = _semantic_token(active.get("algorithm_version"))
    candidate_algorithm = _semantic_token(artifact.get("algorithm_version"))
    if active_algorithm is None or candidate_algorithm != active_algorithm:
        return "comparison_incompatible"
    active_coverage = _artifact_coverage(active)
    candidate_coverage = _artifact_coverage(artifact)
    active_provenance = _semantic_token(active_coverage.get("provenance"))
    candidate_provenance = _semantic_token(candidate_coverage.get("provenance"))
    if active_provenance is None or candidate_provenance != active_provenance:
        return "comparison_incompatible"
    if type(active_coverage.get("acquisition_complete")) is not bool:
        return "coverage_evidence_missing"
    for name in ("pixel_ratio", "solid_angle_ratio"):
        active_value = active_coverage.get(name)
        if (
            name not in active_coverage
            or isinstance(active_value, bool)
            or not isinstance(active_value, (int, float))
            or not math.isfinite(active_value)
            or not 0 <= active_value <= 1
        ):
            return "coverage_evidence_missing"
    if _acquisition_complete(active) and not _acquisition_complete(artifact):
        return "acquisition_completeness_regressed"

    active_progress = _acquisition_progress(active)
    candidate_progress = _acquisition_progress(artifact)
    if (
        type(active_progress.get("primary_complete")) is not bool
        or type(active_progress.get("bands_completed")) is not int
        or active_progress["bands_completed"] < 0
    ):
        return "coverage_evidence_missing"
    if "primary_complete" in active_progress:
        old_primary = active_progress.get("primary_complete")
        new_primary = candidate_progress.get("primary_complete")
        if type(old_primary) is not bool or type(new_primary) is not bool:
            return "coverage_evidence_missing"
        if old_primary and not new_primary:
            return "acquisition_progress_regressed"
    if "bands_completed" in active_progress:
        old_bands = active_progress.get("bands_completed")
        new_bands = candidate_progress.get("bands_completed")
        if (
            type(old_bands) is not int
            or type(new_bands) is not int
            or old_bands < 0
            or new_bands < 0
        ):
            return "coverage_evidence_missing"
        if new_bands < old_bands:
            return "acquisition_progress_regressed"

    active_acquisition = active_coverage.get("acquisition")
    candidate_acquisition = candidate_coverage.get("acquisition")
    active_acquisition = active_acquisition if isinstance(active_acquisition, dict) else {}
    candidate_acquisition = (
        candidate_acquisition if isinstance(candidate_acquisition, dict) else {}
    )
    active_boundaries = active_acquisition.get("boundaries", {})
    candidate_boundaries = candidate_acquisition.get("boundaries", {})
    active_boundaries = active_boundaries if isinstance(active_boundaries, dict) else {}
    candidate_boundaries = (
        candidate_boundaries if isinstance(candidate_boundaries, dict) else {}
    )
    if any(
        boundary.get("confirmed") is True
        and (
            not isinstance(candidate_boundaries.get(identifier), dict)
            or candidate_boundaries[identifier].get("confirmed") is not True
        )
        for identifier, boundary in active_boundaries.items()
    ):
        return "acquisition_topology_regressed"

    active_bands = active_acquisition.get("bands", {})
    candidate_bands = candidate_acquisition.get("bands", {})
    active_bands = active_bands if isinstance(active_bands, dict) else {}
    candidate_bands = candidate_bands if isinstance(candidate_bands, dict) else {}
    for identifier, active_band in active_bands.items():
        candidate_band = candidate_bands.get(identifier)
        if not isinstance(candidate_band, dict):
            return "acquisition_topology_regressed"
        if active_band.get("complete") is True and candidate_band.get("complete") is not True:
            return "acquisition_topology_regressed"
        candidate_edges = candidate_band.get("edges", {})
        for edge_id, active_status in active_band.get("edges", {}).items():
            candidate_status = candidate_edges.get(edge_id)
            if candidate_status is None or (
                active_status in {"limit", "loop"} and candidate_status != active_status
            ):
                return "acquisition_topology_regressed"

    for name in ("pixel_ratio", "solid_angle_ratio"):
        old_present, old_value = _coverage_ratio(active, name)
        new_present, new_value = _coverage_ratio(artifact, name)
        if old_present and (old_value is None or not new_present or new_value is None):
            return "coverage_evidence_missing"
        if (
            old_value is not None
            and new_value is not None
            and new_value + _PROMOTION_RATIO_EPSILON < old_value
        ):
            return "coverage_regressed"

    # Scalar and topology evidence cannot prove that the candidate preserved
    # the same visual region. Until reconstruction persists an aligned coverage
    # relation, retain the current artifact and expose this one as a candidate.
    return "coverage_alignment_unverified"


def _replacement_candidate_reason(
    artifact: dict[str, Any],
    active: dict[str, Any] | None,
    *,
    active_is_current: bool,
    policy_version: int | None,
) -> str | None:
    reason = _promotion_candidate_reason(
        artifact,
        active,
        active_is_current=active_is_current,
    )
    if (
        policy_version == 4
        and artifact.get("status") in {"ready", "partial"}
        and _artifact_quality_approved(artifact)
    ):
        return None
    return reason


def _public_telemetry(value: Any) -> dict[str, Any] | None:
    """Only the latest bounded, scalar motion observations leave the job store."""
    if not isinstance(value, dict) or value.get("kind") != "movement":
        return None
    samples = value.get("samples")
    if not isinstance(samples, list) or not samples:
        return None

    def number(item: Any, *, minimum: float = 0, maximum: float = 1_000_000) -> float | None:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        return float(item) if math.isfinite(item) and minimum <= item <= maximum else None

    timing_basis = value.get("timing_basis")
    if timing_basis not in {"media", "local_observation"}:
        timing_basis = None
    output: dict[str, Any] = {
        "kind": "movement",
        "outcome": value.get("outcome")
        if value.get("outcome") in {"accepted", "timeout", "inconclusive"}
        else "inconclusive",
        "timing_basis": timing_basis,
        "analysis_width": number(value.get("analysis_width"), minimum=2, maximum=8192),
        "samples": [],
    }
    indices = (
        range(len(samples))
        if len(samples) <= 128
        else (round(index * (len(samples) - 1) / 127) for index in range(128))
    )
    previous_time = -1.0
    for index in indices:
        sample = samples[index]
        if not isinstance(sample, dict):
            continue
        elapsed = number(sample.get("elapsed_seconds"), maximum=3600)
        if elapsed is None or elapsed < previous_time:
            continue
        previous_time = elapsed
        media_time = number(sample.get("media_time"), maximum=1_000_000_000_000)
        entry = {
            "elapsed_seconds": elapsed,
            "motion_pixels": number(sample.get("motion_pixels")),
            "speed_px_s": number(sample.get("speed_px_s"))
            if timing_basis == "media" and media_time is not None
            else None,
            "media_time": media_time,
            "drift_pixels": number(sample.get("drift_pixels")),
            "confidence": number(sample.get("confidence"), maximum=1),
            "state": sample.get("state")
            if sample.get("state") in {"observing", "stable", "timeout"}
            else "unknown",
        }
        pose = sample.get("pose")
        if isinstance(pose, dict):
            entry["pose"] = {
                key: number(pose.get(key), minimum=-1_000_000_000, maximum=1_000_000_000)
                for key in ("pan", "tilt", "native_pan", "native_tilt")
            }
        output["samples"].append(entry)
    if not output["samples"]:
        return None
    for field in (
        "command_accepted_seconds",
        "first_target_readback_seconds",
        "first_motion_transition_seconds",
        "stop_requested_seconds",
        "stop_accepted_seconds",
    ):
        if field in value:
            output[field] = number(value[field], maximum=3600)
    return output


def _identity(camera: dict[str, Any], source: dict[str, Any]) -> str:
    # Optical fields remain significant; only presentation/asset metadata is ignored.
    context = {
        "camera": {
            key: item
            for key, item in camera.items()
            if key not in {"sources", "name", "label", "metadata"}
        },
        "source": {
            key: item for key, item in source.items() if key not in {"name", "label", "metadata"}
        },
        "mount_revision": camera.get("metadata", {}).get("panorama_mount_revision"),
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()


def _preserve_panorama_references(
    current: dict[str, Any], proposed: dict[str, Any]
) -> dict[str, Any]:
    """Generic settings drafts cannot overwrite this resource's revisioned data.

    Source deletion is respected. All other proposed settings, including optical
    changes and unrelated metadata, remain under the normal settings contract.
    """
    references: dict[tuple[str, str], Any] = {}
    for camera in current.get("devices", []):
        if not isinstance(camera, dict):
            continue
        for source in camera.get("sources", []):
            if not isinstance(source, dict):
                continue
            metadata = source.get("metadata")
            if isinstance(metadata, dict) and "panorama" in metadata:
                references[(str(camera.get("id")), str(source.get("id")))] = copy.deepcopy(
                    metadata["panorama"]
                )
    devices = proposed.get("devices")
    if not isinstance(devices, list):
        return proposed
    for camera in devices:
        if not isinstance(camera, dict) or not isinstance(camera.get("sources"), list):
            continue
        for source in camera["sources"]:
            if not isinstance(source, dict):
                continue
            key = (str(camera.get("id")), str(source.get("id")))
            metadata = copy.deepcopy(source.get("metadata"))
            if key in references:
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["panorama"] = copy.deepcopy(references[key])
                source["metadata"] = metadata
            elif isinstance(metadata, dict) and "panorama" in metadata:
                metadata.pop("panorama")
                source["metadata"] = metadata
    return proposed


def _reconstruction_process(
    captures: list[dict[str, Any]], directory: str, connection: Any, cancellation: Any
) -> None:
    """A top-level spawn target; no settings, network client or credentials enter it."""
    from .processing.panorama_reconstruction import reconstruct_panorama

    last_progress = 0.0

    def progress(event: dict[str, Any]) -> None:
        nonlocal last_progress
        if time.monotonic() - last_progress >= 0.2:
            connection.send({"progress": _safe(event)})
            last_progress = time.monotonic()

    try:
        result = reconstruct_panorama(
            captures, Path(directory), progress=progress, cancelled=cancellation.is_set
        )
        connection.send(
            {
                "result": _safe(result),
                "quality_approved": _is_quality_approved(result.get("quality", {})),
            }
        )
    except BaseException as exc:
        # Exception strings can contain stream URLs; only transport a typed code.
        code = str(getattr(exc, "code", "reconstruction_failed"))
        if not re.fullmatch(r"[a-z_]{3,80}", code):
            code = "reconstruction_failed"
        connection.send({"error": code})
    finally:
        connection.close()


class SourcePanoramaService:
    def __init__(
        self,
        app: FastAPI,
        *,
        services: Any,
        authorize: Any,
        read_settings: Any,
        camera_factory: Callable[..., Any] | None = None,
        scan_runner: Callable[..., Any] | None = None,
        reconstruct_runner: Callable[..., Any] | None = None,
        max_job_bytes: int = _MAX_JOB_BYTES,
        max_global_bytes: int = _MAX_GLOBAL_BYTES,
        minimum_free_bytes: int = _MIN_FREE_BYTES,
        reconstruction_timeout_seconds: float = 15 * 60,
        reference_coordinator: PanoramaReferenceCoordinator | None = None,
        acquisition_policy: dict[str, Any] | None = None,
    ) -> None:
        self.services, self.authorize, self.read_settings = services, authorize, read_settings
        self.store = app.state.config_store
        register_filter = getattr(self.store, "register_extension_settings_patch_filter", None)
        if callable(register_filter):
            register_filter(_EXTENSION, _preserve_panorama_references)
        self.root = self.store.paths.data_dir / "runtime" / "cameras" / "source-panorama"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.camera_factory = camera_factory
        # No regional policy selects the full reachable-domain scanner. A
        # persisted legacy job always keeps its recorded regional policy.
        self.acquisition_policy = region_policy(acquisition_policy)
        self.scan_runner, self.reconstruct_runner = scan_runner, reconstruct_runner
        self.max_job_bytes, self.max_global_bytes = max_job_bytes, max_global_bytes
        self.minimum_free_bytes = minimum_free_bytes
        if not math.isfinite(reconstruction_timeout_seconds) or reconstruction_timeout_seconds <= 0:
            raise ValueError("Reconstruction timeout must be a positive finite duration")
        self.reconstruction_timeout_seconds = reconstruction_timeout_seconds
        self.reference_coordinator = reference_coordinator or PanoramaReferenceCoordinator()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.cameras: dict[str, Any] = {}
        self.cancelled: set[str] = set()
        self.lock = asyncio.Lock()
        self.acquisition = asyncio.Semaphore(1)
        self.processing = asyncio.Semaphore(1)
        self.closed = False
        for path in (self.root / "jobs").glob("*/job.json"):
            job = self._read(path)
            if (
                not job
                or not _IDENTIFIER.fullmatch(path.parent.name)
                or job.get("id") != path.parent.name
            ):
                continue
            if job.get("status") in _ACTIVE:
                processing_interrupted = (
                    bool(job.get("_processing_only"))
                    or job.get("status") == "processing"
                    or job.get("phase") == "stopping_processing"
                )
                job.update(
                    status="interrupted",
                    phase="interrupted_processing" if processing_interrupted else "interrupted",
                    physical_state=job.get("physical_state", "unknown")
                    if processing_interrupted
                    else "unknown",
                    can_resume=self._can_resume(job),
                    error={
                        "code": "process_interrupted",
                        "message": "A montagem foi interrompida. As imagens foram guardadas."
                        if processing_interrupted
                        else "A captura foi interrompida. As imagens foram guardadas.",
                    },
                )
                self._save(job)
            self.jobs[job["id"]] = job

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                json.dump(_safe(value), handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _job_directory(self, job: dict[str, Any]) -> Path:
        return self.root / "jobs" / job["id"]

    def _save(self, job: dict[str, Any]) -> None:
        if (job.get("_acquisition_policy") or {}).get("version") in {2, 3, 4}:
            job["outcomes"] = self._regional_outcomes(job)
        job["updated_at"] = time.time()
        self._atomic(self._job_directory(job) / "job.json", job)

    @staticmethod
    def _regional_outcomes(job: dict[str, Any]) -> dict[str, str]:
        checkpoint = job.get("_checkpoint") or {}
        acquiring = job.get("status") in {"preparing", "exploring", "capturing"}
        return_attempted = job.get("_return_attempted") or any(
            isinstance(issue, dict) and issue.get("code") in {"return_failed", "return_framing_unconfirmed"}
            for issue in job.get("issues", [])
        )
        return {
            "acquisition": (("completed" if (job.get("_acquisition_policy") or {}).get("version") == 4 else "sufficient") if required_region_captures(checkpoint)
                            else "running" if acquiring else "pending" if not checkpoint else "incomplete"),
            "reconstruction": job.get("_reconstruction_status", "pending"),
            "return": ("verified" if job.get("physical_state") == "restored"
                       else "running" if job.get("physical_state") == "returning" and job.get("status") in _ACTIVE
                       else "unverified" if return_attempted else "pending"),
        }

    @staticmethod
    def _regional_progress(job: dict[str, Any]) -> dict[str, Any] | None:
        checkpoint = job.get("_checkpoint") or {}
        policy_version = (job.get("_acquisition_policy") or {}).get("version")
        region_version = ((checkpoint.get("continuous_cursor") or {}).get("region") or {}).get("version")
        if ((policy_version in {2, 3} and region_version == 2)
                or (policy_version == 4 and region_version == 4)):
            return _safe(region_progress(checkpoint))
        return None

    def _public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        output = _safe({key: value for key, value in job.items() if key in _JOB_PUBLIC})
        if isinstance(job.get("return_closure"), dict):
            output["return_closure"] = {key: value for key, value in job["return_closure"].items()
                                        if key != "references"}
        verification = (job.get("_checkpoint") or {}).get("control_verification", {})
        if job.get("operation") == "verify_control":
            output["control_checks_passed"] = sum(
                item.get("status") == "verified" for item in verification.get("checks", [])
            )
        output["telemetry"] = _public_telemetry(job.get("_telemetry"))
        coverage = self._regional_progress(job) or job.get("coverage_progress")
        if isinstance(coverage, dict):
            output["coverage_progress"] = coverage
        output["can_resume"] = bool(job.get("can_resume")) and self._can_resume(job)
        output["resume_unavailable_code"] = (
            None if output["can_resume"] else self._resume_unavailable_code(job)
        )
        output["can_reconstruct"] = self._can_reconstruct(job)
        output["can_cleanup"] = bool(self._cleanup_destinations(job))
        output["can_return"] = self._has_return_reference(job) and job.get(
            "physical_state"
        ) not in {"restored", "ownership_lost"}
        candidate_reason = job.get("candidate_reason")
        if candidate_reason in _CANDIDATE_REASONS:
            output["candidate_reason"] = candidate_reason
        messages = []
        codes = []
        issues = list(job.get("issues", []))
        if (
            job.get("status") not in _ACTIVE
            and self._resume_unavailable_code(job) == "continuous_resume_unavailable"
        ):
            issues.append("continuous_resume_unavailable")
        for issue in issues:
            code = issue.get("code") if isinstance(issue, dict) else issue
            if isinstance(code, str) and code in _ISSUE_MESSAGES:
                message = _ISSUE_MESSAGES[code]
            elif isinstance(code, str) and re.fullmatch(r"[a-z_]{3,80}", code):
                # Keep the original code in the private manifest; an unknown
                # diagnostic does not establish a cause to present to the user.
                message = "Não foi possível confirmar uma etapa da captura."
            else:
                continue
            if code not in codes:
                codes.append(code)
            if message not in messages:
                messages.append(message)
        output["issues"] = messages
        output["issue_codes"] = codes
        return output

    @staticmethod
    def _can_resume(job: dict[str, Any]) -> bool:
        return SourcePanoramaService._resume_unavailable_code(job) is None

    @staticmethod
    def _resume_unavailable_code(job: dict[str, Any]) -> str | None:
        if job.get("operation") == "verify_control":
            return "resume_unavailable"
        if job.get("status") not in _ACTIVE and any(
            (
                issue.get("code") if isinstance(issue, dict) else issue
            ) == "coverage_exceeds_capture_budget"
            for issue in job.get("issues", [])
        ):
            return "coverage_exceeds_capture_budget"
        checkpoint = job.get("_checkpoint")
        if not isinstance(checkpoint, dict):
            return "resume_unavailable"
        if job.get("_acquisition_policy") is not None or checkpoint.get("acquisition_policy") is not None:
            if job.get("_acquisition_policy") != checkpoint.get("acquisition_policy"):
                return "region_policy_incompatible"
            return region_resume_error(checkpoint)
        if (
            checkpoint.get("mode") == "absolute"
            and "plan" in checkpoint
            and not checkpoint.get("plan")
        ):
            return "absolute_grid_checkpoint_incompatible"
        preplan_error = _absolute_preplan_resume_error(checkpoint)
        if preplan_error is not None:
            return preplan_error
        fallback_record = checkpoint.get("absolute_grid_fallback")
        if (
            isinstance(fallback_record, dict)
            and fallback_record.get("state") in {"selected", "active", "complete"}
            and fallback_record.get("continuous_mode") not in {"velocity", "relative"}
        ):
            return "continuous_resume_unavailable"
        minimum_captures = 2
        if checkpoint.get("mode") == "continuous_fallback_pending":
            fallback = checkpoint.get("absolute_grid_fallback")
            active_seconds = checkpoint.get("active_seconds")
            if (
                not isinstance(fallback, dict)
                or fallback.get("state") not in {"selected", "active"}
                or fallback.get("reason") != "pilot_cycle_unconfirmed"
                or fallback.get("from") != "absolute"
                or fallback.get("to") != "continuous"
                or fallback.get("continuous_mode") not in {"velocity", "relative"}
                or fallback.get("absolute_plan_published") is not False
                or type(active_seconds) not in {int, float}
                or not math.isfinite(active_seconds)
                or not 0 <= active_seconds < _MAX_JOB_SECONDS
            ):
                return "continuous_resume_unavailable"
            minimum_captures = 1
        if checkpoint.get("mode") == "continuous":
            cursor = checkpoint.get("continuous_cursor")
            if (
                not isinstance(cursor, dict)
                or type(cursor.get("version")) is not int
                or cursor["version"] != 4
            ):
                return "continuous_resume_unavailable"
            active_seconds = checkpoint.get("active_seconds")
            anchor = cursor.get("anchor")
            checkpoint_captures = checkpoint.get("captures")
            reference_pending = cursor.get("stage") == "reference"
            if (
                type(active_seconds) not in {int, float}
                or not math.isfinite(active_seconds)
                or active_seconds < 0
                or not isinstance(cursor.get("recovery_attempts"), dict)
                or any(
                    not isinstance(destination, str)
                    or not destination
                    or type(attempts) is not int
                    or not 0 <= attempts <= 1
                    for destination, attempts in cursor["recovery_attempts"].items()
                )
                or (
                    not reference_pending
                    and (
                        not isinstance(anchor, dict)
                        or not isinstance(anchor.get("capture_id"), str)
                        or not anchor["capture_id"]
                        or not isinstance(anchor.get("path"), str)
                        or not anchor["path"]
                        or type(anchor.get("row")) is not int
                    )
                )
                or not isinstance(checkpoint_captures, list)
                or (
                    not reference_pending
                    and not any(
                        isinstance(capture, dict)
                        and capture.get("id") == anchor["capture_id"]
                        and capture.get("path") == anchor["path"]
                        and type(capture.get("row_index")) is int
                        and capture["row_index"] == anchor["row"]
                        and isinstance(capture.get("quality"), dict)
                        and capture["quality"].get("stable") is True
                        for capture in checkpoint_captures
                    )
                )
            ):
                return "continuous_resume_unavailable"
            if active_seconds >= _MAX_JOB_SECONDS:
                return "resume_unavailable"
            precondition_reobservation = cursor.get("precondition_reobservation")
            if precondition_reobservation is not None:
                if (
                    not isinstance(precondition_reobservation, dict)
                    or precondition_reobservation.get("version") != 1
                    or precondition_reobservation.get("state") != "consumed"
                    or not isinstance(precondition_reobservation.get("movement_id"), str)
                    or re.fullmatch(
                        r"[0-9a-f]{32}", precondition_reobservation["movement_id"]
                    )
                    is None
                    or any(
                        precondition_reobservation.get(key) != cursor.get(key)
                        for key in ("stage", "row", "direction", "branch")
                    )
                    or not isinstance(precondition_reobservation.get("intent"), dict)
                ):
                    return "continuous_resume_unavailable"
                return "relocalization_required"
            if not isinstance(cursor.get("stage"), str) or cursor["stage"] not in {
                "reference",
                "pan",
                "step",
                "return_reference",
            }:
                return "resume_unavailable"
            if reference_pending:
                if (
                    cursor.get("row") != 0
                    or cursor.get("direction") not in {-1, 1}
                    or cursor.get("branch") not in {-1, 1}
                    or not isinstance(cursor.get("bands"), dict)
                    or "0" not in cursor["bands"]
                ):
                    return "continuous_resume_unavailable"
                minimum_captures = 1
            else:
                minimum_captures = 2
            recovery_levels = checkpoint.get("seek_recovery_level", {})
            if (
                not isinstance(recovery_levels, dict)
                or any(
                    axis not in {"pan", "tilt"}
                    or type(level) is not int
                    or not 0 <= level <= 2
                    for axis, level in recovery_levels.items()
                )
            ):
                return "continuous_resume_unavailable"
            seek = cursor.get("seek")
            if seek is not None and (
                not isinstance(seek, dict)
                or seek.get("axis") != "pan"
                or seek.get("direction") not in {-1, 1}
                or type(seek.get("row")) is not int
                or type(seek.get("steps")) is not int
                or not 0 <= seek["steps"] <= 64
                or type(seek.get("command_failures", 0)) is not int
                or not 0 <= seek.get("command_failures", 0) <= 1
                or type(seek.get("origin_uncertain_since_anchor", False)) is not bool
                or not isinstance(seek.get("subdivided_steps", []), list)
                or any(
                    type(step) is not int or not 1 <= step <= seek.get("steps", -1)
                    for step in seek.get("subdivided_steps", [])
                )
                or len(set(seek.get("subdivided_steps", [])))
                != len(seek.get("subdivided_steps", []))
                or cursor.get("stage") != "pan"
                or seek.get("row") != cursor.get("row")
                or seek.get("direction") != cursor.get("direction")
                or (
                    "duration" in seek
                    and (
                        type(seek["duration"]) not in {int, float}
                        or not math.isfinite(seek["duration"])
                        or not 0.12 <= seek["duration"] <= 2.0
                    )
                )
            ):
                return "continuous_resume_unavailable"
            vertical_seek = cursor.get("vertical_seek")
            if vertical_seek is not None and (
                not isinstance(vertical_seek, dict)
                or vertical_seek.get("branch") not in {-1, 1}
                or type(vertical_seek.get("row")) is not int
                or type(vertical_seek.get("steps")) is not int
                or not 0 <= vertical_seek["steps"] <= 8
                or type(vertical_seek.get("duration")) not in {int, float}
                or not math.isfinite(vertical_seek["duration"])
                or not 0.12 <= vertical_seek["duration"] <= 2.0
                or type(vertical_seek.get("command_failures", 0)) is not int
                or not 0 <= vertical_seek.get("command_failures", 0) <= 1
                or type(vertical_seek.get("origin_uncertain_since_anchor", False)) is not bool
                or not isinstance(vertical_seek.get("subdivided_steps", []), list)
                or any(
                    type(step) is not int
                    or not 1 <= step <= vertical_seek.get("steps", -1)
                    for step in vertical_seek.get("subdivided_steps", [])
                )
                or len(set(vertical_seek.get("subdivided_steps", [])))
                != len(vertical_seek.get("subdivided_steps", []))
                or cursor.get("stage") != "step"
                or vertical_seek.get("row") != cursor.get("row")
                or vertical_seek.get("branch") != cursor.get("branch")
            ):
                return "continuous_resume_unavailable"
            transition = cursor.get("transition")
            if transition is not None and (
                not isinstance(transition, dict)
                or transition.get("state")
                not in {
                    "pending",
                    "accepted",
                    "confirmed",
                    "no_effect_verified",
                    "not_issued",
                    "relocalized",
                    "effect_or_state_ambiguous",
                    "recovery_exhausted",
                }
            ):
                return "continuous_resume_unavailable"
            if isinstance(transition, dict) and transition.get("state") in {
                "effect_or_state_ambiguous",
                "recovery_exhausted",
            }:
                return "continuous_resume_unavailable"
            try:
                from .panorama_scan import (
                    _connection_recovery,
                    _movement_attempt_id,
                    _pending_seek_intent,
                    _visual_band_return,
                )

                connection_recovery = _connection_recovery(
                    cursor, captures=checkpoint_captures
                )
                visual_band_return = _visual_band_return(
                    cursor, captures=checkpoint_captures
                )
                if visual_band_return is not None and visual_band_return["state"] == "correcting":
                    return "relocalization_required"
                if (
                    isinstance(transition, dict)
                    and "movement_id" in transition
                    and _movement_attempt_id(transition.get("movement_id")) is None
                ):
                    return "continuous_resume_unavailable"
                pending_seek = _pending_seek_intent(cursor)
            except (ImportError, ValueError):
                return "continuous_resume_unavailable"
            if pending_seek is not None:
                pending_state = cursor[pending_seek["state_key"]]
                if (
                    pending_state.get("command_failures", 0) >= 1
                    or recovery_levels.get(pending_seek["axis"], 0) >= 2
                    or pending_seek["duration"] <= 0.12
                ):
                    return "continuous_resume_unavailable"
            if (
                connection_recovery is not None
                and connection_recovery.get("state") == "failed"
            ):
                # The terminal record is safe to archive, but runtime seek
                # remains blocked until _recover performs that archive. A
                # locally matching frame does not complete this state change.
                return "relocalization_required"
            finished = cursor.get("finished_branches", [])
            if (
                cursor["stage"] == "step"
                and isinstance(finished, list)
                and all(branch in finished for branch in (-1, 1))
            ):
                return "resume_unavailable"
            if reference_pending:
                captures = job.get("_captures") or checkpoint.get("captures", [])
                if (
                    not isinstance(captures, list)
                    or not minimum_captures <= len(captures) < _MAX_CAPTURES
                ):
                    return "resume_unavailable"
                return None
            local_anchor = (
                cursor.get("resume_anchor_verified") is True
                and job.get("physical_state") == "stopped"
            )
            # A pending movement is not a position to replay. A structurally
            # valid working destination permits a fresh observation on resume;
            # an actual failed observation must not offer the same attempt again.
            if cursor.get("relocalization_failed") is True and not local_anchor:
                return "relocalization_required"
            destination = cursor.get("reference_destination")
            if destination is None:
                if not local_anchor and visual_band_return is None:
                    return "relocalization_required"
            elif (
                not isinstance(destination, dict)
                or not isinstance(destination.get("kind"), str)
                or destination.get("kind") not in {"absolute", "preset"}
                or destination.get("role") != "work"
                or not isinstance(destination.get("binding"), dict)
                or not destination["binding"]
                or not isinstance(destination.get("capture_id"), str)
                or not destination["capture_id"]
                or not isinstance(destination.get("path"), str)
                or not destination["path"]
                or destination["path"] != cursor.get("reference_path")
                or not any(
                    isinstance(capture, dict)
                    and capture.get("id") == destination["capture_id"]
                    and capture.get("path") == destination["path"]
                    and type(capture.get("row_index")) is int
                    and capture["row_index"] == 0
                    and isinstance(capture.get("quality"), dict)
                    and capture["quality"].get("stable") is True
                    for capture in checkpoint_captures
                )
            ):
                return "relocalization_required"
            elif destination["kind"] == "preset":
                if not all(
                    isinstance(destination.get(key), str) and destination[key]
                    for key in ("preset_token", "preset_name", "owner_id")
                ):
                    return "relocalization_required"
            elif any(
                type(destination.get(axis)) not in {int, float}
                or not math.isfinite(destination[axis])
                for axis in ("pan", "tilt")
            ):
                return "relocalization_required"
        captures = job.get("_captures") or checkpoint.get("captures", [])
        if (
            not isinstance(captures, list)
            or not minimum_captures <= len(captures) < _MAX_CAPTURES
        ):
            return "resume_unavailable"
        return None

    @staticmethod
    def _can_reconstruct(job: dict[str, Any]) -> bool:
        checkpoint = job.get("_checkpoint") or {}
        captures = job.get("_captures") or checkpoint.get("captures", [])
        return (
            job.get("status") not in _ACTIVE
            and isinstance(captures, list)
            and 2 <= len(captures) <= _MAX_CAPTURES
        )

    @staticmethod
    def _has_return_reference(job: dict[str, Any]) -> bool:
        checkpoint = job.get("_checkpoint")
        return (
            isinstance(checkpoint, dict)
            and not job.get("return_closure")
            and isinstance(checkpoint.get("return"), dict)
            and bool(checkpoint["return"])
            and bool(checkpoint.get("initial_path"))
        )

    @staticmethod
    def _cleanup_identity(destination: Any) -> str | None:
        if not isinstance(destination, dict):
            return None
        return json.dumps(
            {key: destination.get(key) for key in ("owner_id", "role", "preset_name", "binding")},
            sort_keys=True,
        )

    @staticmethod
    def _cleanup_destinations(job: dict[str, Any]) -> list[dict[str, Any]]:
        if job.get("status") in _ACTIVE or (
            job.get("can_resume") and SourcePanoramaService._can_resume(job)
        ):
            return []
        checkpoint = job.get("_checkpoint")
        if not isinstance(checkpoint, dict):
            return []
        pending = checkpoint.get("pending_returns")
        if not isinstance(pending, list):
            pending = []
        cursor = checkpoint.get("continuous_cursor")
        destinations = [
            checkpoint.get("return"),
            cursor.get("reference_destination") if isinstance(cursor, dict) else None,
            *pending,
        ]
        protect_original = (
            bool(checkpoint.get("return")) and job.get("physical_state") != "restored"
            and not job.get("return_closure")
        )
        candidates = {}
        for destination in destinations:
            if (
                isinstance(destination, dict)
                and destination.get("kind") in ("preset", "pending_preset")
                and destination.get("role") in ("original", "work")
                and destination.get("owner_id") == job.get("id")
                and isinstance(destination.get("binding"), dict)
                and not (protect_original and destination.get("role") == "original")
            ):
                # Prefer the confirmed canonical token over its earlier creation
                # intent; both records describe the same immutable job/role.
                candidates.setdefault(
                    SourcePanoramaService._cleanup_identity(destination), destination
                )
        return list(candidates.values())

    def _authorize(
        self, request: Request, camera_id: str, *, control: bool = False, write: bool = False
    ) -> None:
        self.authorize(
            request,
            action="core:camera:read",
            resource_type="core:camera",
            resource_selector=camera_id,
        )
        if control:
            self.authorize(
                request,
                action="core:camera:control",
                resource_type="core:camera",
                resource_selector=camera_id,
            )
        if write:
            self.authorize(request, action="core:settings:write")

    async def _context(
        self, camera_id: str, source_id: str, request: Request | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        settings = (
            await self.read_settings(request)
            if request is not None
            else (await self.store.get_settings()).extensions.get(_EXTENSION, {})
        )
        camera = get_camera_device(settings, camera_id=camera_id)
        source = get_camera_source(camera, source_id=source_id, enabled_only=True)
        if camera is None or source is None:
            raise _error("unknown_source", "Esta transmissão não está disponível.", 404)
        return settings, camera, source

    @staticmethod
    def _pointer(source: dict[str, Any]) -> dict[str, Any]:
        metadata = source.get("metadata")
        pointer = metadata.get("panorama") if isinstance(metadata, dict) else None
        return copy.deepcopy(pointer) if isinstance(pointer, dict) else {}

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id) if _IDENTIFIER.fullmatch(job_id) else None
        if job is None:
            raise _error("unknown_job", "Esta captura não foi encontrada.", 404)
        return job

    def _artifact(self, artifact_id: str) -> dict[str, Any]:
        if not _IDENTIFIER.fullmatch(artifact_id):
            raise _error("unknown_artifact", "Esta panorâmica não foi encontrada.", 404)
        artifact = self._read(self.root / "artifacts" / artifact_id / "artifact.json")
        if not artifact or artifact.get("id") != artifact_id:
            raise _error("unknown_artifact", "Esta panorâmica não foi encontrada.", 404)
        return artifact

    @staticmethod
    def _referenced_artifacts(settings: dict[str, Any], camera_id: str) -> set[str]:
        protected: set[str] = set()
        for camera in settings.get("devices", []):
            if not isinstance(camera, dict) or str(camera.get("id")) != camera_id:
                continue
            for source in camera.get("sources", []):
                if not isinstance(source, dict):
                    continue
                metadata = source.get("metadata")
                pointer = metadata.get("panorama") if isinstance(metadata, dict) else None
                if not isinstance(pointer, dict):
                    continue
                for name in ("active", "previous", "candidate"):
                    reference = pointer.get(name)
                    artifact_id = (
                        str(reference.get("artifact_id") or "")
                        if isinstance(reference, dict)
                        else ""
                    )
                    if _IDENTIFIER.fullmatch(artifact_id):
                        protected.add(artifact_id)
        return protected

    def _supersede_camera_jobs(self, camera_id: str, successor_id: str) -> None:
        now = time.time()
        for job in self.jobs.values():
            if job.get("camera_id") != camera_id or job.get("status") in _ACTIVE:
                continue
            checkpoint = job.get("_checkpoint")
            if isinstance(checkpoint, dict):
                checkpoint["return"] = None
                checkpoint["pending_returns"] = []
                cursor = checkpoint.get("continuous_cursor")
                if isinstance(cursor, dict):
                    cursor.pop("reference_destination", None)
            job.update(
                can_resume=False,
                _superseded_at=now,
                _superseded_by=successor_id,
            )
            self._save(job)

    def _prune_superseded_camera_data(
        self, settings: dict[str, Any], camera_id: str
    ) -> None:
        camera_jobs = [
            job
            for job in self.jobs.values()
            if job.get("camera_id") == camera_id and job.get("status") not in _ACTIVE
        ]
        latest_by_source: dict[str, dict[str, Any]] = {}
        for job in camera_jobs:
            source_id = str(job.get("source_id") or "")
            current = latest_by_source.get(source_id)
            created_at = job.get("created_at")
            current_created_at = current.get("created_at") if current else None
            if current is None or (
                isinstance(created_at, (int, float))
                and not isinstance(created_at, bool)
                and (
                    not isinstance(current_created_at, (int, float))
                    or isinstance(current_created_at, bool)
                    or created_at > current_created_at
                )
            ):
                latest_by_source[source_id] = job
        protected_job_ids = {job["id"] for job in latest_by_source.values()}
        protected_artifact_ids = self._referenced_artifacts(settings, camera_id)
        for job in latest_by_source.values():
            artifact_id = str(job.get("artifact_id") or "")
            if _IDENTIFIER.fullmatch(artifact_id):
                protected_artifact_ids.add(artifact_id)

        for job in camera_jobs:
            if job["id"] in protected_job_ids:
                continue
            try:
                shutil.rmtree(self._job_directory(job))
            except FileNotFoundError:
                pass
            except OSError:
                artifact_id = str(job.get("artifact_id") or "")
                if _IDENTIFIER.fullmatch(artifact_id):
                    protected_artifact_ids.add(artifact_id)
                continue
            self.jobs.pop(job["id"], None)

        artifacts_root = self.root / "artifacts"
        for directory in artifacts_root.iterdir() if artifacts_root.is_dir() else ():
            if not directory.is_dir() or not _IDENTIFIER.fullmatch(directory.name):
                continue
            artifact = self._read(directory / "artifact.json")
            if (
                not artifact
                or artifact.get("camera_id") != camera_id
                or directory.name in protected_artifact_ids
            ):
                continue
            try:
                shutil.rmtree(directory)
            except OSError:
                continue

    async def _reconcile_before_capture(
        self,
        *,
        settings: dict[str, Any],
        camera_id: str,
        source_id: str,
        successor_id: str,
    ) -> None:
        if self.camera_factory is None:
            raise _error("capture_unavailable", "O serviço da câmera não está disponível.", 503)
        adapter = self.camera_factory(
            services=self.services,
            camera_id=camera_id,
            source_id=source_id,
            settings=settings,
            job_id=successor_id,
            output_dir=self.root / "jobs" / successor_id,
        )
        if inspect.isawaitable(adapter):
            adapter = await adapter
        try:
            remove_managed_returns = getattr(adapter, "remove_managed_returns", None)
            if not callable(remove_managed_returns):
                raise _error(
                    "capture_unavailable", "O serviço da câmera não está disponível.", 503
                )
            await remove_managed_returns()
        except HTTPException:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "return_cleanup_unconfirmed")
            if not isinstance(code, str) or code not in _ISSUE_MESSAGES:
                code = "return_cleanup_unconfirmed"
            raise _error(code, _ISSUE_MESSAGES[code], 503) from None
        finally:
            await adapter.close()
        self._supersede_camera_jobs(camera_id, successor_id)
        # Reconcile temporary control references, not the photographs and
        # geometry that remain reusable. Storage admission must fail rather
        # than silently delete historical acquisitions to admit a new one.

    def _public_artifact(
        self,
        artifact: dict[str, Any],
        reference: dict[str, Any] | None = None,
        *,
        current_identity: str | None = None,
    ) -> dict[str, Any]:
        result = {key: value for key, value in artifact.items() if not key.startswith("_")}
        if reference and reference.get("artifact_id") == artifact["id"]:
            result.update(
                crop=reference.get("crop", result["crop"]),
                crop_revision=reference.get("crop_revision", 1),
            )
        result["stale"] = artifact.get("_identity") != current_identity
        result["stale_reason"] = (
            ("source_unavailable" if current_identity is None else "source_changed")
            if result["stale"]
            else None
        )
        return _safe(result)

    async def get_source(self, request: Request, camera_id: str, source_id: str) -> dict[str, Any]:
        self._authorize(request, camera_id)
        _, camera, source = await self._context(camera_id, source_id, request)
        current_identity = _identity(camera, source)
        pointer = self._pointer(source)
        output: dict[str, Any] = {
            "camera_id": camera_id,
            "source_id": source_id,
            "active": None,
            "previous": None,
            "candidate": None,
            "replacement_pending": False,
            "job": None,
        }
        for name in ("active", "previous", "candidate"):
            reference = pointer.get(name)
            if isinstance(reference, dict) and reference.get("artifact_id"):
                try:
                    artifact = self._artifact(str(reference["artifact_id"]))
                    if artifact["camera_id"] == camera_id and artifact["source_id"] == source_id:
                        output[name] = self._public_artifact(
                            artifact, reference, current_identity=current_identity
                        )
                except HTTPException:
                    pass  # A missing historical file must not prevent recovery.
        candidates = [
            job
            for job in self.jobs.values()
            if job["camera_id"] == camera_id and job["source_id"] == source_id
        ]
        if candidates:
            latest = max(candidates, key=lambda job: job["created_at"])
            output["job"] = self._public_job(latest)
            if (
                latest.get("artifact_id")
                and latest.get("status") in {"ready", "partial"}
                and output["candidate"] is None
            ):
                try:
                    artifact = self._artifact(latest["artifact_id"])
                    if not output["active"] or output["active"]["id"] != artifact["id"]:
                        output["candidate"] = self._public_artifact(
                            artifact, current_identity=current_identity
                        )
                except HTTPException:
                    pass
        output["replacement_pending"] = self._automatic_replacement_eligible(
            pointer,
            camera_id=camera_id,
            source_id=source_id,
            current_identity=current_identity,
        )
        return output

    def _automatic_replacement_eligible(
        self,
        pointer: dict[str, Any],
        *,
        camera_id: str,
        source_id: str,
        current_identity: str,
    ) -> bool:
        candidate = pointer.get("candidate")
        artifact_id = candidate.get("artifact_id") if isinstance(candidate, dict) else None
        if not isinstance(artifact_id, str) or not _IDENTIFIER.fullmatch(artifact_id):
            return False
        try:
            artifact = self._artifact(artifact_id)
        except HTTPException:
            return False
        job = self.jobs.get(str(artifact.get("_job_id") or ""))
        return bool(
            job
            and (job.get("_acquisition_policy") or {}).get("version") == 4
            and artifact.get("camera_id") == camera_id
            and artifact.get("source_id") == source_id
            and artifact.get("_identity") == current_identity
            and artifact.get("status") in {"ready", "partial"}
            and _artifact_quality_approved(artifact)
            and _artifact_files_are_readable(artifact, self.root / "artifacts")
        )

    async def _promote_legacy_regional_candidate(
        self,
        camera_id: str,
        source_id: str,
        pointer: dict[str, Any],
        *,
        current_identity: str,
    ) -> dict[str, Any]:
        """Finish the old review state for a successful regional capture.

        Policy v4 originally kept every result as a candidate, even when the
        reconstruction was approved.  The product contract now treats capture
        as a replacement operation: a usable result becomes active and the old
        active result remains recoverable as ``previous``.
        """
        candidate = pointer.get("candidate")
        artifact_id = candidate.get("artifact_id") if isinstance(candidate, dict) else None
        if (
            not isinstance(candidate, dict)
            or not isinstance(artifact_id, str)
            or not self._automatic_replacement_eligible(
                pointer,
                camera_id=camera_id,
                source_id=source_id,
                current_identity=current_identity,
            )
        ):
            return pointer
        replacement = {
            "revision": int(pointer.get("revision", 0)) + 1,
            "active": copy.deepcopy(candidate),
            "previous": copy.deepcopy(pointer.get("active")),
        }
        await self._update_pointer(
            camera_id,
            source_id,
            pointer,
            replacement,
            identity=current_identity,
        )
        return replacement

    async def finalize_replacement(
        self,
        request: Request,
        camera_id: str,
        source_id: str,
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        _, camera, source = await self._context(camera_id, source_id, request)
        pointer = self._pointer(source)
        await self._promote_legacy_regional_candidate(
            camera_id,
            source_id,
            pointer,
            current_identity=_identity(camera, source),
        )
        return await self.get_source(request, camera_id, source_id)

    @staticmethod
    def _bytes(directory: Path) -> int:
        return sum(
            path.stat().st_size
            for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def _reserve(self, *, excluding: str | None = None) -> None:
        used = self._bytes(self.root)
        remaining = sum(
            max(0, self.max_job_bytes - self._bytes(self._job_directory(job)))
            for job in self.jobs.values()
            if job["status"] in _ACTIVE
            and job["id"] != excluding
            and not job.get("_processing_only")
        )
        if used + remaining + self.max_job_bytes > self.max_global_bytes:
            raise _error("panorama_storage_full", "O espaço reservado para panorâmicas está cheio.")
        if (
            shutil.disk_usage(self.root).free
            < self.minimum_free_bytes + remaining + self.max_job_bytes
        ):
            raise _error(
                "insufficient_disk_space", "Não há espaço livre suficiente para iniciar a captura."
            )

    def _admit(
        self, camera_id: str, *, excluding: str | None = None, reserve_storage: bool = True
    ) -> None:
        jobs = [
            job for job in self.jobs.values() if job["status"] in _ACTIVE and job["id"] != excluding
        ]
        if any(job["camera_id"] == camera_id for job in jobs):
            raise _error("camera_busy", "Esta câmera já está produzindo uma panorâmica.")
        waiting = sum(
            job["status"] == "queued" or job["phase"] == "queued_processing" for job in jobs
        )
        if waiting >= 2 or len(jobs) >= 4:
            raise _error(
                "panorama_queue_full",
                "A fila de panorâmicas está cheia. Aguarde uma captura terminar.",
            )
        if reserve_storage:
            self._reserve(excluding=excluding)

    def _check_existing_storage(self, job: dict[str, Any]) -> None:
        if self._bytes(self._job_directory(job)) > self.max_job_bytes:
            raise _error("job_storage_exceeded", "Este trabalho atingiu o limite de armazenamento.")
        if self._bytes(self.root) > self.max_global_bytes:
            raise _error("panorama_storage_full", "O espaço reservado para panorâmicas está cheio.")
        if shutil.disk_usage(self.root).free < self.minimum_free_bytes:
            raise _error(
                "insufficient_disk_space", "Não há espaço livre suficiente para repetir a montagem."
            )

    def _admit_processing(self, job: dict[str, Any]) -> None:
        active = [
            item
            for item in self.jobs.values()
            if item["id"] != job["id"] and item["status"] in _ACTIVE
        ]
        waiting = sum(
            item["status"] == "queued" or item["phase"] == "queued_processing" for item in active
        )
        if len(active) >= 4 or waiting >= 2:
            raise _error(
                "panorama_queue_full",
                "A fila de panorâmicas está cheia. Aguarde um trabalho terminar.",
            )
        # Originals already occupy their accounted disk space. Reprocessing does
        # not reserve the budget of a second acquisition or own the camera head.
        self._check_existing_storage(job)

    async def create(
        self, request: Request, camera_id: str, source_id: str, body: CreateSourcePanorama
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, control=True, write=True)
        settings, camera, source = await self._context(camera_id, source_id, request)
        async with self.lock:
            if self.closed:
                raise _error("service_stopping", "O serviço está sendo encerrado.", 503)
            for job in self.jobs.values():
                if (job["camera_id"], job["source_id"], job.get("_idempotency_key")) == (
                    camera_id,
                    source_id,
                    body.idempotency_key,
                ):
                    if job.get("operation", "capture") != body.operation:
                        raise _error("idempotency_conflict", "Esta solicitação já iniciou outra operação.")
                    return {"job": self._public_job(job)}
            job_id = uuid.uuid4().hex
            if body.operation == "capture":
                self._admit(camera_id, reserve_storage=False)
                await self._reconcile_before_capture(
                    settings=settings,
                    camera_id=camera_id,
                    source_id=source_id,
                    successor_id=job_id,
                )
                self._reserve()
            else:
                self._admit(camera_id)
            job = {
                "id": job_id,
                "camera_id": camera_id,
                "source_id": source_id,
                "status": "queued",
                "phase": "queued",
                "operation": body.operation,
                "captures_accepted": 0,
                "physical_state": "unknown",
                "can_resume": False,
                "created_at": time.time(),
                "issues": [],
                "_idempotency_key": body.idempotency_key,
                "_identity": _identity(camera, source),
                "_expected_pointer": self._pointer(source),
                "_checkpoint": None,
                "_captures": [],
            }
            if body.operation == "capture":
                if self.acquisition_policy is not None:
                    job.update(
                        capture_goal="initial_region",
                        _acquisition_policy=dict(self.acquisition_policy),
                    )
                else:
                    job["capture_goal"] = "reachable_domain"
            self._save(job)
            self.jobs[job["id"]] = job
            self.tasks[job["id"]] = asyncio.create_task(self._run(job))
            return {"job": self._public_job(job)}

    async def _progress(
        self, job: dict[str, Any], event: dict[str, Any], *, check_budget: bool = True
    ) -> None:
        if event.get("physical_state") in _PHYSICAL:
            job["physical_state"] = event["physical_state"]
        telemetry = _public_telemetry(event.get("telemetry"))
        if telemetry is not None:
            job["_telemetry"] = telemetry
        if check_budget and self._bytes(self._job_directory(job)) > self.max_job_bytes:
            raise _error("job_storage_exceeded", "A captura atingiu o limite de armazenamento.")
        if check_budget and self._bytes(self.root) > self.max_global_bytes:
            raise _error("panorama_storage_full", "O espaço reservado para panorâmicas está cheio.")
        if check_budget and shutil.disk_usage(self.root).free < self.minimum_free_bytes:
            raise _error(
                "insufficient_disk_space", "O armazenamento disponível terminou durante a captura."
            )
        # An adapter never supplies arbitrary public fields or a stream URL.
        for key in ("captures_accepted", "planned_captures", "estimated_remaining_seconds"):
            value = event.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                job[key] = value
        phase = event.get("phase", event.get("stage"))
        if isinstance(phase, str) and re.fullmatch(r"[a-z_]{1,60}", phase):
            job["phase"] = phase
            if phase == "returning":
                job["_return_attempted"] = True
        if event.get("status") in _ACTIVE and job["status"] != "stopping":
            job["status"] = event["status"]
        if isinstance(event.get("checkpoint"), dict):
            job["_checkpoint"] = _safe(event["checkpoint"])
        coverage = event.get("coverage_progress")
        if isinstance(coverage, dict):
            completed = coverage.get("bands_completed")
            current = coverage.get("current_band")
            if (
                type(completed) is int
                and 0 <= completed <= 256
                and (current is None or (type(current) is int and -256 <= current <= 256))
                and type(coverage.get("primary_complete")) is bool
            ):
                job["coverage_progress"] = {
                    key: coverage[key]
                    for key in ("primary_complete", "bands_completed", "current_band")
                }
                stage = coverage.get("stage")
                if isinstance(stage, str) and stage in {
                    "reference",
                    "pan",
                    "step",
                    "return_reference",
                    "done",
                }:
                    job["coverage_progress"]["stage"] = stage
                pending = coverage.get("regions_pending")
                if type(pending) is int and 0 <= pending <= 256:
                    job["coverage_progress"]["regions_pending"] = pending
                if type(coverage.get("continued_after_recovery")) is bool:
                    job["coverage_progress"]["continued_after_recovery"] = coverage[
                        "continued_after_recovery"
                    ]
                if (
                    job.get("capture_goal") == "initial_region"
                    and coverage.get("goal") == "initial_region"
                    and type(coverage.get("qualified_views")) is int
                    and 0 <= coverage["qualified_views"] <= 6
                    and coverage.get("required_views") == 6
                    and type(coverage.get("region_complete")) is bool
                ):
                    job["coverage_progress"].update({key: coverage[key] for key in (
                        "goal", "qualified_views", "required_views", "region_complete",
                    )})
                regional_progress = self._regional_progress(job)
                if regional_progress is not None:
                    # Derive bounded regional progress from the persisted policy
                    # and evidence, never from an adapter's six-view claim.
                    job["coverage_progress"] = regional_progress
        if isinstance(event.get("captures"), list):
            job["_captures"] = _safe(event["captures"])
            job["captures_accepted"] = len(job["_captures"])
        job["can_resume"] = self._can_resume(job)
        preview = event.get("preview_path")
        if preview:
            path = self._private_path(self._job_directory(job), str(preview))
            if path.suffix.lower() in {".jpg", ".jpeg"} and path.is_file():
                job["_preview"] = str(path.relative_to(self._job_directory(job)))
                job["preview_url"] = f"{_JOBS}/{job['id']}/preview"
        self._save(job)

    @staticmethod
    def _private_path(directory: Path, filename: str) -> Path:
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise _error("invalid_panorama_file", "Este arquivo não está disponível.", 404)
        return path

    async def _scan(self, job: dict[str, Any], *, return_only: bool = False) -> dict[str, Any]:
        settings, camera, source = await self._context(job["camera_id"], job["source_id"])
        if _identity(camera, source) != job["_identity"]:
            raise _error("source_changed", "A transmissão mudou. Inicie uma nova panorâmica.")
        if self.camera_factory is None:
            raise _error("capture_unavailable", "O serviço de captura não está disponível.", 503)
        adapter = self.camera_factory(
            services=self.services,
            camera_id=job["camera_id"],
            source_id=job["source_id"],
            settings=settings,
            job_id=job["id"],
            output_dir=self._job_directory(job),
        )
        if inspect.isawaitable(adapter):
            adapter = await adapter
        self.cameras[job["id"]] = adapter
        runner = self.scan_runner
        if runner is None:
            from .panorama_scan import run_panorama_scan

            runner = run_panorama_scan

        async def progress(event: dict[str, Any]) -> None:
            from .panorama_capture import PanoramaCaptureError

            existing_failure = job.get("_capture_storage_error")
            try:
                await self._progress(
                    job, event, check_budget=not return_only and not existing_failure
                )
            except (HTTPException, OSError) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else {}
                code = (
                    detail.get("code", "capture_write_failed")
                    if isinstance(detail, dict)
                    else "capture_write_failed"
                )
                job["_capture_storage_error"] = code
                if not existing_failure:
                    # The scanner has a typed error boundary that stops and then
                    # attempts a bounded return. Cleanup progress must remain
                    # possible after the first storage failure.
                    raise PanoramaCaptureError(code) from None

        try:
            return await runner(
                adapter,
                self._job_directory(job),
                checkpoint=job.get("_checkpoint"),
                progress=progress,
                cancelled=lambda: job["id"] in self.cancelled,
                return_only=return_only,
                **({"acquisition_policy": region_policy(job["_acquisition_policy"])}
                   if job.get("_acquisition_policy") is not None else {}),
                **({"verify_control": True}
                   if job.get("operation") == "verify_control" and not return_only else {}),
            )
        finally:
            self.cameras.pop(job["id"], None)
            # Scanner owns physical cleanup and return. close may only release resources.
            close = getattr(adapter, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    if job["physical_state"] not in {"ownership_lost", "restored", "stopped"}:
                        job["physical_state"] = "stop_unconfirmed"

    async def _reconstruct(
        self, job: dict[str, Any], captures: list[dict[str, Any]], output: Path
    ) -> dict[str, Any]:
        policy = region_policy(job.get("_acquisition_policy"))
        timeout = min(self.reconstruction_timeout_seconds, policy["reconstruction_seconds"]) if policy else self.reconstruction_timeout_seconds
        if self.reconstruct_runner is not None:
            # Injection is for deterministic test doubles, never the production OpenCV pipeline.
            result = self.reconstruct_runner(
                captures,
                output,
                progress=lambda event: None,
                cancelled=lambda: job["id"] in self.cancelled,
            )
            if inspect.isawaitable(result):
                try:
                    result = await asyncio.wait_for(
                        result, timeout=timeout
                    )
                except TimeoutError:
                    raise _error(
                        "reconstruction_timeout",
                        "A montagem atingiu o tempo máximo. As imagens foram guardadas.",
                    ) from None
            if isinstance(result, dict):
                result = dict(result)
                result["_quality_approved"] = _is_quality_approved(result.get("quality", {}))
            return result
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        cancellation = context.Event()
        process = context.Process(
            target=_reconstruction_process,
            args=(captures, str(output), send, cancellation),
            daemon=True,
        )
        started = time.monotonic()
        try:
            process.start()
        except BaseException:
            receive.close()
            send.close()
            process.close()
            raise
        send.close()
        try:
            while True:
                if job["id"] in self.cancelled:
                    cancellation.set()
                    raise asyncio.CancelledError
                if time.monotonic() - started >= timeout:
                    raise _error(
                        "reconstruction_timeout",
                        "A montagem atingiu o tempo máximo. As imagens foram guardadas.",
                    )
                if receive.poll():
                    try:
                        message = receive.recv()
                    except EOFError:
                        raise _error(
                            "reconstruction_failed", "Não foi possível montar a panorâmica."
                        ) from None
                    if "result" in message:
                        result = message["result"]
                        if isinstance(result, dict):
                            result["_quality_approved"] = message.get("quality_approved") is True
                        return result
                    if "error" in message:
                        raise _error(
                            message["error"],
                            "Não foi possível confirmar uma panorâmica com estas imagens.",
                        )
                    if "progress" in message:
                        await self._progress(job, message["progress"])
                elif not process.is_alive():
                    raise _error(
                        "reconstruction_failed",
                        "A montagem foi interrompida. As imagens foram guardadas.",
                    )
                await asyncio.sleep(0.05)
        finally:
            cancellation.set()
            await asyncio.to_thread(process.join, 0.5)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 2)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 2)
            process.close()
            receive.close()

    def _validated_captures(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        checkpoint = job.get("_checkpoint") or {}
        captures = copy.deepcopy(job.get("_captures") or checkpoint.get("captures", []))
        policy = region_policy(job.get("_acquisition_policy"))
        maximum = policy["maximum_captures"] if policy else _MAX_CAPTURES
        if not isinstance(captures, list) or not 2 <= len(captures) <= maximum:
            raise _error(
                "insufficient_accepted_images",
                "Não há imagens confirmadas suficientes para montar a panorâmica.",
            )
        input_bytes = 0
        frame_identities = set()
        for capture in captures:
            if not isinstance(capture, dict):
                raise _error(
                    "invalid_capture_file", "Uma imagem da captura não pôde ser confirmada."
                )
            if policy:
                instance, generation, sequence = (capture.get(key) for key in ("capture_instance", "generation", "sequence"))
                if (
                    capture.get("capture_schema_version") != 2
                    or not isinstance(instance, str) or not instance
                    or type(generation) is not int or generation < 0
                    or type(sequence) is not int or sequence < 0
                    or not isinstance(capture.get("quality"), dict)
                    or capture["quality"].get("stable") is not True
                    or not isinstance(capture.get("sha256"), str)
                    or (instance, generation, sequence) in frame_identities
                ):
                    raise _error("capture_identity_unverified", _ISSUE_MESSAGES["capture_identity_unverified"])
                frame_identities.add((instance, generation, sequence))
            path = self._private_path(self._job_directory(job), str(capture.get("path", "")))
            input_bytes += path.stat().st_size
            if policy and input_bytes > policy["maximum_input_bytes"]:
                raise _error("region_budget_exhausted", "A região inicial atingiu o limite de fotografias.")
            if (
                path.suffix.lower() not in {".jpg", ".jpeg", ".png"}
                or path.stat().st_size > 24 * 1024**2
            ):
                raise _error(
                    "invalid_capture_file", "Uma imagem da captura não pôde ser confirmada."
                )
            digest = capture.get("sha256")
            if digest is not None:
                if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                    raise _error(
                        "capture_integrity_changed",
                        "Não foi possível verificar a integridade de uma fotografia guardada.",
                    )
                with path.open("rb") as photograph:
                    actual = hashlib.file_digest(photograph, "sha256").hexdigest()
                if actual != digest.lower():
                    raise _error(
                        "capture_integrity_changed",
                        "Uma fotografia foi alterada desde a captura. Gere uma nova panorâmica.",
                    )
            capture["path"] = str(path)
        return captures

    @asynccontextmanager
    async def _queue_slot(self, job: dict[str, Any], semaphore: asyncio.Semaphore):
        policy = region_policy(job.get("_acquisition_policy"))
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=policy["queue_seconds"] if policy else None)
        except TimeoutError:
            code = "capture_queue_timeout" if semaphore is self.acquisition else "reconstruction_queue_timeout"
            raise _error(code, _ISSUE_MESSAGES[code]) from None
        try:
            yield
        finally:
            semaphore.release()

    @staticmethod
    def _scan_summary(job: dict[str, Any]) -> dict[str, Any]:
        saved = job.get("_scan_result")
        if isinstance(saved, dict):
            return copy.deepcopy(saved)
        checkpoint = job.get("_checkpoint") or {}
        # A legacy checkpoint can locate photographs, but its existence or a
        # previous renderer result does not certify the acquired camera domain.
        issues = copy.deepcopy(job.get("issues", []))
        issues.append({"code": "scan_completeness_unverified"})
        return {
            "complete": False,
            "coverage": {
                "kind": "unconfirmed_reachable_domain",
                "boundaries": _safe(checkpoint.get("boundaries", {})),
            },
            "physical_state": job.get("physical_state", "unknown"),
            "issues": issues,
        }

    async def _run(
        self, job: dict[str, Any], *, return_only: bool = False, process_only: bool = False
    ) -> None:
        own_task = asyncio.current_task()
        separate_outcomes = (job.get("_acquisition_policy") or {}).get("version") in {2, 3, 4}
        if return_only and separate_outcomes:
            job["_return_attempted"] = True
        try:
            if process_only:
                _, camera, source = await self._context(job["camera_id"], job["source_id"])
                if _identity(camera, source) != job["_identity"]:
                    raise _error("source_changed", "A transmissão mudou desde esta captura.")
                result = self._scan_summary(job)
                # CPU work cannot erase newer return/stop evidence recorded
                # after the acquisition receipt was saved.
                issues = _safe(job.get("issues", []) + result.get("issues", []))
                job["issues"] = list(
                    {json.dumps(item, sort_keys=True): item for item in issues}.values()
                )
                self._check_existing_storage(job)
            else:
                async with self._queue_slot(job, self.acquisition):
                    if job["id"] in self.cancelled:
                        raise asyncio.CancelledError
                    job.update(
                        status="returning" if return_only else "preparing",
                        phase="returning" if return_only else "preparing",
                    )
                    self._save(job)
                    result = await self._scan(job, return_only=return_only)
                    job["physical_state"] = result.get("physical_state", "unknown")
                    if job["physical_state"] not in _PHYSICAL:
                        job["physical_state"] = "unknown"
                    job["issues"] = _safe(result.get("issues", []))
                    job["_checkpoint"] = _safe(result.get("checkpoint", job.get("_checkpoint")))
                    captured = _safe(result.get("captures", job.get("_captures", [])))
                    job["_captures"], job["captures_accepted"] = captured, len(captured)
                    job["can_resume"] = self._can_resume(job)
                    if not return_only:
                        job["_scan_result"] = {
                            "complete": (
                                result.get("complete")
                                if type(result.get("complete")) is bool
                                else None
                            ),
                            "coverage": _safe(result.get("coverage", {})),
                            "physical_state": job["physical_state"],
                            "issues": copy.deepcopy(job["issues"]),
                        }
                    self._save(job)
                if return_only:
                    job.update(
                        status=job.pop("_before_return", "interrupted"), phase="return_complete"
                    )
                    return
                if job.get("operation") == "verify_control":
                    verification = (job.get("_checkpoint") or {}).get("control_verification", {})
                    verified = (
                        verification.get("status") == "verified"
                        and job["physical_state"] == "restored"
                        and bool(verification.get("checks"))
                        and all(check.get("status") == "verified" for check in verification["checks"])
                    )
                    job.update(
                        status="verified" if verified else "failed",
                        phase="control_verified" if verified else "control_unconfirmed",
                        can_resume=False,
                    )
                    if not verified:
                        job["error"] = {
                            "code": "visual_control_unverified",
                            "message": "Não foi possível confirmar os movimentos e o retorno desta câmera.",
                        }
                    return
            if job["id"] in self.cancelled:
                raise asyncio.CancelledError
            if job.get("_capture_storage_error") and not process_only:
                raise _error(
                    job["_capture_storage_error"],
                    "Não foi possível salvar toda a captura. As imagens disponíveis foram guardadas.",
                )
            captures = await asyncio.to_thread(self._validated_captures, job)
            if separate_outcomes:
                job["_reconstruction_status"] = "running"
            job.update(status="processing", phase="queued_processing")
            self._save(job)
            async with self._queue_slot(job, self.processing):
                if job["id"] in self.cancelled:
                    raise asyncio.CancelledError
                job["phase"] = "reconstructing"
                self._save(job)
                artifact_id = uuid.uuid4().hex
                output = self._job_directory(job) / f"reconstruction-{artifact_id}"
                output.mkdir(mode=0o700)
                reconstruction = await self._reconstruct(job, captures, output)
                if separate_outcomes:
                    job["_reconstruction_status"] = "ready" if reconstruction.get("_quality_approved") is True else "review"
                if job["id"] in self.cancelled:
                    raise asyncio.CancelledError
                artifact = await self._publish(job, result, reconstruction, output, artifact_id)
                job.update(
                    status=artifact["status"],
                    phase="complete",
                    artifact_id=artifact["id"],
                    can_resume=artifact["status"] == "partial" and self._can_resume(job),
                )
        except asyncio.CancelledError:
            if self.tasks.get(job["id"]) is not own_task:
                return
            processing_interrupted = process_only or job["phase"] in {
                "queued_processing",
                "reconstructing",
                "stopping_processing",
            }
            if separate_outcomes and processing_interrupted:
                job["_reconstruction_status"] = "interrupted"
            job.update(
                status="interrupted",
                phase="interrupted_processing" if processing_interrupted else "interrupted",
                can_resume=self._can_resume(job),
            )
        except Exception as exc:
            if self.tasks.get(job["id"]) is not own_task:
                return
            detail = (
                exc.detail
                if isinstance(exc, HTTPException) and isinstance(exc.detail, dict)
                else None
            )
            code = str(getattr(exc, "code", "panorama_failed"))
            if not re.fullmatch(r"[a-z_]{3,80}", code):
                code = "panorama_failed"
            if separate_outcomes and return_only:
                # A later failed physical return cannot revoke a published image.
                job.update(status=job.pop("_before_return", "interrupted"), phase="return_unconfirmed")
                job.setdefault("issues", []).append({"code": "return_failed", "reason": detail.get("code", code) if detail else code})
                return
            if separate_outcomes and job.get("_reconstruction_status") == "running":
                job["_reconstruction_status"] = "failed"
            job.update(
                status="failed",
                phase="failed",
                can_resume=self._can_resume(job),
                error=detail
                or {
                    "code": code,
                    "message": "Não foi possível concluir a panorâmica. As imagens aproveitadas foram guardadas.",
                },
            )
        finally:
            if self.tasks.get(job["id"]) is own_task:
                self.cancelled.discard(job["id"])
                self.tasks.pop(job["id"], None)
                try:
                    self._save(job)
                except OSError:
                    job.update(
                        status="failed",
                        phase="failed",
                        error={
                            "code": "storage_write_failed",
                            "message": "Não foi possível salvar o progresso da panorâmica.",
                        },
                    )

    async def _update_pointer(
        self,
        camera_id: str,
        source_id: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
        *,
        identity: str | None = None,
    ) -> None:
        def update(settings: dict[str, Any]) -> dict[str, Any]:
            # Locate raw records: normalizers return copies and cannot be mutated here.
            devices = settings.get("devices", [])
            camera = next(
                (
                    item
                    for item in devices
                    if isinstance(item, dict) and item.get("id") == camera_id
                ),
                None,
            )
            source = next(
                (
                    item
                    for item in (camera or {}).get("sources", [])
                    if isinstance(item, dict) and item.get("id") == source_id
                ),
                None,
            )
            if source is None:
                raise _error("source_changed", "A transmissão não está mais disponível.")
            normalized_camera = get_camera_device(settings, camera_id=camera_id)
            normalized_source = get_camera_source(
                normalized_camera, source_id=source_id, enabled_only=True
            )
            if normalized_source is None or (
                identity and _identity(normalized_camera, normalized_source) != identity
            ):
                raise _error("source_changed", "A transmissão mudou durante a captura.")
            if self._pointer(source) != expected:
                raise _error(
                    "panorama_revision_conflict",
                    "A panorâmica foi alterada em outra tela. Atualize para continuar.",
                )
            metadata = copy.deepcopy(source.get("metadata", {}))
            metadata["panorama"] = replacement
            source["metadata"] = metadata
            return settings

        # Do not let an approved reconstruction displace geometry while a
        # visual navigation operation is using it. This lock remains outside
        # ConfigStore, so camera I/O never holds the global settings lock.
        async with self.reference_coordinator.hold(camera_id, source_id):
            await self.store.update_extension_settings(_EXTENSION, update)

    async def _publish(
        self,
        job: dict[str, Any],
        scan: dict[str, Any],
        result: dict[str, Any],
        output: Path,
        artifact_id: str,
    ) -> dict[str, Any]:
        files = result.get("files", {})
        if (
            not isinstance(files, dict)
            or not {"panorama", "thumbnail", "coverage", "model", "report"} <= files.keys()
        ):
            raise _error(
                "invalid_reconstruction", "A montagem não produziu todos os arquivos esperados."
            )
        safe_files = {}
        for file_id, filename in files.items():
            if file_id not in _FILE_TYPES:
                continue
            path = self._private_path(output, str(filename))
            if path.suffix != _FILE_TYPES[file_id][1]:
                raise _error("invalid_reconstruction", "A montagem produziu um formato inesperado.")
            safe_files[file_id] = str(path.relative_to(output))
        region_status = None
        regional_version = (job.get("_acquisition_policy") or {}).get("version")
        if job.get("_acquisition_policy") is not None:
            required = required_region_captures(job.get("_checkpoint"))
            source_ids = result.get("source_ids")
            included = (bool(required) and isinstance(source_ids, list)
                        and all(identifier in source_ids for identifier in required))
            if not included and regional_version == 1:
                quality = dict(result.get("quality", {}))
                reasons = list(quality.get("reasons", []))
                if "required_region_missing" not in reasons:
                    reasons.append("required_region_missing")
                quality.update(status="review", reasons=reasons)
                result.update(quality=quality, status="partial", _quality_approved=False)
            region_status = (
                "ready" if included and result.get("_quality_approved") is True
                else "review" if included else "incomplete"
            )
            if regional_version == 4 and included:
                region_status = "review"  # Route and reconstruction are not visual coverage acceptance.
            result["region_status"] = region_status
            if regional_version in {2, 3, 4}:
                result["outcomes"] = self._regional_outcomes(job)
                result["acquisition_decision"] = region_progress(job["_checkpoint"])["decision"]
            self._atomic(self._private_path(output, safe_files["report"]), {
                key: value for key, value in result.items() if not key.startswith("_")
            })
        # Finishing a bounded region cannot certify reachable-domain coverage,
        # even if an injected/older acquisition receipt claims completeness.
        scan_complete = False if region_status is not None else scan.get("complete")
        presentation = _safe(result.get("model", {}).get("presentation", {}))
        presentation_verified = (
            isinstance(presentation, dict) and presentation.get("status") == "verified"
        )
        complete = (
            scan_complete is True
            and result.get("status") == "ready"
            and (
                job.get("capture_goal") != "reachable_domain"
                or presentation_verified
            )
        )
        if regional_version in {2, 3}:
            complete = region_status == "ready"
        coverage = _safe(result.get("coverage", {}))
        coverage["acquisition_complete"] = (
            scan_complete if type(scan_complete) is bool else None
        )
        coverage["acquisition"] = _safe(scan.get("coverage", {}))
        ratio = coverage.get(
            "pixel_ratio", coverage.get("ratio", coverage.get("coverage_ratio", 0))
        )
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(ratio)
            or not 0 <= ratio <= 1
        ):
            ratio = 0
        width, height = result.get("width"), result.get("height")
        if (
            type(width) is not int
            or type(height) is not int
            or not 1 <= width <= 8192
            or not 1 <= height <= 4096
        ):
            raise _error(
                "invalid_reconstruction", "As dimensões da panorâmica não puderam ser confirmadas."
            )
        raw_quality = result.get("quality", {})
        recorded_quality_approval = result.get("_quality_approved")
        quality_approved = (
            recorded_quality_approval
            if type(recorded_quality_approval) is bool
            else _is_quality_approved(raw_quality)
        )
        artifact = {
            "id": artifact_id,
            "revision": 1,
            "camera_id": job["camera_id"],
            "source_id": job["source_id"],
            "status": "ready" if complete else "partial",
            "created_at": time.time(),
            "width": width,
            "height": height,
            "image_url": f"{_ARTIFACTS}/{artifact_id}/files/panorama",
            "coverage_url": f"{_ARTIFACTS}/{artifact_id}/files/coverage",
            "thumbnail_url": f"{_ARTIFACTS}/{artifact_id}/files/thumbnail",
            "crop": {"u_start": 0, "u_width": 1, "v_start": 0, "v_height": 1},
            "crop_revision": 1,
            "coverage_ratio": ratio,
            "quality": _safe(raw_quality),
            "quality_approved": quality_approved,
            "presentation": presentation,
            "coverage": coverage,
            "algorithm_version": result.get("algorithm_version", "unknown"),
            "positioning_status": "not_validated",
            "_files": safe_files,
            "_job_id": job["id"],
            "_identity": job["_identity"],
            "_scan_coverage": _safe(scan.get("coverage", {})),
        }
        if region_status is not None:
            artifact.update(capture_goal="initial_region", region_status=region_status)
            if regional_version in {2, 3, 4}:
                artifact.update(outcomes=self._regional_outcomes(job), acquisition_decision=result["acquisition_decision"])
        elif job.get("capture_goal") == "reachable_domain":
            artifact["capture_goal"] = "reachable_domain"
        if self._bytes(self._job_directory(job)) > self.max_job_bytes:
            raise _error("job_storage_exceeded", "A montagem atingiu o limite de armazenamento.")
        if (
            self._bytes(self.root) > self.max_global_bytes
            or shutil.disk_usage(self.root).free < self.minimum_free_bytes
        ):
            raise _error(
                "insufficient_disk_space", "Não há espaço suficiente para publicar a panorâmica."
            )
        if not _artifact_files_are_readable_in_directory(artifact, output):
            raise _error(
                "invalid_reconstruction",
                "A montagem não produziu arquivos legíveis para publicação.",
            )
        self._atomic(output / "artifact.json", artifact)
        target = self.root / "artifacts" / artifact_id
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.replace(target)
        job["artifact_id"] = artifact_id
        expected = job["_expected_pointer"]
        current = expected.get("active")
        active_artifact = None
        current_id = current.get("artifact_id") if isinstance(current, dict) else None
        if isinstance(current_id, str) and _IDENTIFIER.fullmatch(current_id):
            try:
                active_artifact = self._artifact(current_id)
            except HTTPException:
                pass
        active_is_current = _active_artifact_is_current(
            active_artifact,
            reference=current,
            artifacts_root=self.root / "artifacts",
            camera_id=job["camera_id"],
            source_id=job["source_id"],
            current_identity=job["_identity"],
        )
        candidate_reason = _replacement_candidate_reason(
            artifact,
            active_artifact,
            active_is_current=active_is_current,
            policy_version=regional_version,
        )
        keep_as_candidate = candidate_reason is not None
        reference = {"artifact_id": artifact_id, "crop": artifact["crop"], "crop_revision": 1}
        if keep_as_candidate:
            job["candidate_reason"] = candidate_reason
            if active_is_current and candidate_reason != "quality_not_approved" and not any(
                (issue.get("code") if isinstance(issue, dict) else issue)
                == "active_panorama_preserved"
                for issue in job["issues"]
            ):
                job["issues"].append(
                    {"code": "active_panorama_preserved", "reason": candidate_reason}
                )
            replacement = copy.deepcopy(expected)
            replacement["revision"] = int(expected.get("revision", 0)) + 1
            replacement["candidate"] = reference
            await self._update_pointer(
                job["camera_id"],
                job["source_id"],
                expected,
                replacement,
                identity=job["_identity"],
            )
            return artifact
        job.pop("candidate_reason", None)
        replacement = {
            "revision": int(expected.get("revision", 0)) + 1,
            "active": reference,
            "previous": current if active_is_current else expected.get("previous"),
        }
        await self._update_pointer(
            job["camera_id"], job["source_id"], expected, replacement, identity=job["_identity"]
        )
        return artifact

    async def stop(self, request: Request, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        processing_only = bool(job.get("_processing_only")) or (
            job["status"] == "processing" and job_id not in self.cameras
        )
        self._authorize(
            request, job["camera_id"], control=not processing_only, write=processing_only
        )
        if job["status"] not in _ACTIVE:
            return {"job": self._public_job(job)}
        was_waiting = job["phase"] in {"queued", "queued_processing"}
        self.cancelled.add(job_id)
        job.update(
            status="stopping", phase="stopping_processing" if processing_only else "stopping"
        )
        self._save(job)
        adapter = self.cameras.get(job_id)
        if adapter is None and was_waiting and job_id in self.tasks:
            # A queued request has no physical owner. Finish its cancellation now,
            # instead of waiting for another camera's acquisition or CPU work.
            self.tasks.pop(job_id).cancel()
            self.cancelled.discard(job_id)
            job.update(
                status="interrupted",
                phase="interrupted_processing" if processing_only else "interrupted",
            )
            self._save(job)
        if adapter is not None:
            try:
                await asyncio.wait_for(adapter.stop(), timeout=8)
            except Exception as exc:
                job["physical_state"] = (
                    "ownership_lost"
                    if getattr(exc, "code", None)
                    in {"control_lost", "camera_configuration_changed"}
                    else "stop_unconfirmed"
                )
                self._save(job)
        return {"job": self._public_job(job)}

    async def resume(
        self, request: Request, job_id: str, *, return_only: bool = False
    ) -> dict[str, Any]:
        job = self._job(job_id)
        self._authorize(request, job["camera_id"], control=True, write=not return_only)
        async with self.lock:
            if job["status"] in _ACTIVE:
                return {"job": self._public_job(job)}
            if (return_only and not self._has_return_reference(job)) or (
                not return_only and (not job.get("can_resume") or not self._can_resume(job))
            ):
                code = self._resume_unavailable_code(job) if not return_only else None
                raise _error(
                    code or "resume_unavailable",
                    _ISSUE_MESSAGES[code]
                    if code == "continuous_resume_unavailable"
                    else "Não foi possível confirmar um ponto seguro para retomar esta captura.",
                )
            if job.get("physical_state") == "ownership_lost" and return_only:
                raise _error(
                    "ownership_lost",
                    "O controle da câmera mudou. O retorno desta captura não está disponível.",
                )
            _, camera, source = await self._context(job["camera_id"], job["source_id"], request)
            if _identity(camera, source) != job["_identity"]:
                raise _error("source_changed", "A transmissão mudou. Inicie uma nova panorâmica.")
            self._admit(job["camera_id"], excluding=job_id, reserve_storage=not return_only)
            job["_expected_pointer"] = self._pointer(source)
            job.pop("_capture_storage_error", None)
            job["_processing_only"] = False
            if return_only:
                job["_before_return"] = job["status"]
            self.cancelled.discard(job_id)
            job.update(status="queued", phase="queued", error=None)
            self._save(job)
            self.tasks[job_id] = asyncio.create_task(self._run(job, return_only=return_only))
            return {"job": self._public_job(job)}

    async def reconstruct_job(self, request: Request, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        self._authorize(request, job["camera_id"], write=True)
        async with self.lock:
            if self.closed:
                raise _error("service_stopping", "O serviço está sendo encerrado.", 503)
            if job["status"] in _ACTIVE:
                if job.get("_processing_only"):
                    return {"job": self._public_job(job)}
                raise _error("job_busy", "Este trabalho ainda está em andamento.")
            if not self._can_reconstruct(job):
                raise _error(
                    "reconstruction_unavailable",
                    "Não há fotografias suficientes para repetir a montagem.",
                )
            _, camera, source = await self._context(job["camera_id"], job["source_id"], request)
            if _identity(camera, source) != job["_identity"]:
                raise _error("source_changed", "A transmissão mudou desde esta captura.")
            await asyncio.to_thread(self._validated_captures, job)
            self._admit_processing(job)
            job["_expected_pointer"] = self._pointer(source)
            job["_scan_result"] = self._scan_summary(job)
            job["_processing_only"] = True
            self.cancelled.discard(job_id)
            job.update(status="processing", phase="queued_processing", error=None)
            self._save(job)
            self.tasks[job_id] = asyncio.create_task(self._run(job, process_only=True))
            return {"job": self._public_job(job)}

    async def cleanup_job(self, request: Request, job_id: str, *, abandon_return: bool = False) -> dict[str, Any]:
        job = self._job(job_id)
        self._authorize(request, job["camera_id"], control=True, write=True)
        async with self.lock:
            if self.closed:
                raise _error("service_stopping", "O serviço está sendo encerrado.", 503)
            if job["status"] in _ACTIVE or job_id in self.tasks:
                raise _error("job_busy", "Este trabalho ainda está em andamento.")
            self._admit(job["camera_id"], excluding=job_id, reserve_storage=False)
            if job.get("can_resume") and self._can_resume(job):
                raise _error(
                    "resume_available", "Estas referências ainda permitem continuar a captura."
                )
            settings, camera, source = await self._context(
                job["camera_id"], job["source_id"], request
            )
            if _identity(camera, source) != job["_identity"]:
                raise _error("source_changed", "A transmissão mudou desde esta captura.")
            if abandon_return and not job.get("return_closure"):
                checkpoint = job.get("_checkpoint") or {}
                job["return_closure"] = {
                    "decision": "deliberately_abandoned", "decided_at": time.time(),
                    "physical_state_at_closure": job.get("physical_state"),
                    "return_verified": checkpoint.get("restored_return_epoch") is not None,
                    "references": copy.deepcopy({key: checkpoint.get(key) for key in
                                                 ("return", "pending_returns", "initial_path")}),
                    "resources_released": False,
                }
                self._save(job)
            candidates = self._cleanup_destinations(job)
            if not candidates:
                return {"job": self._public_job(job), "cleanup_complete": True, "cleaned_count": 0}
            if self.camera_factory is None:
                raise _error("capture_unavailable", "O serviço da câmera não está disponível.", 503)
            adapter = self.camera_factory(
                services=self.services,
                camera_id=job["camera_id"],
                source_id=job["source_id"],
                settings=settings,
                job_id=job_id,
                output_dir=self._job_directory(job),
            )
            if inspect.isawaitable(adapter):
                adapter = await adapter
            cleaned = 0
            checkpoint = job["_checkpoint"]
            try:
                # Preset CRUD verifies binding/ownership itself. Acquiring a
                # manual movement lease could preempt and Stop another owner.
                try:
                    await adapter.discover()
                except Exception as exc:
                    code = getattr(exc, "code", "camera_discovery_failed")
                    if not isinstance(code, str) or code not in _ISSUE_MESSAGES:
                        code = "camera_discovery_failed"
                    raise _error(code, _ISSUE_MESSAGES[code], 503) from None
                for destination in candidates:
                    try:
                        await adapter.remove_return(destination)
                    except Exception as exc:
                        code = getattr(exc, "code", "return_cleanup_unconfirmed")
                        if not isinstance(code, str) or code not in _ISSUE_MESSAGES:
                            code = "return_cleanup_unconfirmed"
                        if not any(
                            (issue.get("code") if isinstance(issue, dict) else issue) == code
                            for issue in job.get("issues", [])
                        ):
                            job.setdefault("issues", []).append({"code": code})
                    else:
                        checkpoint["pending_returns"] = [
                            item
                            for item in checkpoint.get("pending_returns", [])
                            if self._cleanup_identity(item) != self._cleanup_identity(destination)
                        ]
                        token = destination.get("preset_token") or destination.get("resolved_token")
                        original = checkpoint.get("return")
                        if original == destination or (
                            token
                            and isinstance(original, dict)
                            and original.get("owner_id") == job_id
                            and original.get("preset_token") == token
                        ):
                            checkpoint["return"] = None
                        cursor = checkpoint.get("continuous_cursor")
                        if isinstance(cursor, dict):
                            reference = cursor.get("reference_destination")
                            if reference == destination or (
                                token
                                and isinstance(reference, dict)
                                and reference.get("owner_id") == job_id
                                and reference.get("preset_token") == token
                            ):
                                cursor.pop("reference_destination", None)
                        cleaned += 1
                    # Keep successful removals and unresolved ownership durable
                    # independently; retry reconciles inventory without movement.
                    self._save(job)
            finally:
                await adapter.close()
            remaining = bool(self._cleanup_destinations(job))
            if not remaining:
                if job.get("return_closure"):
                    job["return_closure"].update(resources_released=True, released_at=time.time())
                job["issues"] = [
                    issue
                    for issue in job.get("issues", [])
                    if (issue.get("code") if isinstance(issue, dict) else issue)
                    != "return_cleanup_unconfirmed"
                ]
                self._save(job)
            return {
                "job": self._public_job(job),
                "cleanup_complete": not remaining,
                "cleaned_count": cleaned,
            }

    async def save_crop(
        self, request: Request, camera_id: str, source_id: str, body: SavePanoramaCrop
    ) -> dict[str, Any]:
        self._authorize(request, camera_id, write=True)
        _, camera, source = await self._context(camera_id, source_id, request)
        artifact = self._artifact(body.artifact_id)
        if artifact["camera_id"] != camera_id or artifact["source_id"] != source_id:
            raise _error("unknown_artifact", "Esta panorâmica não pertence à transmissão.", 404)
        expected = self._pointer(source)
        selected = next(
            (
                key
                for key in ("active", "candidate")
                if isinstance(expected.get(key), dict)
                and expected[key].get("artifact_id") == body.artifact_id
            ),
            None,
        )
        reference = expected.get(selected) if selected else None
        if (
            not isinstance(reference, dict)
            or reference.get("crop_revision") != body.expected_revision
        ):
            raise _error(
                "panorama_revision_conflict",
                "A área útil mudou em outra tela. Atualize para continuar.",
            )
        replacement = copy.deepcopy(expected)
        reference = replacement[selected]
        reference.update(crop=body.crop.model_dump(), crop_revision=body.expected_revision + 1)
        replacement["revision"] = int(expected.get("revision", 0)) + 1
        await self._update_pointer(camera_id, source_id, expected, replacement)
        return {
            "artifact": self._public_artifact(
                artifact, reference, current_identity=_identity(camera, source)
            )
        }

    async def shutdown(self) -> None:
        self.closed = True
        pending = list(self.tasks.values())
        for job_id in list(self.tasks):
            self.cancelled.add(job_id)
            adapter = self.cameras.get(job_id)
            if adapter is not None:
                try:
                    await asyncio.wait_for(adapter.stop(), timeout=5)
                except Exception:
                    self.jobs[job_id]["physical_state"] = "stop_unconfirmed"
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=12)
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)


def register_source_panorama_routes(
    app: FastAPI, *, services: Any, authorize: Any, read_settings: Any, **options: Any
) -> SourcePanoramaService:
    service = SourcePanoramaService(
        app, services=services, authorize=authorize, read_settings=read_settings, **options
    )
    app.state.camera_source_panorama = service

    @app.get(_SOURCE)
    async def get_source(request: Request, camera_id: str, source_id: str) -> dict[str, Any]:
        return await service.get_source(request, camera_id, source_id)

    @app.post(_SOURCE + "/jobs")
    async def create(
        request: Request, camera_id: str, source_id: str, body: CreateSourcePanorama
    ) -> dict[str, Any]:
        return await service.create(request, camera_id, source_id, body)

    @app.post(_SOURCE + "/finalize")
    async def finalize_replacement(
        request: Request, camera_id: str, source_id: str
    ) -> dict[str, Any]:
        return await service.finalize_replacement(request, camera_id, source_id)

    @app.get(_JOBS + "/{job_id}")
    async def get_job(request: Request, job_id: str) -> dict[str, Any]:
        job = service._job(job_id)
        service._authorize(request, job["camera_id"])
        return {"job": service._public_job(job)}

    @app.post(_JOBS + "/{job_id}/stop")
    async def stop(request: Request, job_id: str) -> dict[str, Any]:
        return await service.stop(request, job_id)

    @app.post(_JOBS + "/{job_id}/resume")
    async def resume(request: Request, job_id: str) -> dict[str, Any]:
        return await service.resume(request, job_id)

    @app.post(_JOBS + "/{job_id}/return")
    async def return_to_start(request: Request, job_id: str) -> dict[str, Any]:
        return await service.resume(request, job_id, return_only=True)

    @app.post(_JOBS + "/{job_id}/reconstruct")
    async def reconstruct(request: Request, job_id: str) -> dict[str, Any]:
        return await service.reconstruct_job(request, job_id)

    @app.post(_JOBS + "/{job_id}/cleanup")
    async def cleanup(request: Request, job_id: str, abandon_return: bool = False) -> dict[str, Any]:
        return await service.cleanup_job(request, job_id, abandon_return=abandon_return)

    @app.patch(_SOURCE + "/crop")
    async def save_crop(
        request: Request, camera_id: str, source_id: str, body: SavePanoramaCrop
    ) -> dict[str, Any]:
        return await service.save_crop(request, camera_id, source_id, body)

    @app.get(_JOBS + "/{job_id}/preview")
    async def preview(request: Request, job_id: str) -> FileResponse:
        job = service._job(job_id)
        service._authorize(request, job["camera_id"])
        if not job.get("_preview"):
            raise _error("preview_unavailable", "A prévia ainda não está disponível.", 404)
        path = service._private_path(service._job_directory(job), job["_preview"])
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get(_ARTIFACTS + "/{artifact_id}")
    async def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
        artifact = service._artifact(artifact_id)
        service._authorize(request, artifact["camera_id"])
        reference = None
        current_identity = None
        try:
            _, camera, source = await service._context(
                artifact["camera_id"], artifact["source_id"], request
            )
            current_identity = _identity(camera, source)
            reference = next(
                (
                    value
                    for value in service._pointer(source).values()
                    if isinstance(value, dict) and value.get("artifact_id") == artifact_id
                ),
                None,
            )
        except HTTPException:
            pass
        return {
            "artifact": service._public_artifact(
                artifact, reference, current_identity=current_identity
            )
        }

    @app.get(_ARTIFACTS + "/{artifact_id}/files/{file_id}")
    async def artifact_file(request: Request, artifact_id: str, file_id: str) -> FileResponse:
        artifact = service._artifact(artifact_id)
        service._authorize(request, artifact["camera_id"])
        filename = artifact.get("_files", {}).get(file_id)
        if file_id not in _FILE_TYPES or not filename:
            raise _error("unknown_file", "Este arquivo não está disponível.", 404)
        path = service._private_path(service.root / "artifacts" / artifact_id, filename)
        return FileResponse(
            path,
            media_type=_FILE_TYPES[file_id][0],
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    return service
