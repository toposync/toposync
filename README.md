<p align="center">
  <img src="frontend/src/assets/toposync-symbol.svg" alt="Toposync" width="96" />
</p>

# Toposync

Toposync is an open source, local-first platform for spatial home automation, cameras, visual context, extensions, and distributed processing.

It combines a Python/FastAPI backend, a React/ThreeJS frontend host, installable extension packages, camera and Home Assistant integrations, pipeline orchestration, and optional processing servers for heavier workloads.

> Toposync is currently **early access alpha**. Test it in contained environments and contained networks first. Do not rely on it yet for daily household operation, safety-critical automation, unattended security monitoring, emergency workflows, access control, or any automation where failure could cause harm, property damage, privacy exposure, or loss of essential service.

## What Toposync provides

- A local web app with API and frontend served by the same backend in production.
- A 2D and 3D spatial workspace for homes, rooms, models, images, cameras, and Home Assistant entities.
- A Python extension runtime with frontend remotes loaded by the host UI.
- A default CPU product bundle with structural, models, images, Home Assistant, cameras, vision, and spatial video extensions.
- ONNX Runtime CPU as the default vision path.
- Optional streaming, CUDA, DirectML, AI, Docker, Home Assistant add-on, and processing-server paths.

The core is intentionally generic. Domain-specific behavior belongs in extensions.

## Quick install

Python 3.12 is recommended.

```bash
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install toposync
toposync serve
```

Open:

```text
http://127.0.0.1:8000/
```

On Windows PowerShell:

```powershell
uv venv .venv --python 3.12
.venv\Scripts\Activate.ps1
uv pip install toposync
toposync serve
```

Optional upgrades:

```bash
uv pip install toposync-streaming
uv pip install toposync-vision-cuda
uv pip install toposync-vision-directml
```

Use CUDA for NVIDIA hosts and DirectML for Windows GPU acceleration. Streaming is optional because it brings additional media-runtime requirements.

See [Toposync installation](docs-site/docs/installation/choose-your-installation.mdx) for Python, Docker, CUDA, Home Assistant add-on, and processing-server scenarios.

## Home Assistant

Toposync can run as a Home Assistant add-on with sidebar ingress, supervised execution, direct-port access when enabled, and internal access to the Home Assistant Core API.

Use the dedicated add-on repository:

```text
https://github.com/toposync/toposync-homeassistant-addon
```

Start with [Home Assistant add-on installation](docs-site/docs/installation/home-assistant-addon.mdx). For Raspberry Pi and HAOS, treat the add-on as a lightweight origin server and delegate heavy vision or multi-camera processing to a processing server when needed.

## Development

Prerequisites:

- Python 3.12;
- `uv`;
- Node 20 or newer;
- npm.

From the repository root:

```bash
uv sync
npm install
npm run build:extensions
TOPOSYNC_AUTH_MODE=bypass npm run dev
```

Open:

```text
http://127.0.0.1:5173/
```

The default development data directory is `.toposync-data`.

See [Development setup](docs-site/docs/developers/development-setup.mdx) for the full local workflow.

## Repository map

- `src/toposync`: core backend, API, extension manager, pipeline runtime, processing server.
- `frontend`: React/ThreeJS frontend host.
- `packages/plugin-api`: public TypeScript contract for frontend extensions.
- `packages/toposync`: default Python product bundle.
- `packages/toposync-streaming`: streaming bundle.
- `packages/toposync-vision-cuda`: NVIDIA CUDA upgrade bundle.
- `packages/toposync-vision-directml`: Windows DirectML upgrade bundle.
- `extensions`: first-party extension packages.
- `docs-site`: Docusaurus documentation site.
- `integrations/home_assistant`: Home Assistant integration and add-on related assets.
- `scripts`: distribution, validation, and service helper scripts.

## Documentation

Documentation lives in `docs-site`.

Useful starting points:

- [Installation](docs-site/docs/installation/choose-your-installation.mdx)
- [Compatibility](docs-site/docs/installation/architecture-support.mdx)
- [Architecture](docs-site/docs/developers/architecture.mdx)
- [Extension authoring](docs-site/docs/developers/extension-authoring.mdx)
- [Plugin API](docs-site/docs/developers/plugin-api.mdx)
- [Pipelines](docs-site/docs/developers/pipelines.mdx)
- [Visual identity](docs-site/docs/developers/visual-identity.mdx)
- [Release process](docs-site/docs/developers/release-process.mdx)

Build the documentation site locally:

```bash
npm run docs:start
npm run docs:build
```

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

Short version:

- keep changes focused;
- preserve Home Assistant ingress paths;
- keep the core generic;
- put domain-specific behavior in extensions;
- do not commit secrets, runtime data, or private environment files;
- run the smallest checks that cover your change.

## Security and support

- Security reports: read [SECURITY.md](SECURITY.md) and do not open public issues for vulnerabilities.
- Support expectations: read [SUPPORT.md](SUPPORT.md).
- Funding: GitHub Sponsors is configured for this repository.

## License

Toposync is released under the [MIT License](LICENSE).
