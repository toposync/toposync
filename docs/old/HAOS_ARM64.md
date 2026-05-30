# HAOS on ARM64

Toposync supports Home Assistant OS add-on deployments on `aarch64` / `linux/arm64`.

The supported ARM target is 64-bit Home Assistant OS. The 32-bit architectures `armv7`, `armhf` and `i386` are not supported by Home Assistant Supervisor updates and are outside the Toposync support target.

## Recommended Hardware

Use Raspberry Pi 5 with 8 GB RAM and NVMe storage as the practical baseline for a modern Home Assistant deployment.

Raspberry Pi 4 and SD-card installations are best-effort. They are useful for compatibility checks, but not for judging camera or vision performance.

Streaming pass-through is expected to be reasonable on ARM64. Heavy RTSP decoding, OpenCV frame processing and ONNX Runtime CPU inference should be delegated to a separate processing server when possible.

## Published Add-on Scope

The Home Assistant add-on repository is:

```text
https://github.com/toposync/toposync-homeassistant-addon
```

The current add-on release line installs:

```text
toposync-streaming==0.7.2
```

The add-on itself may have its own version, for example `0.7.3`, because Home Assistant tracks the add-on package separately from the Python package installed inside the image.

The add-on is CPU-only. CUDA remains a separate Docker/package path for NVIDIA hosts and is not part of the HAOS ARM64 add-on.

## Local ARM64 Validation

Validate published dependencies without building local wheels:

```bash
docker run --rm --platform linux/arm64 python:3.12-slim-bookworm sh -lc \
  'python -m pip install --upgrade pip >/dev/null && python -m pip install --dry-run toposync-streaming==0.7.2'
```

Validate the local Docker runtime through QEMU/buildx:

```bash
python scripts/check_arm64_distribution.py
```

That script checks:

- pip dependency resolution for `toposync-streaming==0.7.2`
- `runtime-cpu` Docker build for `linux/arm64`
- container startup with `/data`
- `/api/health`
- `/api/extensions`
- bundled frontend shell
- `go2rtc` and FFmpeg availability in the streaming runtime image

## Home Assistant Testing Without Raspberry Pi Hardware

Use the Home Assistant add-on development devcontainer or a HAOS VM before buying hardware. The official devcontainer runs Supervisor and Home Assistant locally and maps local add-ons into the Local Add-ons repository.

For cross-build checks of the add-on repository:

```bash
docker build --platform linux/arm64 \
  --build-arg TOPOSYNC_PIP_SPEC=toposync-streaming==0.7.2 \
  -t local/toposync-addon-arm64 \
  /path/to/toposync-homeassistant-addon/toposync
```

## Processing Server Offload

Keep the Home Assistant add-on as the origin/UI/control plane on ARM64.

Move heavy work to a processing server when needed:

```bash
toposync processing-serve --host 0.0.0.0 --port 49321 --data-dir .toposync-processing
```

Then register that server in the origin and assign camera transmissions/pipelines to the same server id:

- transmission `host_server_id`: remote processing server id
- pipeline `processing_server_id`: same remote processing server id

This keeps the existing affinity rule intact: `stream.publish_video` must run on the same processing server that hosts the transmission.

Home Assistant native camera export supports remote transmission hosts through the origin server. The origin resolves the remote processing server, returns an RTSP URL for Home Assistant Core, and reports actionable blocking errors when the remote server URL, RTSP port or LAN reachability is not valid.

## References

- [Home Assistant app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [Home Assistant local app testing](https://developers.home-assistant.io/docs/apps/testing/)
- [Home Assistant app publishing](https://developers.home-assistant.io/docs/apps/publishing/)
- [Home Assistant OS on macOS](https://www.home-assistant.io/installation/macos/)
- [Home Assistant unsupported system architectures](https://www.home-assistant.io/more-info/unsupported/system_architecture/)
