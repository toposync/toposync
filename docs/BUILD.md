# Build (produção / distribuição)

## Backend (core)

Gera wheel/sdist do core:

```bash
uv build
```

## Frontend host (bundle)

Gera `frontend/dist`:

```bash
npm --workspace @toposync/frontend run build
```

## Extensões (wheel com assets)

1) Build do bundle JS da extensão (gera `static/` dentro do pacote Python):

```bash
npm run build:extensions
```

2) Build do wheel da extensão:

```bash
uv build extensions/structural
uv build extensions/models
uv build extensions/home_assistant
uv build extensions/cameras
uv build extensions/images
```

Isso gera `extensions/<ext>/dist/*.whl`. Usuário final instala só o wheel (sem Node) e o app carrega o frontend prebuilt.

