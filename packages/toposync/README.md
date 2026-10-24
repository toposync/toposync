# Toposync

Default Toposync application bundle.

This distribution installs:

- `toposync-core`
- `toposync-ext-structural`
- `toposync-ext-models`
- `toposync-ext-home-assistant`
- `toposync-ext-images`
- `toposync-ext-cameras`
- `toposync-ext-vision`
- `toposync-ext-ptz-attention`
- `toposync-ext-spatial-video`
- `onnxruntime`

The official first-party vision runtime in this bundle is ONNX Runtime on CPU by default.

Development bundles pin the matching local core and Vision versions. Build and install
their wheels together; mixing these bundles with wheels from an older release is unsupported.

Alternative first-party bundles:

- `toposync-vision-cuda` for NVIDIA CUDA environments
- `toposync-vision-directml` for Windows DirectML environments

Optional first-party add-on bundles:

- `toposync-streaming` for the MediaMTX/FFmpeg streaming stack
