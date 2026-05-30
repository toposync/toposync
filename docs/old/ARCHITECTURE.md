# Arquitetura

## Base

- **Backend (Python)**: host de extensões (entry points), event bus async (com prevenção/substituição), service registry e servidor de assets estáticos das extensões.
- **Frontend (React/ThreeJS)**: host que consulta `/api/extensions` e carrega `remoteEntry.js` de cada extensão em runtime (Module Federation).
- **Extensão = pacote Python**: `extension.json` + entry point + `static/remoteEntry.js` embutido no wheel.
- **Contrato TS (host)**: extensões registram `element types` (objeto 3D + modais de ação/edição), `notification renderers`, `settings panels` e **temas** (ver `frontend/packages/plugin-api/index.d.ts`).

## Estrutura do repo

- `src/toposync`: backend core (FastAPI) + runtime de extensões
- `frontend`: app host (React/Three + webpack)
- `extensions/structural`: extensão “first‑party” (paredes/áreas: ferramentas 2D + render 3D)
- `extensions/models`: extensão “first‑party” (importar GLB/GLTF + prévia 2D + render 3D)
- `extensions/home_assistant`: extensão “first‑party” (scaffold: configurar servidores Home Assistant)
- `extensions/cameras`: extensão “first‑party” (RTSP snapshots + processamento local/remoto + detecções)
- `extensions/images`: extensão “first‑party” (importar imagens como sobreposição ou decalque)

