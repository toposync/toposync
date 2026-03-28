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

Para gerar o bundle opcional de streaming:

```bash
uv build packages/toposync-streaming
```

Isso gera o pacote `toposync-streaming`, que depende do bundle padrão `toposync` + `toposync-ext-streaming`.

## Bundles alternativos de aceleração

Para gerar os bundles first-party de aceleração opcional:

```bash
uv build packages/toposync-vision-cuda
uv build packages/toposync-vision-directml
```

Eles geram respectivamente `toposync-vision-cuda` e `toposync-vision-directml`.

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
uv build extensions/streaming
```

Isso gera `extensions/<ext>/dist/*.whl`. Usuário final instala só o wheel (sem Node) e o app carrega o frontend prebuilt.

Para validar que cada wheel leva seus assets de runtime (`extension.json`, `static/`, manifests, dados e licenças), rode:

```bash
python scripts/check_extension_wheels.py
```

## Smoke test de distribuição

Para validar o fluxo de release fora do monorepo, rode:

```bash
python scripts/test_distribution_install.py
```

Esse smoke test:

- rebuilda o frontend host e as UIs das extensões padrão
- gera um wheelhouse local com `toposync-core`, `toposync` e as extensões padrão
- cria um venv limpo fora do checkout
- instala `toposync` com `pip` a partir dos wheels gerados
- sobe `toposync serve` usando apenas o ambiente instalado
- valida `/api/extensions`
- roda Playwright contra a UI host embutida para confirmar carregamento dos remotes

A CI executa esse mesmo fluxo no workflow [`.github/workflows/distribution-smoke.yml`](/Users/c/Projects/toposync-2/.github/workflows/distribution-smoke.yml).
