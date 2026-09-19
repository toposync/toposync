from __future__ import annotations

from dataclasses import dataclass
import hashlib
from functools import lru_cache
import math
from pathlib import Path
import threading

import numpy as np

from ..registry.manifests import ModelManifest
from .contracts import IdentityEvidence, Species


class IdentityModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    reason: str
    evidence: IdentityEvidence | None = None
    crop: bytes | None = None


def verified_model_path(manifest: ModelManifest) -> Path:
    path = manifest.resolve_artifact_path()
    if not path.is_file():
        raise IdentityModelUnavailable("model_not_installed")
    if not 1 <= path.stat().st_size <= 512 * 1024 * 1024 or len(manifest.sha256) != 64:
        raise IdentityModelUnavailable("model_integrity_unverifiable")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != manifest.sha256:
        raise IdentityModelUnavailable("model_checksum_mismatch")
    return path


def validate_identity_frame(frame: object) -> str | None:
    """Verificação barata antes da serialização e novamente no trabalhador."""
    if (
        type(frame) is not np.ndarray
        or frame.dtype != np.uint8
        or frame.ndim != 3
        or frame.shape[2] != 3
    ):
        return "unsupported_image"
    height, width = frame.shape[:2]
    if not height or not width or height * width > 12_000_000:
        return "image_dimensions_exceed_budget"
    return None


class IdentityExtractor:
    """Inferência CPU limitada; o scheduler controla concorrência e amostragem."""

    def __init__(self, manifest: ModelManifest, *, face_detector: ModelManifest | None = None):
        self.manifest = manifest
        self.face_detector = face_detector
        self._lock = threading.Lock()
        self._session = None
        self._detector = None
        self._recognizer = None
        if manifest.task != "embedding":
            raise ValueError("identity extraction requires an embedding model")
        adapter = manifest.resolved_adapter_family()
        if adapter not in {"pet_identity_rgb_v1", "sface_aligncrop_bgr_v1"}:
            raise ValueError("unsupported identity model preprocessing")
        if adapter == "sface_aligncrop_bgr_v1" and face_detector is None:
            raise ValueError("face recognition requires an explicit face detector")
        self.embedding_dimensions = 512 if adapter == "pet_identity_rgb_v1" else 128
        # Alinhamento, detector e pesos fazem parte do espaço; dimensão sozinha não basta.
        signature = [manifest.sha256, adapter, manifest.input.model_dump_json()]
        if face_detector:
            signature.extend([face_detector.sha256, face_detector.resolved_adapter_family()])
        self.embedding_space = (
            "identity:v1:" + hashlib.sha256("\0".join(signature).encode()).hexdigest()
        )

    def process_specification(self) -> tuple[str, str | None]:
        # Resolver caminhos no servidor selecionado antes de cruzar seu processo local.
        def encode(manifest):
            return manifest.model_copy(
                update={"artifact_path": str(manifest.resolve_artifact_path())}
            ).model_dump_json()

        return encode(self.manifest), encode(self.face_detector) if self.face_detector else None

    def _load(self) -> None:
        if self._session is not None or self._recognizer is not None:
            return
        path = verified_model_path(self.manifest)
        if self.manifest.resolved_adapter_family() == "pet_identity_rgb_v1":
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self._session = ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            inputs, outputs = self._session.get_inputs(), self._session.get_outputs()
            if (
                len(inputs) != 1
                or inputs[0].name != "input"
                or inputs[0].shape[1:] != [3, 224, 224]
            ):
                self._session = None
                raise IdentityModelUnavailable("model_input_contract_mismatch")
            if not any(item.name == "embedding" and item.shape[-1] == 512 for item in outputs):
                self._session = None
                raise IdentityModelUnavailable("model_output_contract_mismatch")
        else:
            import cv2

            detector_path = verified_model_path(self.face_detector)
            self._detector = cv2.FaceDetectorYN.create(
                str(detector_path), "", (320, 320), 0.8, 0.3, 64
            )
            self._recognizer = cv2.FaceRecognizerSF.create(str(path), "")

    def extract(
        self,
        frame: np.ndarray,
        *,
        species: Species,
        occurrence_id: str,
        source_id: str,
        camera_id: str,
        capture_id: str,
        observed_at: float,
        region: tuple[float, float, float, float],
        color_order: str = "bgr",
    ) -> ExtractionResult:
        if not self.manifest.supports_capability(f"identity.{species}"):
            return ExtractionResult("unavailable", "model_species_mismatch")
        image_error = validate_identity_frame(frame)
        if image_error:
            return ExtractionResult("unobservable", image_error)
        height, width = frame.shape[:2]
        if color_order not in {"rgb", "bgr"}:
            return ExtractionResult("unobservable", "unsupported_color_order")
        if len(region) != 4 or not all(
            math.isfinite(value) and 0 <= value <= 1 for value in region
        ):
            return ExtractionResult("unobservable", "invalid_subject_region")
        x1, y1, x2, y2 = region
        if x2 <= x1 or y2 <= y1:
            return ExtractionResult("unobservable", "invalid_subject_region")
        left, top = int(x1 * width), int(y1 * height)
        right, bottom = min(width, math.ceil(x2 * width)), min(height, math.ceil(y2 * height))
        if min(right - left, bottom - top) < 64:
            return ExtractionResult("unobservable", "insufficient_resolution")
        import cv2

        crop = np.ascontiguousarray(frame[top:bottom, left:right])
        if color_order == "rgb":
            crop = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        if max(crop.shape[:2]) > 1024:
            factor = 1024 / max(crop.shape[:2])
            crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)

        def abstain(status: str, reason: str) -> ExtractionResult:
            # A foto pode ser nomeada sem embedding nem admissão como referência.
            encoded, retained = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            blob = retained.tobytes() if encoded and retained.nbytes <= 1024 * 1024 else None
            observation = IdentityEvidence(
                species=species,
                occurrence_id=occurrence_id,
                source_id=source_id,
                camera_id=camera_id,
                capture_id=capture_id,
                observed_at=observed_at,
                embedding_space=self.embedding_space,
                vector=None,
                status=status,
                reason=reason,
                quality=0,
                reference_eligible=False,
                quality_reasons=(reason,),
                region=region,
            )
            return ExtractionResult(status, reason, observation, blob)

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        light = float(np.mean(gray))
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if light < 15 or light > 240:
            return abstain("unobservable", "exposure")
        if sharpness < 16:
            return abstain("unobservable", "blur")
        if not self._lock.acquire(blocking=False):
            return abstain("unavailable", "inference_busy")
        try:
            try:
                self._load()
            except IdentityModelUnavailable as exc:
                return abstain("unavailable", str(exc))
            except Exception:
                return abstain("unavailable", "model_load_failed")
            selected_region = region
            if species == "person":
                self._detector.setInputSize((crop.shape[1], crop.shape[0]))
                _, faces = self._detector.detect(crop)
                if faces is None or len(faces) == 0:
                    return abstain("unobservable", "face_not_visible")
                if len(faces) != 1:
                    return abstain("unobservable", "ambiguous_face_association")
                face = faces[0]
                if min(float(face[2]), float(face[3])) < 48:
                    return abstain("unobservable", "insufficient_face_resolution")
                aligned = self._recognizer.alignCrop(crop, face)
                vector = self._recognizer.feature(aligned).reshape(-1)
                # A região retornada permanece relativa ao quadro original, não ao crop.
                sx, sy = (right - left) / crop.shape[1], (bottom - top) / crop.shape[0]
                selected_region = (
                    max(0, (left + float(face[0]) * sx) / width),
                    max(0, (top + float(face[1]) * sy) / height),
                    min(1, (left + float(face[0] + face[2]) * sx) / width),
                    min(1, (top + float(face[1] + face[3]) * sy) / height),
                )
                retained = aligned
            else:
                rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                resized = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_CUBIC)
                normalized = resized.astype(np.float32) / 255.0
                normalized = (
                    normalized - np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
                ) / np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
                tensor = np.ascontiguousarray(normalized.transpose(2, 0, 1)[None])
                vector = self._session.run(["embedding"], {"input": tensor})[0].reshape(-1)
                retained = crop
            if vector.size != self.embedding_dimensions:
                return abstain("unavailable", "model_output_contract_mismatch")
            evidence = IdentityEvidence(
                species=species,
                occurrence_id=occurrence_id,
                source_id=source_id,
                camera_id=camera_id,
                capture_id=capture_id,
                observed_at=observed_at,
                embedding_space=self.embedding_space,
                vector=tuple(float(value) for value in vector),
                quality=min(1.0, sharpness / 200),
                reference_eligible=True,
                region=selected_region,
            )
            encoded, image = cv2.imencode(".jpg", retained, [cv2.IMWRITE_JPEG_QUALITY, 85])
            blob = image.tobytes() if encoded and image.nbytes <= 1024 * 1024 else None
            return ExtractionResult("pending", "evidence_ready", evidence, blob)
        except Exception:
            # Não incluir parâmetros, pixels ou vetores nas mensagens operacionais.
            return abstain("unavailable", "inference_failed")
        finally:
            self._lock.release()


@lru_cache(maxsize=2)
def _process_extractor(specification: tuple[str, str | None]) -> IdentityExtractor:
    """Each existing executor process retains at most two model instances, no gallery."""
    manifest, detector = specification
    return IdentityExtractor(
        ModelManifest.model_validate_json(manifest),
        face_detector=ModelManifest.model_validate_json(detector) if detector else None,
    )


def extract_in_process(
    specification: tuple[str, str | None], frame: np.ndarray, **values
) -> ExtractionResult:
    """Picklable entry point; native imports and inference stay outside the application loop."""
    return _process_extractor(specification).extract(frame, **values)
