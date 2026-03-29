# Processing Server (Development)

This guide explains how to install dependencies and run the Toposync processing server in development mode.

The processing server is used by distributed pipelines. It receives pipeline config from the origin server, runs processing-side nodes, and streams projected events back to origin.

## Prerequisites

- Python `3.11+`
- `uv`
- Optional for camera pipelines: `ffmpeg` available in `PATH`

Node.js is not required to run the processing server itself. It is only needed if you also run the frontend UI.

## 1) Install dependencies

From the repository root:

```bash
uv sync
```

The first-party ONNX Runtime backend for `vision.detect` and `vision.segment_instances` is installed with the `com.toposync.vision` extension.

Built-in first-party detection manifests currently target RTMDet:
- `rtmdet_det_tiny`
- `rtmdet_det_small`
- `rtmdet_det_medium`

Built-in first-party detection manifests also include RF-DETR:
- `rfdetr_det_nano`
- `rfdetr_det_small`
- `rfdetr_det_medium`

Built-in first-party segmentation manifests currently target RTMDet-Ins:
- `rtmdet_ins_tiny`
- `rtmdet_ins_small`
- `rtmdet_ins_medium`

Their manifests are discovered automatically from `extensions/vision/manifests/`. Custom manifests can still be added with `TOPOSYNC_VISION_MANIFESTS_DIR` or `TOPOSYNC_VISION_MANIFEST_PATHS`, and manifests imported from the UI are stored under `.toposync-data/vision-manifests/` on that processing server.

The first-party manifests are bundled with the extension, but the official ONNX artifacts are not. In installed environments, Toposync expects them under `TOPOSYNC_DATA_DIR/vision-models/...` (or `.toposync-data/vision-models/...` by default). In a source checkout, existing files under `extensions/vision/models/...` are still honored. A validated manual provisioning flow for `rtmdet_det_tiny`, `rtmdet_det_small`, and `rtmdet_det_medium` is documented in [VISION_MODEL_PROVISIONING.md](VISION_MODEL_PROVISIONING.md).

Experimental assisted local build is also available for RTMDet detection in this phase when all of these are true:
- the processing server is Linux
- `docker` or `podman` is available in `PATH`
- the user explicitly starts the local build flow from the UI/API
- the model manifest includes upstream checkpoint/config metadata and a known ONNX checksum

That flow still stays inside the "yellow" boundary:
- the checkpoint is downloaded directly by the target processing server
- the ONNX is exported locally on that machine
- the final artifact is validated against the manifest checksum
- TopoSync does not mirror, bundle, or host the checkpoint/ONNX for the user

Experimental assisted local build is also available for RF-DETR detection in this phase when all of these are true:
- the processing server host is `linux`, `darwin`, or `windows`
- a compatible local Python runtime is available
- the user explicitly starts the local build flow from the UI/API
- the model manifest includes upstream checkpoint metadata

That RF-DETR flow also stays inside the same "yellow" boundary:
- the checkpoint is downloaded directly by the target processing server
- the ONNX is exported locally on that machine through the official RF-DETR Python package
- TopoSync does not mirror, bundle, or host the checkpoint/ONNX for the user

UI entry points in this phase:
- pipeline editor recovery card for `vision.detect`
- Processing Servers screen after the server status has been tested/refreshed

The assisted local-build audit trail now records, per job:
- who started it
- which upstream sources were accepted
- builder/runtime metadata
- the final exported ONNX sha256 after verification

The Processing Servers screen also surfaces the latest assisted-build actor/source/hash summary from status, so admins do not need shell access just to understand the last run.

If you want the UI to offer one-click install for recommended models, configure one of these source mechanisms on the processing server:
- `TOPOSYNC_VISION_MODEL_SOURCE_<MODEL_ID>`: absolute file path or HTTP/HTTPS URL for one specific model
- `TOPOSYNC_VISION_MODEL_URL_<MODEL_ID>`: HTTP/HTTPS URL for one specific model
- `TOPOSYNC_VISION_MODEL_PATH_<MODEL_ID>`: absolute file path for one specific model
- `TOPOSYNC_VISION_OFFICIAL_MODEL_SOURCE_DIR`: local directory that already contains the official ONNX files
- `TOPOSYNC_VISION_OFFICIAL_MODEL_BASE_URL`: base URL that exposes the official ONNX files by filename

When one of those sources is configured and the manifest has a checksum, the pipeline editor and processing server screen can start the install and show progress.
For legal safety, remote download sources are only enabled when the manifest says redistribution is allowed. The current built-in RTMDet manifests are intentionally marked as review-required for redistribution, so the supported default path is local copy from an admin-managed directory.
Product direction: keep RTMDet / RTMDet-Ins on `guided_upload`. RF-DETR is available through assisted local build only in this phase; it is not exposed as first-party `auto_download`, mirror, or bundle support.

`ModelManifest` also accepts optional `capabilities` such as `reid`. This does not enable multi-camera tracking yet; it only reserves the registry/catalog shape so future re-identification models can be added without changing the API.

## 2) Run the processing server

```bash
uv run toposync processing-serve --host 0.0.0.0 --port 9001 --data-dir .toposync-data
```

Default command values are `--host 127.0.0.1` and `--port 9001`.

## 3) Verify it is online

In another terminal:

```bash
curl http://127.0.0.1:9001/api/processing/status
```

You should get a JSON response. `active: false` is expected until the origin server pushes a pipeline config.

The status payload now includes:
- ONNX Runtime availability and execution providers
- registered/installed vision model manifests
- per-task model catalogs with availability/compatibility for the current machine
- install capability and active/recent install jobs for models with automatic sources configured
- model capabilities declared by each manifest
- RTMDet detection shortlist
- RF-DETR detection shortlist
- RTMDet-Ins segmentation shortlist
- a reserved `pose` task catalog/recommendation slot for future first-party pose models
- simple hardware-based recommendations for the initial detection/segmentation model

For custom models, the UI import flow is:
1. paste/import the manifest
2. point the ONNX artifact path on that machine
3. validate runtime compatibility
4. persist under `.toposync-data/vision-manifests/`
5. the model appears automatically in `vision.detect` or `vision.segment_instances`

In the pipeline editor, the basic configuration flow stays task-oriented and machine-aware. Advanced fields are hidden until the user explicitly opens advanced details.

`vision.pose_estimate` is scaffolded in this phase only. The processing server already understands `task=pose` manifests and diagnostics, but no first-party pose model/backend is enabled for end users yet.

## 4) Optional basic auth

The processing server can require HTTP Basic Auth:

```bash
TOPOSYNC_PROCESSING_USERNAME=dev \
TOPOSYNC_PROCESSING_PASSWORD=devpass \
uv run toposync processing-serve --host 0.0.0.0 --port 9001 --data-dir .toposync-data
```

Quick check:

```bash
curl -u dev:devpass http://127.0.0.1:9001/api/processing/status
```

## 5) Connect it to an origin server (distributed mode)

Start the origin backend:

```bash
uv run toposync serve --host 0.0.0.0 --port 8000 --data-dir .toposync-data
```

Register the processing server in origin:

```bash
curl -X PUT http://127.0.0.1:8000/api/processing-servers/dev_remote \
  -H 'content-type: application/json' \
  -d '{"id":"dev_remote","name":"Dev Remote","kind":"http","url":"http://127.0.0.1:9001","username":"","password":""}'
```

Then assign your final pipeline to `processing_server_id: "dev_remote"` (via UI or `PUT /api/pipelines/{pipeline_name}`).

Connection test through origin:

```bash
curl http://127.0.0.1:8000/api/processing-servers/dev_remote/status
```

## 6) Common issues

- `401 Unauthorized`: `TOPOSYNC_PROCESSING_USERNAME`/`TOPOSYNC_PROCESSING_PASSWORD` do not match the credentials saved in origin.
- Processing server always idle: pipeline `processing_server_id` is still `"local"`.
- Connection refused from origin: processing server is bound to `127.0.0.1` instead of a reachable interface (`0.0.0.0`).
- `vision.track` unavailable for `norfair`: the processing server environment is missing the `norfair` package or the extension install is stale.
- `vision.detect` or `vision.segment_instances` unavailable for a given model: the model manifest is missing/invalid, the ONNX artifact path does not exist, or ONNX Runtime cannot load the model/providers.
