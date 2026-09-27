"""Local multilingual image retrieval; similarity is not identity or a safety decision."""
from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

# These exports share the CLIP ViT-B/32 space. Do not substitute weights by filename.
MODEL_FILES = {
    "clip-vit-base-patch32/onnx/vision_model_quantized.onnx": "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299",
    "clip-multilingual-text/onnx/model_qint8_arm64.onnx": "3bed77e83926519660b5c0833cb3321cfde952331edec4f7d501c7d384b8a8a7",
    "clip-multilingual-text/2_Dense/model.safetensors": "d12568dc7300970a4d3dbb49068ad16cd89b99840b74b026f8e48071e9414f74",
    "clip-multilingual-text/tokenizer.json": "5b4e1a8171c81dfd666ae40265b9530c6e0b3d53923fe8ac493dcc84229adf81",
}
SPACE = "clip-multilingual-rgb-center224-mean-pool-v1:" + hashlib.sha256(
    "\n".join(MODEL_FILES.values()).encode()
).hexdigest()
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_BATCH = 8
# ponytail: one inference per process; use a worker pool only after measured demand.
_LOCK = threading.Lock()


def image_tensor(data: bytes) -> np.ndarray:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("visual_image_bytes_exceed_budget")
    with Image.open(io.BytesIO(data)) as source:
        if (not 0 < source.width * source.height <= 16_000_000
                or max(source.size) / min(source.size) > 32):
            raise ValueError("visual_image_dimensions_exceed_budget")
        image = ImageOps.exif_transpose(source).convert("RGB")
        ratio = 224 / min(image.size)
        image = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.Resampling.BICUBIC)
        left, top = (image.width - 224) // 2, (image.height - 224) // 2
        image = image.crop((left, top, left + 224, top + 224))
        pixels = np.asarray(image, dtype=np.float32) / 255
    pixels = (pixels - np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)) / np.array(
        [0.26862954, 0.26130258, 0.27577711], dtype=np.float32
    )
    return np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])


def _normalized(value: np.ndarray) -> list[float]:
    vector = np.asarray(value, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(vector)
    if vector.shape != (512,) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 0:
        raise ValueError("invalid_visual_embedding")
    return (vector / norm).tolist()


def embed_history(*, data_dir: Path, query: str, images: dict[str, bytes]) -> dict:
    """No downloads, network, persistent sessions or image retention."""
    if not isinstance(query, str) or not query.strip() or len(query) > 1000 or len(images) > MAX_BATCH:
        raise ValueError("invalid_visual_embedding_request")
    if any(not isinstance(value, bytes) or not 0 < len(value) <= MAX_IMAGE_BYTES for value in images.values()):
        raise ValueError("visual_image_bytes_exceed_budget")
    with _LOCK:
        try:
            import onnxruntime as ort
            from safetensors.numpy import load_file
            from tokenizers import Tokenizer

            root = Path(data_dir) / "vision-models"
            for name, expected in MODEL_FILES.items():
                with (root / name).open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                        raise ValueError("visual_model_checksum_mismatch")
            tokenizer = Tokenizer.from_file(str(root / "clip-multilingual-text/tokenizer.json"))
            encoded = tokenizer.encode(query)
            if len(encoded.ids) > 128:
                raise ValueError("visual_query_token_limit")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(str(root / "clip-multilingual-text/onnx/model_qint8_arm64.onnx"),
                sess_options=options, providers=["CPUExecutionProvider"])
            try:
                mask = np.array([encoded.attention_mask], dtype=np.int64)
                hidden = session.run(["last_hidden_state"], {
                    "input_ids": np.array([encoded.ids], dtype=np.int64), "attention_mask": mask,
                })[0]
                pooled = (hidden * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
                weights = load_file(root / "clip-multilingual-text/2_Dense/model.safetensors")["linear.weight"]
                query_vector = _normalized(pooled @ weights.T)
            finally:
                del session
            vectors, unavailable = {}, []
            if images:
                session = ort.InferenceSession(str(root / "clip-vit-base-patch32/onnx/vision_model_quantized.onnx"),
                    sess_options=options, providers=["CPUExecutionProvider"])
                try:
                    for identifier, data in images.items():
                        try:
                            tensor = image_tensor(data)
                        except (OSError, ValueError, Image.DecompressionBombError):
                            unavailable.append(identifier)
                            continue
                        vectors[identifier] = _normalized(session.run(["image_embeds"], {"pixel_values": tensor})[0])
                finally:
                    del session
            return {"space": SPACE, "query": query_vector, "images": vectors, "unavailable": unavailable}
        except (ImportError, OSError) as exc:
            raise ValueError("local_visual_embeddings_unavailable") from exc
