# Contrato TypeScript (plugin API)

O contrato público fica no pacote npm `@toposync/plugin-api`.

No monorepo, a fonte canônica continua em `frontend/packages/plugin-api/index.d.ts`.

Para extensões de terceiros, use o pacote publicado e prefira a mesma linha minor do host Toposync alvo.

Hoje, o host suporta:

- **Element types**: definem um tipo de elemento que pode entrar na composição:
  - como criar o objeto 3D (`create3D`)
  - como renderizar no editor 2D (`render2D`)
  - modal de ação (quando o objeto é clicado no 3D): `renderActionModal`
  - modal de edição (na tela de composição): `renderEditorModal`
  - agrupamento no painel de camadas (`layerGroup`, ex.: `walls` e `areas`)
- **Editor tools**: ferramentas selecionáveis na edição de composição (cada uma controla a interação no canvas)
- **Notification renderers**: como renderizar um tipo de notificação em card
- **Settings panels**: UI de configurações dentro do modal global (persistidas no backend em `settings.extensions[extension_id]`)

O modelo base de instância em uma composição é `CompositionElement`:

- `id`, `type`, `name`
- `position`/`rotation` (Vector3)
- `props` (objeto livre da extensão)

## Editor tools (rápido)

- O editor 2D envia eventos de ponteiro em coordenadas do “mundo” (plano X/Z).
- A ferramenta cria uma sessão (`createSession`) e recebe callbacks (`getElements`, `createElement`, `openEditor`, etc).
- Exemplos reais: `extensions/structural` (paredes/áreas) e `extensions/models` (importar GLB/GLTF).

Exemplo mínimo de ferramenta “clique para adicionar”:

```ts
host.registerEditorTool({
  id: "com.exemplo.tool.point",
  name: "Point",
  icon: "plus",
  group: { id: "other", name: "Other", order: 900 },
  order: 10,
  createSession: ({ createElement }) => ({
    onPointerEvent: (e) => {
      if (e.kind !== "down" || e.button !== 0) return
      createElement("com.exemplo.point", { position: { x: e.world.x, y: 0, z: e.world.z } })
    },
  }),
})
```

`group` e `order` são opcionais. O host usa esses campos para agrupar e ordenar ferramentas no editor de composição; ferramentas antigas sem esses campos continuam aparecendo no grupo padrão ao final.

## Instalação para extensões externas

```bash
npm install @toposync/plugin-api react react-dom three
npm install -D typescript @types/react @types/react-dom webpack webpack-cli ts-loader
```

O pacote é types-first. Use `import type` sempre que possível.

## Settings panels (rápido)

Extensões podem adicionar UI no modal global de configurações e persistir um blob JSON por extensão:

```ts
host.registerSettingsPanel({
  id: "com.exemplo.minha_ext",
  icon: "gear",
  name: { key: "ext.minha_ext.settings.name", fallback: "Minha Extensão" },
  render: ({ i18n, settings, updateSettings }) => (
    <button
      onClick={() => updateSettings({ enabled: true })}
    >
      {i18n.t("core.actions.save")}
    </button>
  ),
})
```

O backend salva em `config.json` em `settings.extensions["<id>"]`.

## Temas (via extensões)

Extensões podem registrar temas chamando `host.registerTheme()` no `activate(host)`. Um tema é basicamente um conjunto de overrides de CSS variables (ex.: `--bg`, `--accent`), aplicado no `:root`.

O tema ativo é escolhido em **Configurações → Base → Tema** e fica salvo no `localStorage`.

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
