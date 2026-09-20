from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import replace
import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines.execution import (
    PipelineRuntimeDependencies,
    TransformOperatorRuntime,
)
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.packet_contract import resolve_media_ts, resolve_source_id
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from ..registry import build_default_model_registry
from .contracts import IdentityEvidence, RecognitionDecision, RecognitionPolicy
from .extraction import IdentityExtractor, extract_in_process, validate_identity_frame
from .spatial import spatial_context

PRIVATE_EVIDENCE_ARTIFACT = "identity_evidence"
PRIVATE_DECISION_ARTIFACT = "identity_decision"


async def read_identity_decision(
    packet: Packet, services: Any, *, max_age_seconds: float = 10
) -> RecognitionDecision | None:
    """Contrato para consumidores internos; nunca copiar a decisão para campos públicos.

    A galeria é revalidada: um resultado em fila não pode vencer correção/exclusão.
    """
    artifact = packet.artifacts.get(PRIVATE_DECISION_ARTIFACT)
    if (
        artifact is None
        or not artifact.private
        or not isinstance(artifact.data, bytes)
        or len(artifact.data) > 65536
    ):
        return None
    if not 0 <= time.time() - packet.created_at <= max_age_seconds:
        return None
    try:
        decision = RecognitionDecision.model_validate_json(artifact.data)
        if decision.occurrence_id != occurrence_key(packet):
            return None
        store = await services.call("vision.identity.store")
        current = await asyncio.to_thread(
            store.validated_decision,
            decision.occurrence_id,
            decision_revision=decision.revision,
            observed_at=resolve_media_ts(packet),
            max_age_seconds=max_age_seconds,
        )
        if not 0 <= time.time() - packet.created_at <= max_age_seconds:
            return None
        return current
    except Exception:
        return None


class IdentityContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False


class IdentityEvidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    person_model_id: str = "opencv_sface_2021dec"
    face_detector_model_id: str = "opencv_yunet_2023mar"
    pet_model_id: str = "open_noodle_pet_small"
    input_artifact_name: str = "main"
    sample_interval_seconds: float = Field(default=2.0, ge=0.25, le=60)
    max_active_occurrences: int = Field(default=128, ge=1, le=2048)
    inference_timeout_seconds: float = Field(default=2.0, ge=0.05, le=30)


class RecognizeIdentityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    policies: list[RecognitionPolicy] = Field(default_factory=list, max_length=16)


def occurrence_key(packet: Packet) -> str | None:
    subject = packet.payload.get("subject")
    if not isinstance(subject, dict) or subject.get("type") != "event" or not subject.get("id"):
        return None
    correlation = packet.payload.get("correlation_id") or packet.metadata.get("correlation_id")
    if not correlation:
        return None
    parts = [
        resolve_source_id(packet) or packet.payload.get("source_stream_id") or packet.stream_id,
        subject["id"],
        str(correlation),
    ]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def _summary(packet: Packet, *, status: str, reason: str, occurrence_id: str | None) -> Packet:
    payload = dict(packet.payload)
    payload["recognition"] = {
        "schema_version": 1,
        "status": status,
        "reason": reason,
        "occurrence_id": occurrence_id,
        "provenance": "none",
    }
    return replace(packet, payload=payload)


class IdentityContextRuntime(TransformOperatorRuntime):
    """Attach a stable gallery link before an optional recognition branch.

    This operator performs no inference or storage. Existing fanout and queues
    determine backpressure; it does not promise isolation under overload.
    """

    def __init__(self, config: dict, dependencies: PipelineRuntimeDependencies):
        self.config = IdentityContextConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context: Any) -> list[Packet]:
        if not self.config.enabled:
            return [packet]
        key = occurrence_key(packet)
        species = (
            str(packet.payload.get("subject", {}).get("category") or "").lower() if key else ""
        )
        if key is None or species not in {"person", "cat", "dog"}:
            return [
                _summary(
                    packet,
                    status="unobservable",
                    reason="individual_occurrence_required",
                    occurrence_id=None,
                )
            ]
        return [
            _summary(packet, status="pending", reason="recognition_scheduled", occurrence_id=key)
        ]


class IdentityEvidenceRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict, dependencies: PipelineRuntimeDependencies):
        self.config = IdentityEvidenceConfig.model_validate(config)
        self.dependencies = dependencies
        self._extractors: dict[str, IdentityExtractor] = {}
        self._sampled: OrderedDict[str, float] = OrderedDict()

    def _extractor(self, species: str) -> IdentityExtractor:
        key = "person" if species == "person" else "pet"
        if key not in self._extractors:
            registry = self.dependencies.vision_model_registry or build_default_model_registry()
            model_id = self.config.person_model_id if key == "person" else self.config.pet_model_id
            manifest = registry.get_manifest(model_id)
            if manifest is None:
                raise ValueError("model_not_registered")
            detector = (
                registry.get_manifest(self.config.face_detector_model_id)
                if key == "person"
                else None
            )
            self._extractors[key] = IdentityExtractor(manifest, face_detector=detector)
        return self._extractors[key]

    async def process_packet(self, packet: Packet, context: Any) -> list[Packet]:
        if not self.config.enabled:
            return [packet]
        key = occurrence_key(packet)
        if key is None:
            return [
                _summary(
                    packet,
                    status="unobservable",
                    reason="individual_occurrence_required",
                    occurrence_id=None,
                )
            ]
        if packet.lifecycle == Lifecycle.CLOSE:
            self._sampled.pop(key, None)
            return [
                _summary(packet, status="pending", reason="occurrence_closed", occurrence_id=key)
            ]
        now = time.monotonic()
        if key in self._sampled and now - self._sampled[key] < self.config.sample_interval_seconds:
            return [_summary(packet, status="pending", reason="sampling", occurrence_id=key)]
        if key not in self._sampled and len(self._sampled) >= self.config.max_active_occurrences:
            stale = [item for item, sampled_at in self._sampled.items() if now - sampled_at > 120]
            for item in stale:
                self._sampled.pop(item, None)
            if len(self._sampled) >= self.config.max_active_occurrences:
                return [
                    _summary(
                        packet,
                        status="unavailable",
                        reason="occurrence_capacity",
                        occurrence_id=key,
                    )
                ]
        subject = packet.payload["subject"]
        species = str(subject.get("category") or "").lower()
        if species not in {"person", "cat", "dog"}:
            return [
                _summary(
                    packet, status="unobservable", reason="unsupported_species", occurrence_id=key
                )
            ]
        artifact = packet.artifacts.get(self.config.input_artifact_name)
        region = subject.get("bbox01")
        if (
            artifact is None
            or artifact.private
            or artifact.data is None
            or not isinstance(region, (list, tuple))
        ):
            return [
                _summary(
                    packet, status="unobservable", reason="subject_image_missing", occurrence_id=key
                )
            ]
        if packet.payload.get("frame_crop") or packet.payload.get("frame_warp"):
            return [
                _summary(
                    packet,
                    status="unobservable",
                    reason="original_frame_required",
                    occurrence_id=key,
                )
            ]
        image_error = validate_identity_frame(artifact.data)
        if image_error:
            return [_summary(packet, status="unobservable", reason=image_error, occurrence_id=key)]
        self._sampled[key] = now
        try:
            extractor = self._extractor(species)
            async with asyncio.timeout(self.config.inference_timeout_seconds):
                result = await context.run_blocking(
                    extract_in_process,
                    extractor.process_specification(),
                    artifact.data,
                    species=species,
                    occurrence_id=key,
                    source_id=resolve_source_id(packet)
                    or str(packet.payload.get("source_stream_id") or packet.stream_id),
                    camera_id=str(packet.payload.get("camera_id") or ""),
                    capture_id=str(
                        packet.payload.get("frame_ts", packet.parent_packet_id or packet.packet_id)
                    ),
                    observed_at=resolve_media_ts(packet),
                    region=tuple(region),
                    color_order=str(artifact.metadata.get("color_order") or "bgr"),
                    concurrency_key="vision.identity_evidence",
                    process_pool_key="vision.identity_evidence",
                    process_pool_deadline_seconds=30.0,
                )
        except TimeoutError:
            return [
                _summary(
                    packet, status="unavailable", reason="inference_timeout", occurrence_id=key
                )
            ]

        except Exception:
            return [
                _summary(
                    packet, status="unavailable", reason="inference_unavailable", occurrence_id=key
                )
            ]
        output = _summary(packet, status=result.status, reason=result.reason, occurrence_id=key)
        if result.evidence is not None:
            data = json.dumps(
                {
                    "evidence": result.evidence.model_copy(
                        update={"spatial_context": spatial_context(packet)}
                    ).model_dump(),
                    "crop": base64.b64encode(result.crop).decode() if result.crop else None,
                },
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            if len(data) > 2 * 1024 * 1024:
                return [
                    _summary(
                        packet,
                        status="unavailable",
                        reason="evidence_size_limit",
                        occurrence_id=key,
                    )
                ]
            output = output.with_artifact(
                Artifact(
                    name=PRIVATE_EVIDENCE_ARTIFACT,
                    data=data,
                    mime_type="application/vnd.toposync.identity-evidence+json",
                    private=True,
                )
            )
        return [output]


class RecognizeIdentityRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict, dependencies: PipelineRuntimeDependencies):
        self.config = RecognizeIdentityConfig.model_validate(config)
        self.dependencies = dependencies

    def _resolve(self, packet: Packet, store: Any, key: str, artifact: Artifact | None):
        # Decodificação, locks e SQLite ficam no executor limitado do operador.
        # Cancelar a espera não interrompe uma transação já iniciada na thread.
        current = store.decision(key)
        if packet.lifecycle == Lifecycle.CLOSE:
            store.close_occurrence(key)
        elif artifact is not None:
            if (
                not artifact.private
                or not isinstance(artifact.data, bytes)
                or len(artifact.data) > 2 * 1024 * 1024
            ):
                raise ValueError("invalid_evidence_envelope")
            decoded = json.loads(artifact.data)
            evidence = IdentityEvidence.model_validate(decoded["evidence"])
            if evidence.occurrence_id != key or evidence.camera_id != packet.payload.get(
                "camera_id"
            ):
                raise ValueError("evidence_provenance_mismatch")
            policy = next(
                (
                    item
                    for item in self.config.policies
                    if item.species == evidence.species
                    and item.embedding_space == evidence.embedding_space
                ),
                None,
            )
            if policy is not None:
                store.register_policy(policy)
            crop = base64.b64decode(decoded["crop"], validate=True) if decoded.get("crop") else None
            current = store.observe(
                evidence,
                policy,
                crop=crop,
                expected_gallery_revision=store.revision,
            )
        elif current is not None:
            incoming = packet.payload.get("recognition", {})
            policy = next(
                (
                    item
                    for item in self.config.policies
                    if item.embedding_space == current.embedding_space
                    and item.species == str(packet.payload["subject"].get("category") or "").lower()
                ),
                None,
            )
            current = store.no_evidence(
                key,
                observed_at=resolve_media_ts(packet),
                status=str(incoming.get("status", "unobservable")),
                reason=str(incoming.get("reason", "evidence_missing")),
                continuity_seconds=policy.continuity_seconds if policy else 0,
                expected_policy_fingerprint=policy.fingerprint() if policy else "",
            )
        return current

    async def process_packet(self, packet: Packet, context: Any) -> list[Packet]:
        artifact = packet.artifacts.get(PRIVATE_EVIDENCE_ARTIFACT)
        # O consumidor terminal remove evidência mesmo quando desligado ou indisponível.
        clean = (
            replace(
                packet,
                artifacts={
                    name: value
                    for name, value in packet.artifacts.items()
                    if name not in {PRIVATE_EVIDENCE_ARTIFACT, PRIVATE_DECISION_ARTIFACT}
                },
            )
            if artifact or PRIVATE_DECISION_ARTIFACT in packet.artifacts
            else packet
        )
        if not self.config.enabled:
            return [clean]
        key = occurrence_key(packet)
        if key is None:
            return [
                _summary(
                    clean,
                    status="unobservable",
                    reason="individual_occurrence_required",
                    occurrence_id=None,
                )
            ]
        if self.dependencies.services is None:
            return [
                _summary(
                    clean, status="unavailable", reason="gallery_unavailable", occurrence_id=key
                )
            ]
        try:
            store = await self.dependencies.services.call("vision.identity.store")
            current = await context.run_blocking(self._resolve, packet, store, key, artifact)
            if current is not None:
                payload = dict(clean.payload)
                payload["recognition"] = current.packet_summary()
                output = replace(clean, payload=payload).with_artifact(
                    Artifact(
                        name=PRIVATE_DECISION_ARTIFACT,
                        data=current.model_dump_json(exclude={"candidate_ids"}).encode(),
                        mime_type="application/vnd.toposync.identity-decision+json",
                        private=True,
                    )
                )
                return [output]
            return [clean]
        except Exception:
            return [
                _summary(
                    clean,
                    status="unavailable",
                    reason="gallery_operation_failed",
                    occurrence_id=key,
                )
            ]


def register_identity_operators(registry: OperatorRegistry) -> None:
    for operator_id, schema, factory, capabilities, resource in [
        (
            "vision.identity_context",
            IdentityContextConfig,
            IdentityContextRuntime,
            ["vision"],
            "none",
        ),
        (
            "vision.identity_evidence",
            IdentityEvidenceConfig,
            IdentityEvidenceRuntime,
            ["vision", "heavy_compute", "private_data"],
            "vision_model",
        ),
        (
            "vision.recognize_identity",
            RecognizeIdentityConfig,
            RecognizeIdentityRuntime,
            ["vision", "origin_only", "private_data"],
            "storage",
        ),
    ]:
        if registry.get(operator_id) is not None:
            continue
        registry.register_operator(
            operator_id=operator_id,
            description="Local person and pet identity; explicit activation required.",
            config_model=schema,
            defaults=schema().model_dump(),
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=capabilities,
            execution_mode="in_event_loop"
            if operator_id == "vision.identity_context"
            else "process_pool"
            if operator_id == "vision.identity_evidence"
            else "thread_pool",
            max_concurrency=1,
            state_kind="stateless"
            if operator_id == "vision.identity_context"
            else "stateful_per_subject",
            share_strategy="never",
            ordering="strict",
            resource_kind=resource,
            pressure_behavior="ignore",
            preserves_lifecycle=True,
            requires_payload_keys=["subject"],
            produces_payload_keys=["recognition"],
            produces_artifacts=[]
            if operator_id == "vision.identity_context"
            else [
                PRIVATE_EVIDENCE_ARTIFACT
                if operator_id == "vision.identity_evidence"
                else PRIVATE_DECISION_ARTIFACT
            ],
            owner="com.toposync.vision",
            runtime_factory=factory,
        )
