// Import the helper directly from package source so this file can run before
// webpack module sharing is initialized in development.
import { resolveToposyncUrl } from "../../packages/plugin-api/basePath";

declare global {
  interface Window {
    __toposyncNetworkShimsInstalled?: boolean;
  }
}

function resolveRequestInput(input: RequestInfo | URL): string | null {
  if (typeof input === "string") return resolveToposyncUrl(input);
  if (input instanceof URL) return resolveToposyncUrl(input.toString());
  if (typeof Request !== "undefined" && input instanceof Request) return resolveToposyncUrl(input.url);
  return null;
}

export function installRuntimeNetworkShims(): void {
  if (typeof window === "undefined") return;
  if (window.__toposyncNetworkShimsInstalled) return;
  window.__toposyncNetworkShimsInstalled = true;

  const originalFetch = window.fetch.bind(window);
  window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const resolved = resolveRequestInput(input);
    if (!resolved) return originalFetch(input as any, init);
    if (typeof input !== "string" && !(input instanceof URL) && typeof Request !== "undefined" && input instanceof Request) {
      return originalFetch(new Request(resolved, input), init);
    }
    return originalFetch(resolved, init);
  }) as typeof window.fetch;

  const OriginalEventSource = window.EventSource;
  if (typeof OriginalEventSource === "function") {
    class PatchedEventSource extends OriginalEventSource {
      constructor(url: string | URL, eventSourceInitDict?: EventSourceInit) {
        super(resolveToposyncUrl(String(url)), eventSourceInitDict);
      }
    }
    Object.defineProperty(PatchedEventSource, "CONNECTING", { value: OriginalEventSource.CONNECTING });
    Object.defineProperty(PatchedEventSource, "OPEN", { value: OriginalEventSource.OPEN });
    Object.defineProperty(PatchedEventSource, "CLOSED", { value: OriginalEventSource.CLOSED });
    window.EventSource = PatchedEventSource as typeof window.EventSource;
  }
}
