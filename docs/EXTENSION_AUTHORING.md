# Criando uma extensão

Uma extensão Toposync é um pacote Python que:

- se registra via entry point em `toposync.extensions`
- inclui um `extension.json`
- inclui um bundle frontend prebuilt em `static/remoteEntry.js` (Module Federation)

Referências:

- Runtime de extensões: `docs/EXTENSIONS_RUNTIME.md`
- Contrato TS (host): `docs/PLUGIN_API.md`

## Passo 1) Estruture o pacote Python

Crie uma pasta `extensions/minha_ext` com:

- `pyproject.toml`
- `src/toposync_ext_minha_ext/`
  - `__init__.py`
  - `plugin.py`
  - `extension.json`
  - `static/` (onde o build do frontend vai cair)

No `pyproject.toml`, declare o entry point:

```toml
[project.entry-points."toposync.extensions"]
minha_ext = "toposync_ext_minha_ext.plugin:MinhaExtensao"
```

Implemente um plugin (pode herdar de `BaseExtension`):

```py
from toposync.extensions import BaseExtension

class MinhaExtensao(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_minha_ext")
```

Opcionalmente, implemente `setup()` para registrar rotas e hooks:

- rotas FastAPI
- handlers no `EventBus`
- serviços no `ServiceRegistry`

## Passo 2) Crie o frontend prebuilt (Module Federation)

Crie `extensions/minha_ext/ui` com webpack configurado para:

- gerar `remoteEntry.js`
- expor `./activate`
- **output direto** em `extensions/minha_ext/src/toposync_ext_minha_ext/static/`
- depender de `@toposync/plugin-api` na mesma linha minor do host alvo

O `activate.tsx` deve exportar `activate(host)` e registrar coisas:

```ts
import type { TopoSyncHost } from "@toposync/plugin-api";

export function activate(host: TopoSyncHost) {
  host.registerElementType({
    type: "com.suaorg.minha_ext.meu_elemento",
    name: "Meu Elemento",
    // ...
  });
}
```

Para uma extensão fora deste monorepo, instale o contrato público:

```bash
npm install @toposync/plugin-api react react-dom three
```

## Passo 3) Build e instalação (dev)

1) Instale em editable:

```bash
uv pip install -e extensions/minha_ext
```

2) Build do bundle JS:

```bash
npm --workspace @toposync/extension-minha-ext-ui run build
```

3) Suba o backend + frontend (ver `docs/DEVELOPMENT.md`).

Na tela de **Editar composição**, seu elemento aparece em “Elementos disponíveis”.

## Passo 4) Distribuir sem toolchain

1) garanta que `static/` está preenchido (build JS)
2) gere o wheel:

```bash
uv build extensions/minha_ext
```

Usuário final instala `*.whl` e o host carrega `remoteEntry.js` servido pelo backend.

## Formato mínimo (checklist)

Uma extensão “prebuilt” precisa:

- `pyproject.toml` com entry point em `toposync.extensions`
- `src/<pkg>/extension.json` (manifesto)
- `src/<pkg>/static/remoteEntry.js` (+ chunks, se houver)

Se a extensão tiver outros assets de runtime, como manifests, modelos, templates, binários auxiliares ou licenças, eles também precisam estar dentro do pacote importável (`src/<pkg>/...`) ou ser copiados para lá no build do wheel. A extensão instalada nunca deve depender de caminhos do checkout do repo.

O manifesto base está em `extensions/structural/src/toposync_ext_structural/extension.json`.
