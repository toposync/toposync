# Rodar em dev

Pré‑requisitos: `uv`, Python 3.11+ e Node 20+.

## 1) Python (deps + extensões do repo em editable)

```bash
uv sync
```

Opcional (deps pesadas para detecção/tracking YOLO na extensão de câmeras):

```bash
uv sync --group cameras-yolo
```

Obs: `uv sync` é exato e remove pacotes fora dos grupos padrão; para manter o YOLO instalado, inclua `--group cameras-yolo` nas próximas sincronizações.

## 2) Node (workspaces)

```bash
npm install
```

## 3) Build das UIs das extensões (gera `static/remoteEntry.js` nos pacotes Python)

Atalho:

```bash
npm run build:extensions
```

Ou individual:

```bash
npm --workspace @toposync/extension-structural-ui run build
npm --workspace @toposync/extension-models-ui run build
npm --workspace @toposync/extension-home-assistant-ui run build
npm --workspace @toposync/extension-cameras-ui run build
npm --workspace @toposync/extension-images-ui run build
```

## 4) Rodar backend + frontend

Separado:

```bash
uv run toposync serve --data-dir .toposync-data
npm --workspace @toposync/frontend run dev
```

Atalho (um comando):

```bash
npm run dev
```

Abra `http://localhost:5173`.

## Rodar 2 instâncias em paralelo (sem conflito)

As portas padrão continuam:

- backend: `8000`
- frontend: `5173`
- processing server (quando usado): `9001`

Você pode sobrescrever por ambiente:

- `TOPOSYNC_BACKEND_PORT`
- `TOPOSYNC_FRONTEND_PORT`
- `TOPOSYNC_PROCESSING_PORT`
- `TOPOSYNC_DATA_DIR` (recomendado quando rodar duas instâncias)

`npm run dev` lê automaticamente variáveis de `.env` (ou de `TOPOSYNC_ENV_FILE`, se definido).

Exemplo usando o template:

```bash
cp .env.example .env
npm run dev
```

Exemplo de segunda instância local:

```bash
TOPOSYNC_BACKEND_PORT=8100 \
TOPOSYNC_FRONTEND_PORT=5174 \
TOPOSYNC_DATA_DIR=.toposync-data-2 \
npm run dev
```

Nesse caso, abra `http://localhost:5174`.

## Quando rodar o quê (atalho mental)

- Alterou código Python do core ou da extensão → reinicie `uv run toposync serve` (use o mesmo `--data-dir`, se estiver usando)
- Alterou UI do host → o `webpack-dev-server` recarrega (HMR)
- Alterou UI de uma extensão → rode `npm --workspace <ext-ui> run build` (ou use `--watch`) e dê refresh

## Testes E2E (Playwright)

1) Instalar deps Node:

```bash
npm install
```

2) Instalar o browser do Playwright (1x):

```bash
npx playwright install chromium
```

3) Rodar:

```bash
npm run test:e2e
```
