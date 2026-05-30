import { resolveToposyncUrl, type ToposyncHost } from "@toposync/plugin-api";

declare const __webpack_init_sharing__: (scope: string) => Promise<void>;
declare const __webpack_share_scopes__: { default: unknown };

type Container = {
  init(shareScope: unknown): Promise<void>;
  get(module: string): Promise<() => any>;
};

const loaded = new Map<string, Promise<void>>();

function loadRemoteEntry(remoteEntryUrl: string): Promise<void> {
  if (loaded.has(remoteEntryUrl)) return loaded.get(remoteEntryUrl)!;

  const p = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = resolveToposyncUrl(remoteEntryUrl);
    script.type = "text/javascript";
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Failed to load ${remoteEntryUrl}`));
    document.head.appendChild(script);
  });

  loaded.set(remoteEntryUrl, p);
  return p;
}

export async function loadRemoteActivate(
  remoteEntryUrl: string,
  scope: string,
  module: string,
): Promise<(host: ToposyncHost) => void | Promise<void>> {
  await __webpack_init_sharing__("default");
  await loadRemoteEntry(remoteEntryUrl);

  const container = (window as any)[scope] as Container | undefined;
  if (!container) throw new Error(`Remote container "${scope}" not found on window`);

  try {
    await container.init(__webpack_share_scopes__.default);
  } catch {
    // ignore: container may be already initialized
  }

  const factory = await container.get(module);
  const mod = factory();
  if (!mod?.activate) throw new Error(`Remote module "${scope}/${module}" has no activate()`);
  return mod.activate;
}
