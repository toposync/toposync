# Models (3D)

First-party extension that lets you upload and place GLB/GLTF assets as composition elements, including a generated top-down preview used by the 2D editor.

This extension depends on the core backend `/api/files/upload` + `/files/*` endpoints.


The editor accepts a known object height in meters and applies a uniform scale, preserving proportions. Imported models are grounded at their lowest point by default. The scale multiplier remains available under advanced settings; it is not a measurement inferred from photographs.

Import checks selected files against a combined 64 MiB limit. Before preview and scene rendering, geometry must have finite vertices and transforms, at most 2 million vertices and at most 16,777,216 decoded texture pixels in total. Decoded assets are released after preview generation and on failed or superseded loads. These checks run after the loader decodes the model; they do not impose a hard peak-memory limit on decoding or remote resources referenced by GLTF.

Image-to-model generation and metric reconstruction are not provided. Import an existing GLB/GLTF asset; supplying a height reference establishes its scale, not its accuracy.

Focused verification (compile output must stay outside the repository):

```sh
npx tsc extensions/models/ui/src/validation.test.ts --module commonjs --target ES2022 --moduleResolution node --esModuleInterop --skipLibCheck --outDir "$MODEL_TEST_OUTPUT"
NODE_PATH="$PWD/node_modules" node --test "$MODEL_TEST_OUTPUT/validation.test.js"
```

Leaving the model tool or pressing Escape aborts pending file uploads and discards
late import results. A cancelled operation cannot place a model or reset a newer
operation. Already stored files are not deleted; a preview decoder that has already
started may finish, but its result is discarded after cancellation.

## Optional local generation backend

`POST /api/models/generate` accepts one multipart `file` and requires both
`core:files:read` and `core:files:write`. The model tool uses this endpoint. It accepts a PNG/JPEG/WebP image, at most 8 MiB and 16 million pixels. Existing transparency is reused. Opaque photos require the optional local background model below. EXIF orientation is normalized before processing. Empty transparent and uniform opaque images are rejected before starting a worker.

The server looks for a trusted, administrator-installed TripoSR runtime under
`<data_dir>/models/triposr`, or `TOPOSYNC_MODELS_TRIPOSR_HOME`. It never accepts
runtime paths, commands, model downloads or cloud fallbacks from the request.
Required layout:

- `environment/bin/python`: isolated Python with PyTorch, torchmcubes and TripoSR dependencies.
- `source/tsr/`: official TripoSR source revision `107cefdc244c39106fa830359024f6a2f1c78871`.
- `weights/config.yaml` and `weights/model.ckpt`: `stabilityai/TripoSR` revision `5b521936b01fbe1890f6f9baed0254ab6351c04a`.
- `huggingface/hub/`: offline cache for the `facebook/dino-vitb16` configuration, revision `f205d5d8e640a89a2b8ef0369670dfc37cc07fc2`, including its `refs/main` pointer. Additional DINO weights are not needed.

`triposr_manifest.json` pins source/configuration/weight SHA-256 hashes, checked
inside every worker before importing provider code. Preserve the upstream MIT
license and attribution with an installation. The application does not infer
readiness from the presence of files alone.
Missing runtime returns 503; a conflicting generation returns 409. The evaluated
runtime uses macOS/CPU, Python 3.12, PyTorch 2.11.0, torchmcubes revision
`879926d0ef58e6ce0ac2630fdecb5e53af7ed3ff`, transformers 4.35.0, numpy 1.26.4,
trimesh 4.0.5, omegaconf 2.3.0, einops 0.7.0 and rembg 2.0.69. Other environments
require qualification; Windows is currently unavailable. The server environment
itself does not need PyTorch. Do not install these versions over an existing
processing environment.

One worker per runtime directory is enforced across server processes with a file
lock. The worker uses two CPU threads, a 128³ extraction grid and offline caches;
a watchdog checks its peak memory every 100 ms and exits above 12 GiB. The server
enforces a 300-second timeout and reaps the process on disconnect or cancellation.
This is a sampled memory guard, not a reservation or an instantaneous allocation
cap. Original input and intermediate files are temporary and removed at the end.

A successful response follows the existing file-upload shape (`dir`, `filename`,
`url`, `size_bytes`) and adds `generation`. The same metadata is stored alongside
`model.glb` as `generation.json`. It identifies the provider/revisions, input hash,
geometry counts and `generated_approximation`, with `metric_reconstruction=false`.
Authorization is checked again before publication. The generated Z-up mesh is
converted to GLTF Y-up; the existing importer supplies scale/floor placement.
This does not establish real dimensions or observed geometry behind the object.

### Generate through the model tool

Select **3D model**, choose a placement point, then select one PNG/JPEG/WebP image of an isolated object (up to 8 MiB). The same tool imports GLB/GLTF and their assets when a model file is selected. Generation uses the optional local runtime above; missing runtimes, unusable images, busy workers and failures have explicit messages. The progress dialog supports keyboard access, Cancel and Escape.

Generated elements retain the generation provenance and show an approximation notice when edited. Enter a known height to scale the object; generated geometry does not supply a measured reference height. Preview, placement, floor height and advanced scale reuse the importer. Opaque photos use the optional background model described below. Runtime installation is an explicit administrator operation.

### Install the evaluated runtime

On **macOS ARM with Python 3.12**, install `uv`, CMake and Apple Command Line Tools first. From the server Python environment, run:

```sh
python -m toposync_ext_models.install_triposr /absolute/path/to/data/models/triposr
```

This creates a separate environment; it does not modify the server environment or global compiler settings. Allow several gigabytes of free disk space. It downloads the pinned provider source, MIT license, configuration and approximately 1.68 GB checkpoint; generation itself remains offline. `triposr_requirements.txt` fixes all evaluated Python dependency versions. The extractor is built from a fixed Git revision without reusing binary build cache, with two compilation jobs and explicit linkage to the isolated PyTorch. Package artifacts are obtained from the administrator's configured package index; dependency versions are checked, but the installer does not claim a complete dependency artifact hash lock.

An existing directory is refused. If this installer was interrupted, inspect its `installation.log`, fix the diagnosed cause, then repeat with `--resume`. Resumption requires the matching incomplete-install marker; it never adopts an unrelated directory. Existing assets with wrong hashes are preserved and rejected. The server refuses a runtime while its incomplete marker exists. Installation holds the same runtime lock used by generation.

Optional `--asset-cache /path/to/existing/runtime` reuses **copies** of hash-verified source/weight assets, without changing the cache. This avoids downloading the checkpoint again. The evaluated installation used this path; a complete uncached download/install journey has not been separately qualified.

```sh
python -m toposync_ext_models.install_triposr /absolute/path/to/runtime --check
```

The check verifies asset hashes, exact dependency versions, extractor source revision, dependency compatibility, provider import and a real CPU mesh extraction. It loads no model and does not certify generation quality. Success writes `installation.json` on installation and removes the incomplete marker. Commands have bounded timeouts and their process groups are reaped on timeout/interruption; logs stay in the chosen runtime directory. Other operating systems/architectures remain unqualified by this installer.

### Correct object orientation

Open **Orientation and scale** in the model editor. Adjust forward/side tilt or turn, then **Apply orientation**. The original GLB/GLTF stays unchanged. The same transform is applied before computing the top-down preview and rendering the model; dimensions, footprint and base are updated together after preview upload succeeds. The rotated static geometry is recentered and rests at the selected height. A known-height reference is cleared because tilting changes the vertical dimension.

Closing the editor aborts upload and discards late results; failure leaves the previously applied model intact. Already uploaded previews remain stored, as with imports. Ground contact is computed from static vertices, not guaranteed throughout animation. Orientation is manual: generation does not establish gravity or real-world dimensions.

### Optional background removal for ordinary photographs

After installing the base runtime, explicitly add the lightweight U2netp model:

```sh
python -m toposync_ext_models.install_triposr /absolute/path/to/runtime --install-background
```

This verifies the existing runtime, shares its generation lock, and adds only the 4,574,861-byte pinned ONNX weight. It does not rebuild the environment or download the 3D checkpoint again. `--asset-cache` can supply the same `weights/u2netp.onnx` layout. The original installation receipt remains intact; `background-installation.json` records the added weight. `--check` also verifies this weight when present.

The worker uses the installed rembg adapter with an explicitly local weight path and CPU provider, two intra-operation threads and one inter-operation thread. It verifies SHA-256 before loading and never invokes rembg's downloader. Missing or corrupt weights yield an unavailable response; transparent cutouts remain usable without them. Source and attribution: [U2Net](https://github.com/xuebinqin/U-2-Net) and [rembg](https://github.com/danielgatis/rembg). The ONNX asset URL and SHA-256 are fixed in `generation_worker.py`.

Masks whose foreground occupies less than 0.5% or more than 98% of pixels are rejected before loading the 3D model. These are degeneracy checks, **not** proof of an object, a quality score, or a reliable detector of all empty scenes. Prefer a single object against a contrasting background. Complex scenes, thin structures, occlusion and reflective/transparent objects remain unqualified. The evaluated examples are two controlled chair backgrounds and one ordinary coffee photograph; this is not a broad benchmark.

Generated provenance records `background_removal.method` as `provided_alpha` or `estimated`; estimated masks also identify the provider and weight hash. The editor labels automatic background removal and asks the user to inspect outline and orientation. Approximate geometry still requires manual orientation/scale correction when necessary. Segmentation sessions are released before 3D model loading, within the existing bounded worker lifecycle.
