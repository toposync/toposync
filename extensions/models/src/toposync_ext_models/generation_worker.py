"""Offline, bounded TripoSR subprocess. Heavy dependencies stay outside the server."""
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time


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
    model = TSR.from_pretrained(str(home / "weights"), config_name="config.yaml", weight_name="model.ckpt")
    model.eval()
    model.renderer.set_chunk_size(8192)
    image = resize_foreground(Image.open(source).convert("RGBA"), 0.85)
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
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "vertices": len(mesh.vertices), "faces": len(mesh.faces), "bounds": mesh.bounds.tolist(),
    }))


if __name__ == "__main__":
    main()
