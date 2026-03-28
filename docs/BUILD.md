# Build (produção / distribuição)

## Backend (core)

Gera wheel/sdist do core:

```bash
uv build
```

Isso gera o pacote `toposync-core`.

Durante o build, o hook do core garante que o frontend host entre no artefato. Se `frontend/dist` ainda não existir, ele tenta gerar esse bundle com:

```bash
npm run build:frontend
```

O wheel/sdist final já sai com a UI host embutida dentro do pacote Python.

## Produto (bundle padrão)

Gera o wheel do pacote instalável por usuário final:

```bash
uv build packages/toposync
```

Isso gera o pacote `toposync`, que depende de `toposync-core` + extensões padrão.

## Frontend host (override opcional)

Se você quiser rebuildar manualmente o bundle do host antes do `uv build`:

```bash
npm run build:frontend
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
