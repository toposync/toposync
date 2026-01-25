# Toposync

Plataforma local‑first (Python + React + ThreeJS) para “gêmeo digital” de automação residencial — com **runtime de extensões** como alicerce.

O objetivo dessa base é resolver o “maior nó”: **extensões com backend Python + frontend TypeScript instaláveis via wheel** (sem exigir toolchain de build no usuário).

## Documentação

- Índice: `docs/README.md`
- Self‑hosting (produção): `docs/SELF_HOSTING.md`
- Rodar em dev: `docs/DEVELOPMENT.md`
- Extensões (runtime): `docs/EXTENSIONS_RUNTIME.md`
- Contrato TS / plugin API: `docs/PLUGIN_API.md`
- Criar uma extensão: `docs/EXTENSION_AUTHORING.md`

## Quickstart (dev)

Pré‑requisitos: `uv`, Python 3.11+ e Node 20+.

```bash
uv sync
npm install
npm run build:extensions
npm run dev
```

Abra `http://localhost:5173`.

