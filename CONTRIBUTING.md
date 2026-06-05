# Contributing to Toposync

Toposync is an open source, local-first platform for home context, cameras, spatial data, automation, extensions, and distributed processing.

The project is still pre-1.0 alpha. Contributions are welcome, but changes should be focused, careful, and easy to validate. Packaging, Home Assistant, camera, streaming, authentication, and processing-server changes can affect real installations, so document your assumptions and run the smallest checks that cover the risk.

## Before you start

Install:

- Python 3.12 recommended;
- `uv`;
- Node 20 or newer;
- npm.

The repository uses `package-lock.json` and npm workspaces. Do not use Yarn or pnpm for repository-level dependency changes.

For the complete local workflow, see [Development setup](docs-site/docs/developers/development-setup.mdx).

## Repository setup

From the repository root:

```bash
uv sync
npm install
npm run build:extensions
```

`uv sync` installs the Python core, development dependencies, and first-party extensions in editable mode. `npm install` installs the frontend host, plugin API package, extension UI workspaces, documentation site, and test tooling.

## Run the app locally

For the normal development loop:

```bash
TOPOSYNC_AUTH_MODE=bypass npm run dev
```

Open:

```text
http://127.0.0.1:5173/
```

The default development data directory is `.toposync-data`. Use a separate `TOPOSYNC_DATA_DIR` when running multiple local instances.

## Working areas

- Core backend and runtime live under `src/toposync`.
- Frontend host code lives under `frontend`.
- First-party Python extensions live under `extensions/*`.
- Extension frontend bundles live under `extensions/*/ui`.
- The public documentation site lives under `docs-site`.
- Processing-server behavior is part of the core runtime and extension pipeline model.
- Home Assistant add-on user documentation lives in `docs-site/docs/home-assistant-addon` and installation docs live in `docs-site/docs/installation`.

For related references, see [Extension authoring](docs-site/docs/developers/extension-authoring.mdx), [Plugin API](docs-site/docs/developers/plugin-api.mdx), and [Pipelines](docs-site/docs/developers/pipelines.mdx).

## Development rules

- Keep the core generic. Domain-specific behavior belongs in extensions.
- Preserve Home Assistant ingress paths for frontend routes, links, API calls, event streams, WebSockets, extension assets, and file URLs.
- Do not commit local runtime data, generated secrets, credentials, tokens, or private environment files.
- Do not commit build artifacts unless they are intentionally packaged source assets.
- Prefer targeted changes over broad rewrites.
- Keep current contracts clean in pre-release areas: do not add compatibility
  layers for obsolete config, payload, pipeline, or API shapes by default.
- Preserve user data and add explicit migrations only for established
  user-facing contracts or when a release note calls that out.

## Checks

Run the smallest checks that cover your change.

Python:

```bash
uv run pytest
uv run pytest tests/test_extension_manager_compatibility.py
```

Frontend host:

```bash
npm --workspace @toposync/frontend run typecheck
```

Plugin API:

```bash
npm run typecheck:plugin-api
npm run pack:plugin-api
```

Extension UI bundles:

```bash
npm run build:extensions
```

Documentation:

```bash
npm run docs:build
```

Distribution and platform checks:

```bash
python scripts/test_distribution_install.py
python scripts/check_arm64_distribution.py
```

Use distribution checks when changing packaging, dependency pins, entry points, package data, frontend embedding, Docker, Home Assistant add-on behavior, or installation docs.

## Documentation changes

Public documentation lives in `docs-site`.

Use English as the primary language. When a page already exists in both English and `pt-BR`, update both versions in the same change unless there is a deliberate reason not to.

Run:

```bash
npm run docs:build
```

Do not migrate raw legacy notes directly into the public site. Curate them into current, user-facing documentation.

## Pull requests

Keep pull requests small and focused.

A good pull request includes:

- what changed and why;
- tests or checks run;
- screenshots or short recordings for UI changes;
- installation, distribution, Home Assistant, or processing-server impact when relevant;
- compatibility, removal, or migration notes when behavior changes.

Avoid mixing unrelated refactors with functional changes.

## Commit messages

Use short conventional summaries, for example:

```text
feat: add camera import flow
fix: preserve ingress path for stream diagnostics
docs: update installation guide
test: cover extension compatibility checks
refactor: simplify pipeline telemetry storage
i18n: translate developer docs
```

Add a scope only when it improves clarity. Do not add a trailing period.

## Release changes

Do not publish packages, bump release trains, or change public release metadata without following [Release process](docs-site/docs/developers/release-process.mdx).

Release-related changes must be reproducible, auditable, and validated against clean installs.

## Security

Do not report vulnerabilities in public issues.

Follow [Security Policy](SECURITY.md). Use private GitHub vulnerability reporting or a private GitHub security advisory when available. Do not include credentials, tokens, private network details, or exploit instructions in public discussions.
