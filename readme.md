# TopoSync

Plataforma local‑first (Python + React + ThreeJS) para “gêmeo digital” de automação residencial — com **runtime de extensões** como alicerce.

O objetivo dessa base é resolver o “maior nó”: **extensões com backend Python + frontend TypeScript instaláveis via wheel** (sem exigir toolchain de build no usuário).

## Arquitetura (base)

- **Backend (Python)**: host de extensões (entry points), event bus async (com prevenção/substituição), service registry e servidor de assets estáticos das extensões.
- **Frontend (React/ThreeJS)**: host que consulta `/api/extensions` e carrega `remoteEntry.js` de cada extensão em runtime (Module Federation).
- **Extensão = pacote Python**: `extension.json` + entry point + `static/remoteEntry.js` embutido no wheel.
- **Contrato TS (host)**: extensões registram `element types` (objeto 3D + modais de ação/edição) e `notification renderers` (ver `frontend/packages/plugin-api/index.d.ts`).

## Estrutura do repo

- `src/toposync`: backend core (FastAPI) + runtime de extensões
- `frontend`: app host (React/Three + webpack)
- `extensions/hello_lamp`: extensão exemplo (Python + UI prebuilt)

## Rodar (dev)

Pré‑requisitos: `uv`, Python 3.11+ e Node 20+.

1) Python (cria `.venv`, resolve e instala deps):

```bash
uv sync
```

2) Node (workspaces):

```bash
npm install
```

3) Instale a extensão exemplo (editable, para o backend descobrir via entry point):

```bash
uv pip install -e extensions/hello_lamp
```

4) Build do frontend da extensão (gera `static/remoteEntry.js` dentro do pacote Python da extensão):

```bash
npm --workspace @toposync/extension-hello-lamp-ui run build
```

5) Backend:

```bash
uv run toposync serve --data-dir $(pwd)/.toposync-data
```

6) Frontend host (dev server com proxy para o backend):

```bash
npm --workspace @toposync/frontend run dev
```

Abra `http://localhost:5173`, clique em **Editar**, adicione a **Lâmpada (Hello Lamp)** e volte para a tela principal. Clique no objeto 3D para abrir o modal de ação.

### Quando rodar o quê (atalho mental)

- Alterou código Python do core ou da extensão → reinicie `uv run toposync serve` (use o mesmo `--data-dir`, se estiver usando)
- Alterou UI do host → o `webpack-dev-server` recarrega (HMR)
- Alterou UI de uma extensão → rode `npm --workspace <ext-ui> run build` (ou use `--watch`) e dê refresh

## Build (produção / distribuição)

### Backend (core)

Gera wheel/sdist do core:

```bash
uv build
```

### Frontend host (bundle)

Gera `frontend/dist`:

```bash
npm --workspace @toposync/frontend run build
```

### Extensões (wheel com assets)

1) Build do bundle JS da extensão (gera `static/`):

```bash
npm --workspace @toposync/extension-hello-lamp-ui run build
```

2) Build do wheel da extensão:

```bash
uv build extensions/hello_lamp
```

Isso gera `extensions/hello_lamp/dist/*.whl`. Usuário final instala só o wheel (sem Node) e o app carrega o frontend prebuilt.

## Como as extensões funcionam

### 1) Descoberta no backend (entry points)

Cada extensão declara um entry point em `toposync.extensions`. O core descobre tudo via `importlib.metadata.entry_points()` e instancia o plugin.

Exemplo (`extensions/hello_lamp/pyproject.toml`):

```toml
[project.entry-points."toposync.extensions"]
hello_lamp = "toposync_ext_hello_lamp.plugin:HelloLampExtension"
```

### 2) Manifesto (`extension.json`)

O core lê `extension.json` via `importlib.resources` dentro do pacote da extensão. Esse manifesto é o contrato mínimo e é versionado por `schema_version`.

Exemplo:

```json
{
  "schema_version": 1,
  "id": "com.suaorg.minha_ext",
  "name": "Minha Extensão",
  "version": "0.1.0",
  "frontend": {
    "kind": "module-federation",
    "remote_entry": "remoteEntry.js",
    "scope": "minha_ext",
    "module": "./activate"
  }
}
```

### 3) Servir assets estáticos

Como o bundle do frontend está dentro do wheel, o backend expõe:

- `/extensions/<extension_id>/<path>`

E o endpoint `/api/extensions` retorna, para cada extensão, um `frontend.remote_entry_url` já pronto (relativo, para funcionar atrás de proxy).

### 4) Carregamento no frontend (runtime)

O host faz:

1) `GET /api/extensions`
2) para cada extensão com `frontend.kind = "module-federation"`:
   - injeta o script `remoteEntry.js`
   - inicializa o share scope (`__webpack_init_sharing__`)
   - chama `container.get("./activate")`
   - executa `activate(host)`

Ou seja: instalar o wheel = instalar backend + UI prebuilt, e o host “puxa” isso automaticamente.

## Contrato TypeScript (o que uma extensão pode entregar)

O contrato fica em `frontend/packages/plugin-api/index.d.ts`.

Hoje, o host suporta:

- **Element types**: definem um tipo de elemento que pode entrar na composição:
  - como criar o objeto 3D (`create3D`)
  - modal de ação (quando o objeto é clicado no 3D): `renderActionModal`
  - modal de edição (na tela de composição): `renderEditorModal`
- **Notification renderers**: como renderizar um tipo de notificação em card

O modelo base de instância em uma composição é `CompositionElement`:

- `id`, `type`, `name`
- `position`/`rotation` (Vector3)
- `props` (objeto livre da extensão)

## i18n (en + pt-BR)

O core suporta i18n no frontend (e extensões) com um dicionário simples por chaves.

- Idiomas suportados: `en` e `pt-BR`
- Como o idioma é escolhido:
  - se existir `localStorage["toposync.locale"]`, ele é usado
  - senão: `navigator.language` (`pt*` vira `pt-BR`, o resto vira `en`)
- Para trocar rápido (dev): `localStorage.setItem("toposync.locale", "en"); location.reload()`

### Como uma extensão usa

No `activate(host)`:

1) Registre as traduções:

```ts
host.i18n.registerTranslations({
  en: { "ext.minha_ext.element.name": "Camera" },
  "pt-BR": { "ext.minha_ext.element.name": "Câmera" },
})
```

2) Use `LocalizedString` em `ElementType.name/description` para o host renderizar corretamente:

```ts
host.registerElementType({
  type: "com.exemplo.camera",
  name: { key: "ext.minha_ext.element.name", fallback: "Camera" },
  // ...
})
```

3) Dentro de componentes React da extensão, use `host.i18n.useI18n()` para re-renderizar quando o idioma mudar:

```ts
function MyAction({ i18n }: { i18n: HostI18n }) {
  const { t } = i18n.useI18n()
  return <button>{t("core.actions.close")}</button>
}
```

Dica: o core já fornece chaves comuns em `core.actions.*` (ex.: `close`, `delete`, `edit`).

## Persistência (local-first)

- O backend salva a configuração em um único arquivo: `<data_dir>/config.json`
- Arquivos auxiliares do usuário ficam em: `<data_dir>/files/`
- Para definir o diretório explicitamente use `TOPOSYNC_DATA_DIR=/caminho/para/dados` (ou `toposync serve --data-dir ...`)
- Dica (dev): `uv run toposync serve --data-dir $(pwd)/.toposync-data` (pasta ignorada pelo git)
- Default por SO:
  - Linux: `$XDG_DATA_HOME/toposync` ou `~/.local/share/toposync`
  - macOS: `~/Library/Application Support/TopoSync`
  - Windows: `%APPDATA%/TopoSync`

Se bater dúvida sobre *qual* diretório está em uso, chame `GET /api/system/paths`.

O frontend lê/salva a composição via `GET/PUT /api/composition`. Versões antigas usavam `localStorage` (`toposync.composition.v1`) e o app tenta migrar automaticamente quando o backend está vazio.

## Guia rápido: criando uma extensão nova

### Passo 1) Estruture o pacote Python

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

### Passo 2) Crie o frontend prebuilt (Module Federation)

Crie `extensions/minha_ext/ui` com webpack configurado para:

- gerar `remoteEntry.js`
- expor `./activate`
- **output direto** em `extensions/minha_ext/src/toposync_ext_minha_ext/static/`

O `activate.tsx` deve exportar `activate(host)` e registrar coisas:

```ts
import type { TopoSyncHost } from "@toposync/plugin-api";

export function activate(host: TopoSyncHost) {
  host.registerElementType({
    type: "com.suaorg.minha_ext.meu_elemento",
    name: "Meu Elemento",
    create3D: ({ THREE }, element) => {
      const mesh = new THREE.Mesh(
        new THREE.BoxGeometry(0.4, 0.4, 0.4),
        new THREE.MeshStandardMaterial({ color: 0x64748b }),
      );
      return { object: mesh };
    },
    renderActionModal: ({ element, close }) => (
      <div>
        <div>Você clicou em: {element.name}</div>
        <button onClick={close}>Fechar</button>
      </div>
    ),
  });
}
```

### Passo 3) Build e instalação (dev)

1) Instale em editable:

```bash
uv pip install -e extensions/minha_ext
```

2) Build do bundle JS:

```bash
npm --workspace @toposync/extension-minha-ext-ui run build
```

3) Suba o backend + frontend.

Na tela de **Editar composição**, seu elemento aparece em “Elementos disponíveis”.

### Passo 4) Distribuir sem toolchain

1) garanta que `static/` está preenchido (build JS)
2) gere o wheel:

```bash
uv build extensions/minha_ext
```

Usuário final instala `*.whl` e o host carrega `remoteEntry.js` servido pelo backend.

## Backend: hooks e serviços (base)

O core expõe (para evoluir):

- `EventBus`: pipeline async com prioridade e `EventOutcome` (stop propagation, prevent default, override payload/result)
- `ServiceRegistry`: registry simples para desacoplar extensões por ID

Ponto de partida de evento (exemplo atual): `device.action_requested`.

## Troubleshooting

- **Module Federation + HMR**: se aparecer `Shared module is not available for eager consumption`, o host deve inicializar `__webpack_init_sharing__("default")` antes de importar React (o projeto já usa o padrão `bootstrap`).
- **Mudou UI da extensão e não refletiu**: rode o build de novo do workspace da extensão e dê refresh (o backend só serve arquivos estáticos).

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
uv run toposync serve --data-dir $(pwd)/.toposync-data
```

5) Frontend host:

```bash
npm --workspace @toposync/frontend run dev
```

Abra `http://localhost:5173`, clique em **Editar**, adicione a **Lâmpada (Hello Lamp)** e volte para a tela principal. Clique no objeto 3D para abrir o modal de ação.

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
