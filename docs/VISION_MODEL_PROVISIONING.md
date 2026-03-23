# Vision Model Provisioning

This guide provisions the official first-party RTMDet detection shortlist locally for Toposync.

Provisioned model ids:
- `rtmdet_det_tiny`
- `rtmdet_det_small`
- `rtmdet_det_medium`

The ONNX artifacts are local machine assets. They are intentionally not versioned in git because the manifests already mark redistribution as review-required.

## Where the files must end up

Toposync expects these files:

```text
extensions/vision/models/rtmdet/rtmdet_det_tiny.end2end.onnx
extensions/vision/models/rtmdet/rtmdet_det_small.end2end.onnx
extensions/vision/models/rtmdet/rtmdet_det_medium.end2end.onnx
```

After the files exist there, the local processing server will automatically report them as `artifact_exists: true`.

## Step by step

This is the exact export recipe validated on this repository on March 22, 2026.

### Option A: macOS with OrbStack

1. Start OrbStack:

```bash
orbctl start
```

2. Clone the official source repos in a temporary location:

```bash
git clone https://github.com/open-mmlab/mmdeploy /tmp/mmdeploy
git clone https://github.com/open-mmlab/mmdetection /tmp/mmdetection
```

3. Install `uv` inside the Linux VM and create the export environment:

```bash
orb bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
orb bash -lc 'source ~/.local/bin/env && uv python install 3.12'
orb bash -lc "sudo apt-get update && sudo apt-get install -y build-essential python3-dev ninja-build git pkg-config libopenblas-dev libgl1 libglib2.0-0"
orb bash -lc 'source ~/.local/bin/env && uv venv --python 3.12 /tmp/rtmexport251'
orb bash -lc 'source ~/.local/bin/env && source /tmp/rtmexport251/bin/activate && uv pip install --python /tmp/rtmexport251/bin/python pip "setuptools<81" wheel'
orb bash -lc 'source ~/.local/bin/env && source /tmp/rtmexport251/bin/activate && uv pip install --python /tmp/rtmexport251/bin/python torch==2.5.1 torchvision==0.20.1 mmengine==0.10.7 mmdet==3.3.0 onnx onnxruntime onnxsim aenum grpcio multiprocess prettytable protobuf==3.20.2'
orb bash -lc 'source /tmp/rtmexport251/bin/activate && pip install --no-build-isolation --force-reinstall --no-binary mmcv mmcv==2.1.0'
```

4. Download the official checkpoints:

```bash
mkdir -p /tmp/rtmdet-export/checkpoints /tmp/rtmdet-export/work

curl -L --fail -o /tmp/rtmdet-export/checkpoints/rtmdet_tiny_8xb32-300e_coco_20220902_112414-78e30dcc.pth \
  https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_tiny_8xb32-300e_coco/rtmdet_tiny_8xb32-300e_coco_20220902_112414-78e30dcc.pth

curl -L --fail -o /tmp/rtmdet-export/checkpoints/rtmdet_s_8xb32-300e_coco_20220905_161602-387a891e.pth \
  https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_s_8xb32-300e_coco/rtmdet_s_8xb32-300e_coco_20220905_161602-387a891e.pth

curl -L --fail -o /tmp/rtmdet-export/checkpoints/rtmdet_m_8xb32-300e_coco_20220719_112220-229f527c.pth \
  https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_m_8xb32-300e_coco/rtmdet_m_8xb32-300e_coco_20220719_112220-229f527c.pth
```

5. Export the ONNX models with the official MMDeploy config:

```bash
orb bash -lc 'source /tmp/rtmexport251/bin/activate && export PYTHONPATH=/mnt/mac/private/tmp/mmdeploy:/mnt/mac/private/tmp/mmdetection && python /mnt/mac/private/tmp/mmdeploy/tools/deploy.py /mnt/mac/private/tmp/mmdeploy/configs/mmdet/detection/detection_onnxruntime_static.py /mnt/mac/private/tmp/mmdetection/configs/rtmdet/rtmdet_tiny_8xb32-300e_coco.py /mnt/mac/private/tmp/rtmdet-export/checkpoints/rtmdet_tiny_8xb32-300e_coco_20220902_112414-78e30dcc.pth /mnt/mac/private/tmp/mmdeploy/demo/resources/det.jpg --work-dir /mnt/mac/private/tmp/rtmdet-export/work/rtmdet_det_tiny --device cpu --log-level INFO'

orb bash -lc 'source /tmp/rtmexport251/bin/activate && export PYTHONPATH=/mnt/mac/private/tmp/mmdeploy:/mnt/mac/private/tmp/mmdetection && python /mnt/mac/private/tmp/mmdeploy/tools/deploy.py /mnt/mac/private/tmp/mmdeploy/configs/mmdet/detection/detection_onnxruntime_static.py /mnt/mac/private/tmp/mmdetection/configs/rtmdet/rtmdet_s_8xb32-300e_coco.py /mnt/mac/private/tmp/rtmdet-export/checkpoints/rtmdet_s_8xb32-300e_coco_20220905_161602-387a891e.pth /mnt/mac/private/tmp/mmdeploy/demo/resources/det.jpg --work-dir /mnt/mac/private/tmp/rtmdet-export/work/rtmdet_det_small --device cpu --log-level INFO'

orb bash -lc 'source /tmp/rtmexport251/bin/activate && export PYTHONPATH=/mnt/mac/private/tmp/mmdeploy:/mnt/mac/private/tmp/mmdetection && python /mnt/mac/private/tmp/mmdeploy/tools/deploy.py /mnt/mac/private/tmp/mmdeploy/configs/mmdet/detection/detection_onnxruntime_static.py /mnt/mac/private/tmp/mmdetection/configs/rtmdet/rtmdet_m_8xb32-300e_coco.py /mnt/mac/private/tmp/rtmdet-export/checkpoints/rtmdet_m_8xb32-300e_coco_20220719_112220-229f527c.pth /mnt/mac/private/tmp/mmdeploy/demo/resources/det.jpg --work-dir /mnt/mac/private/tmp/rtmdet-export/work/rtmdet_det_medium --device cpu --log-level INFO'
```

6. Copy the exported ONNX files into the repository:

```bash
mkdir -p extensions/vision/models/rtmdet

cp /tmp/rtmdet-export/work/rtmdet_det_tiny/end2end.onnx \
  extensions/vision/models/rtmdet/rtmdet_det_tiny.end2end.onnx

cp /tmp/rtmdet-export/work/rtmdet_det_small/end2end.onnx \
  extensions/vision/models/rtmdet/rtmdet_det_small.end2end.onnx

cp /tmp/rtmdet-export/work/rtmdet_det_medium/end2end.onnx \
  extensions/vision/models/rtmdet/rtmdet_det_medium.end2end.onnx
```

7. Verify from Toposync:

```bash
curl -fsS http://127.0.0.1:8000/api/processing-servers/local/status | jq '.status.vision.models_installed[] | select(.task=="detection") | {model_id,artifact_exists}'
```

Expected result:

```json
{"model_id":"rtmdet_det_tiny","artifact_exists":true}
{"model_id":"rtmdet_det_small","artifact_exists":true}
{"model_id":"rtmdet_det_medium","artifact_exists":true}
```

### Option B: native Linux

Use the same steps, but run them directly on Linux without the `orb bash -lc` wrapper and adjust the paths:
- `/mnt/mac/private/tmp/mmdeploy` -> `/tmp/mmdeploy`
- `/mnt/mac/private/tmp/mmdetection` -> `/tmp/mmdetection`
- `/mnt/mac/private/tmp/rtmdet-export/...` -> `/tmp/rtmdet-export/...`

## Expected hashes

These are the hashes generated by the validated export flow above:

```text
7581382585401922c870b3e25080dd1215c7048c632c72499dd5d8e7f9e8fddf  rtmdet_det_tiny.end2end.onnx
e23a7b1ec6b7d7a2bd82b825e6702b7686af95c711ac1aa59655a3dfddf36c7c  rtmdet_det_small.end2end.onnx
f89e0b0e87f4cb93260ef62a187dfa71fa4dc5fdaf4173e5eeeaa624498fe18e  rtmdet_det_medium.end2end.onnx
```

## After provisioning

For a normal user, the practical flow is:
1. Open the processing server status or pipeline editor.
2. Confirm the chosen detection model appears as available.
3. In `vision.detect`, pick `RTMDet Small` for general use, or `RTMDet Tiny` on weaker machines.
4. Save the pipeline.
5. If a pipeline was previously disabled only because the model was missing, re-enable it.
