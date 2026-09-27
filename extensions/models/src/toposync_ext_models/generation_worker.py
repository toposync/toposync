"""Offline, bounded TripoSR subprocess. Heavy dependencies stay outside the server."""
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time


BACKGROUND = {
    "file": "weights/u2netp.onnx",
    "url": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx",
    "sha256": "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8",
    "bytes": 4_574_861,
}


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
    home, source, output = map(Path, sys.argv[1:4])

    def guard():
        while True:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform != "darwin":
                peak *= 1024
            if peak > 12 * 1024**3:
                os._exit(75)
            time.sleep(0.1)

    threading.Thread(target=guard, daemon=True).start()
    manifest = json.loads(Path(__file__).with_name("triposr_manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        with (home / relative).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ValueError("provider_asset_integrity_failed")
    sys.path.insert(0, str(home / "source"))
    import numpy as np
    import torch
    import trimesh
    from PIL import Image
    from tsr.system import TSR
    from tsr.utils import resize_foreground

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    image, background = isolate_object(home, Image.open(source).convert("RGBA"))
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
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "vertices": len(mesh.vertices), "faces": len(mesh.faces), "bounds": mesh.bounds.tolist(),
    }))


if __name__ == "__main__":
    main()
