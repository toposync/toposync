# `@toposync/plugin-api`

Public TypeScript contract for Toposync frontend extensions.

This package contains the stable host-side types used by Module Federation remotes, including:

- `TopoSyncHost`
- element types
- editor tools
- settings panels
- notification renderers
- i18n helpers

## Install

```bash
npm install @toposync/plugin-api react react-dom three
```

For TypeScript projects that render React UI, also install the usual type packages when needed:

```bash
npm install -D typescript @types/react @types/react-dom
```

## Usage

Use `import type` from the package and expose `activate(host)` from your remote entry:

```ts
import type { TopoSyncHost } from "@toposync/plugin-api";

export function activate(host: TopoSyncHost): void {
  host.registerSettingsPanel({
    id: "com.example.demo",
    name: { key: "ext.demo.settings.name", fallback: "Demo" },
    render: () => null,
  });
}
```

## Runtime model

`@toposync/plugin-api` is intentionally a types-first package. It ships a minimal runtime stub only so bundlers and package resolvers have a concrete entry point. Extension code should treat it as a contract package and import from it using `import type` whenever possible.

Editor tools may optionally declare `group` and `order` metadata. Hosts use those fields to organize the composition editor toolbar; tools without them remain compatible and fall back to the default group.

3D element and notification overlay `tick(deltaSeconds)` callbacks are render-on-demand aware. Return `true` while continuous frames are required, return `false` when the object can sleep, and call `ctx.requestRender?.()` after asynchronous loads or other out-of-band visual changes.

## Versioning

The package version follows the Toposync frontend/host release line. Third-party extensions should target the same minor line as the host they expect to run against.
