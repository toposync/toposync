# TopoSync

Plataforma local‑first (Python + React + ThreeJS) para “gêmeo digital” de automação residencial — com **runtime de extensões** como alicerce.

O objetivo dessa base é resolver o “maior nó”: **extensões com backend Python + frontend TypeScript instaláveis via wheel** (sem exigir toolchain de build no usuário). O core descobre extensões via **entry points** e o frontend carrega bundles em runtime via **Module Federation** (estilo “prebuilt extensions” do JupyterLab).

## Arquitetura (base)

- **Backend (Python)**: host de extensões, event bus async (com prevenção/substituição) e service registry.
- **Frontend (React/ThreeJS)**: host que consulta `/api/extensions` e carrega `remoteEntry.js` de cada extensão em runtime.
- **Extensão = pacote Python**: `extension.json` + entry point + `static/remoteEntry.js` embutido no wheel.
- **Contrato TS (host)**: registries para `tools`, `panels`, `overlays 3D` e `notification renderers` (ver `frontend/packages/plugin-api/index.d.ts`).

## Rodar (dev)

Pré‑requisitos: `uv`, Python 3.11+, Node 20+.

1) Dependências Python + venv:

```bash
uv sync
```

2) Dependências Node (workspaces):

```bash
npm install
```

3) Instale a extensão exemplo (editable):

```bash
uv pip install -e extensions/hello_lamp
```

4) Backend:

```bash
uv run toposync serve
```

5) Frontend host:

```bash
npm --workspace @toposync/frontend run dev
```

Abra `http://localhost:5173` e clique no “cubo lâmpada” (o core emite `device.action_requested`; a extensão intercepta e retorna o novo estado).

## Formato de uma extensão (mínimo)

Uma extensão “prebuilt” precisa:

- `pyproject.toml` com entry point em `toposync.extensions`
- `src/<pkg>/extension.json` (manifesto)
- `src/<pkg>/static/remoteEntry.js` (+ chunks, se houver)

O manifesto base está em `extensions/hello_lamp/src/toposync_ext_hello_lamp/extension.json`.

## Build/distribuição da extensão (wheel)

No exemplo, o bundle do frontend pode ser (re)gerado via:

```bash
npm --workspace @toposync/extension-hello-lamp-ui run build
```

E o wheel via:

```bash
uv build extensions/hello_lamp
```

Isso gera `extensions/hello_lamp/dist/*.whl`, que pode ser instalado sem Node.
