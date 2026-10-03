"""Offline, bounded TripoSR subprocess. Heavy dependencies stay outside the server."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import struct
import sys
import threading
import time


BACKGROUND = {
    "file": "weights/u2netp.onnx",
    "url": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx",
    "sha256": "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8",
    "bytes": 4_574_861,
}


def canonicalize_transparent_pixels(image):
    """Discard invisible RGB noise without changing any visible pixel or alpha."""
    transparent = image.getchannel("A").point(lambda alpha: 255 if alpha == 0 else 0)
    image.paste((0, 0, 0, 0), mask=transparent)


def prepared_image_digest(image):
    """Bind a review to dimensions and exact RGBA pixels, not PNG encoding."""
    rgba = image.convert("RGBA")
    digest = hashlib.sha256(b"toposync-prepared-rgba-v1\0")
    digest.update(struct.pack("<II", *rgba.size))
    digest.update(rgba.tobytes())
    return digest.hexdigest()


def validate_background_mask(mask):
    histogram = mask.convert("L").histogram()
    fraction = sum(histogram[128:]) / sum(histogram)
    # This only rejects degenerate masks; saliency is not object/quality confidence.
    if not 0.005 <= fraction <= 0.98:
        raise SystemExit(74)


def isolate_object(home, image):
    low, high = image.getchannel("A").getextrema()
    if high == 0:
        raise SystemExit(74)
    if low < 255:
        return image, {"method": "provided_alpha"}
    if all(low == high for low, high in image.convert("RGB").getextrema()):
        raise SystemExit(74)
    weights = home / BACKGROUND["file"]
    if not weights.is_file():
        raise SystemExit(76)
    with weights.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != BACKGROUND["sha256"]:
            raise SystemExit(76)
    import onnxruntime as ort
    from rembg.sessions.u2netp import U2netpSession

    class OfflineSession(U2netpSession):
        @classmethod
        def download_models(cls, *args, **kwargs):
            return str(weights)

    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = OfflineSession("u2netp", options, providers=["CPUExecutionProvider"])
    mask = session.predict(image.convert("RGB"))[0]
    validate_background_mask(mask)
    image.putalpha(mask)
    return image, {"method": "estimated", "provider": "U2netp", "weights_sha256": BACKGROUND["sha256"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("home", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--expected-prepared-sha256")
    options = parser.parse_args()
    home, source, output = options.home, options.source, options.output

    def guard():
        while True:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform != "darwin":
                peak *= 1024
            if peak > (2 if options.prepare_only else 12) * 1024**3:
                os._exit(75)
            time.sleep(0.1)

    threading.Thread(target=guard, daemon=True).start()
    # Preparation only needs Pillow and the optional pinned segmentation model.
    # It must not read the 3D checkpoint or import its heavy runtime.
    from PIL import Image

    image, background = isolate_object(home, Image.open(source).convert("RGBA"))
    canonicalize_transparent_pixels(image)
    prepared_sha256 = prepared_image_digest(image)
    if options.expected_prepared_sha256 and options.expected_prepared_sha256 != prepared_sha256:
        raise SystemExit(77)
    if options.prepare_only:
        image.save(output, "PNG")
        output.with_suffix(".json").write_text(json.dumps({
            "kind": "image_preparation",
            "prepared_sha256": prepared_sha256,
            "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "background_removal": background,
        }))
        return

    manifest = json.loads(Path(__file__).with_name("triposr_manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        with (home / relative).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ValueError("provider_asset_integrity_failed")
    sys.path.insert(0, str(home / "source"))
    import numpy as np
    import torch
    import trimesh
    from tsr.system import TSR
    from tsr.utils import resize_foreground

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    image = resize_foreground(image, 0.85)
    model = TSR.from_pretrained(str(home / "weights"), config_name="config.yaml", weight_name="model.ckpt")
    model.eval()
    model.renderer.set_chunk_size(8192)
    pixels = np.array(image).astype(np.float32) / 255
    pixels = pixels[:, :, :3] * pixels[:, :, 3:4] + (1 - pixels[:, :, 3:4]) * 0.5
    image = Image.fromarray((pixels * 255).astype(np.uint8))
    with torch.inference_mode():
        codes = model([image], device="cpu")
        mesh = model.extract_mesh(codes, has_vertex_color=True, resolution=128)[0]
    if (not 0 < len(mesh.vertices) <= 2_000_000 or not len(mesh.faces)
            or not np.isfinite(mesh.vertices).all()
            or mesh.faces.min() < 0 or mesh.faces.max() >= len(mesh.vertices)):
        raise ValueError("invalid_generated_geometry")
    # Provider camera convention is Z-up; GLTF and Toposync use Y-up.
    mesh.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    if min(mesh.extents) <= 0:
        raise ValueError("empty_generated_geometry")
    mesh.export(output, file_type="glb")
    if not 0 < output.stat().st_size <= 64 * 1024**2:
        raise ValueError("generated_model_too_large")
    output.with_suffix(".json").write_text(json.dumps({
        "kind": "generated_approximation", "provider": manifest["provider"],
        "model_revision": manifest["model_revision"], "source_revision": manifest["source_revision"],
        "license": manifest["license"], "metric_reconstruction": False,
        "background_removal": background,
        "prepared_sha256": prepared_sha256,
        "preparation_review_verified": options.expected_prepared_sha256 is not None,
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "vertices": len(mesh.vertices), "faces": len(mesh.faces), "bounds": mesh.bounds.tolist(),
    }))


if __name__ == "__main__":
    main()
