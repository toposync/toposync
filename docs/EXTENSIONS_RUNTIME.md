# Extensões (runtime)

## 1) Descoberta no backend (entry points)

Cada extensão declara um entry point em `toposync.extensions`. O core descobre tudo via `importlib.metadata.entry_points()` e instancia o plugin.

Exemplo (`extensions/structural/pyproject.toml`):

```toml
[project.entry-points."toposync.extensions"]
structural = "toposync_ext_structural.plugin:StructuralExtension"
```

## 2) Manifesto (`extension.json`)

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

## 3) Servir assets estáticos

Como o bundle do frontend está dentro do wheel, o backend expõe:

- `/extensions/<extension_id>/<path>`

E o endpoint `/api/extensions` retorna, para cada extensão, um `frontend.remote_entry_url` já pronto (relativo, para funcionar atrás de proxy).

## 4) Carregamento no frontend (runtime)

O host faz:

1) `GET /api/extensions`
2) para cada extensão com `frontend.kind = "module-federation"`:
   - injeta o script `remoteEntry.js`
   - inicializa o share scope (`__webpack_init_sharing__`)
   - chama `container.get("./activate")`
   - executa `activate(host)`

Ou seja: instalar o wheel = instalar backend + UI prebuilt, e o host “puxa” isso automaticamente.

## Backend: hooks e serviços (base)

O core expõe (para evoluir):

- `EventBus`: pipeline async com prioridade e `EventOutcome` (stop propagation, prevent default, override payload/result)
- `ServiceRegistry`: registry simples para desacoplar extensões por ID

Ponto de partida de evento (exemplo atual): `device.action_requested`.

