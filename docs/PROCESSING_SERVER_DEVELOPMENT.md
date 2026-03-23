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

Built-in first-party segmentation manifests currently target RTMDet-Ins:
- `rtmdet_ins_tiny`
- `rtmdet_ins_small`
- `rtmdet_ins_medium`

Their manifests are discovered automatically from `extensions/vision/manifests/`. Custom manifests can still be added with `TOPOSYNC_VISION_MANIFESTS_DIR` or `TOPOSYNC_VISION_MANIFEST_PATHS`, and manifests imported from the UI are stored under `.toposync-data/vision-manifests/` on that processing server.

Official RTMDet detection artifacts are local files under `extensions/vision/models/rtmdet/`. A validated manual provisioning flow for `rtmdet_det_tiny`, `rtmdet_det_small`, and `rtmdet_det_medium` is documented in [VISION_MODEL_PROVISIONING.md](VISION_MODEL_PROVISIONING.md).

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
- model capabilities declared by each manifest
- RTMDet detection shortlist
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
