import React, { useCallback, useEffect, useMemo, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  Notification,
  NotificationRenderer,
  TopoSyncHost,
  Vector3,
} from "@toposync/plugin-api";

import { fetchExtensions, getComposition, getDevice, emitEvent, putComposition } from "../util/api";
import { loadRemoteActivate } from "../util/moduleFederation";
import { CompositionEditorScreen } from "./screens/CompositionEditorScreen";
import { MainScreen } from "./screens/MainScreen";

type ExtensionRecord = {
  id: string;
  name: string;
  version: string;
  frontend?: {
    kind: string;
    remote_entry_url: string;
    scope: string;
    module: string;
  };
};

type Screen = "main" | "editor";

type Composition = {
  id: string;
  name: string;
  elements: CompositionElement[];
};

const LEGACY_STORAGE_KEY = "toposync.composition.v1";
const SAVE_DEBOUNCE_MS = 400;

function asNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function asString(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}

function asRecord(v: unknown): Record<string, unknown> {
  if (v && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  return {};
}

function asVector3(v: unknown, fallback: Vector3): Vector3 {
  const obj = asRecord(v);
  return {
    x: asNumber(obj.x, fallback.x),
    y: asNumber(obj.y, fallback.y),
    z: asNumber(obj.z, fallback.z),
  };
}

function defaultComposition(): Composition {
  return { id: "ground", name: "Térreo", elements: [] };
}

function loadLegacyComposition(): Composition | null {
  try {
    const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
    if (!raw) return null;
    const obj = JSON.parse(raw);
    const rec = asRecord(obj);
    const elementsRaw = Array.isArray(rec.elements) ? rec.elements : [];
    const elements: CompositionElement[] = elementsRaw
      .map((e) => {
        const el = asRecord(e);
        return {
          id: asString(el.id, ""),
          type: asString(el.type, ""),
          name: asString(el.name, ""),
          position: asVector3(el.position, { x: 0, y: 0, z: 0 }),
          rotation: asVector3(el.rotation, { x: 0, y: 0, z: 0 }),
          props: asRecord(el.props),
        } satisfies CompositionElement;
      })
      .filter((e) => Boolean(e.id) && Boolean(e.type));
    return {
      id: asString(rec.id, "ground"),
      name: asString(rec.name, "Térreo"),
      elements,
    };
  } catch {
    return null;
  }
}

function newId(): string {
  const cryptoAny = crypto as unknown as { randomUUID?: () => string };
  return cryptoAny.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function mergeElement(el: CompositionElement, patch: CompositionElementPatch): CompositionElement {
  return {
    ...el,
    ...patch,
    position: { ...el.position, ...patch.position },
    rotation: { ...el.rotation, ...patch.rotation },
    props: { ...el.props, ...patch.props },
  };
}

export function App(): React.ReactElement {
  const [screen, setScreen] = useState<Screen>("main");
  const [elementTypesById, setElementTypesById] = useState<Record<string, ElementType>>({});
  const [notificationRenderersById, setNotificationRenderersById] = useState<Record<string, NotificationRenderer>>({});
  const [notifications] = useState<Notification[]>([]);
  const [composition, setComposition] = useState<Composition>(() => defaultComposition());
  const [compositionLoaded, setCompositionLoaded] = useState(false);
  const [backendAvailable, setBackendAvailable] = useState(false);

  const notificationRenderers = useMemo(
    () => Object.values(notificationRenderersById),
    [notificationRenderersById],
  );

  const host: TopoSyncHost = useMemo(
    () => ({
      registerElementType(elementType) {
        setElementTypesById((prev) => ({ ...prev, [elementType.type]: elementType }));
      },
      registerNotificationRenderer(renderer) {
        setNotificationRenderersById((prev) => ({ ...prev, [renderer.id]: renderer }));
      },
      api: {
        emitEvent,
        getDevice,
      },
    }),
    [],
  );

  useEffect(() => {
    let cancelled = false;

    async function hydrate() {
      try {
        const fromBackend = await getComposition();
        if (cancelled) return;

        const legacy = loadLegacyComposition();
        if (fromBackend.elements.length === 0 && legacy && legacy.elements.length > 0) {
          const saved = await putComposition(legacy);
          if (cancelled) return;
          setComposition(saved);
          setBackendAvailable(true);
          try {
            localStorage.removeItem(LEGACY_STORAGE_KEY);
          } catch {
            // ignore
          }
        } else {
          setComposition(fromBackend);
          setBackendAvailable(true);
          if (legacy) {
            try {
              localStorage.removeItem(LEGACY_STORAGE_KEY);
            } catch {
              // ignore
            }
          }
        }
      } catch (err) {
        console.error("Failed to load composition from backend", err);
        const legacy = loadLegacyComposition();
        if (legacy) setComposition(legacy);
        setBackendAvailable(false);
      } finally {
        if (!cancelled) setCompositionLoaded(true);
      }
    }

    void hydrate();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!compositionLoaded) return;
    if (!backendAvailable) return;

    const handle = window.setTimeout(() => {
      void putComposition(composition).catch((err) => {
        console.error("Failed to save composition to backend", err);
      });
    }, SAVE_DEBOUNCE_MS);

    return () => window.clearTimeout(handle);
  }, [backendAvailable, composition, compositionLoaded]);

  useEffect(() => {
    let cancelled = false;

    async function run() {
      const exts: ExtensionRecord[] = await fetchExtensions();
      for (const ext of exts) {
        if (!ext.frontend || ext.frontend.kind !== "module-federation") continue;
        try {
          const activate = await loadRemoteActivate(
            ext.frontend.remote_entry_url,
            ext.frontend.scope,
            ext.frontend.module,
          );
          await activate(host);
        } catch (err) {
          if (cancelled) return;
          console.error(`[extension:${ext.id}]`, err);
        }
      }
    }

    void run();
    return () => {
      cancelled = true;
    };
  }, [host]);

  const addElement = useCallback(
    (typeId: string): string | null => {
      const def = elementTypesById[typeId];
      if (!def) return null;

      const id = newId();
      setComposition((prev) => {
        const idx = prev.elements.length;
        const col = idx % 4;
        const row = Math.floor(idx / 4);

        const element: CompositionElement = {
          id,
          type: typeId,
          name: def.name,
          position: { x: (col - 1.5) * 1.3, y: 0, z: (row - 1.5) * 1.3 },
          rotation: { x: 0, y: 0, z: 0 },
          props: { ...(def.defaultProps ?? {}) },
        };
        return { ...prev, elements: [...prev.elements, element] };
      });
      return id;
    },
    [elementTypesById],
  );

  const updateElement = useCallback((elementId: string, patch: CompositionElementPatch) => {
    setComposition((prev) => ({
      ...prev,
      elements: prev.elements.map((el) => (el.id === elementId ? mergeElement(el, patch) : el)),
    }));
  }, []);

  const removeElement = useCallback((elementId: string) => {
    setComposition((prev) => ({ ...prev, elements: prev.elements.filter((el) => el.id !== elementId) }));
  }, []);

  return (
    <div className="appShell">
      {screen === "main" ? (
        <MainScreen
          compositionName={composition.name}
          elements={composition.elements}
          elementTypesById={elementTypesById}
          notificationRenderers={notificationRenderers}
          notifications={notifications}
          api={host.api}
          updateElement={updateElement}
          onEditComposition={() => setScreen("editor")}
        />
      ) : (
        <CompositionEditorScreen
          compositionName={composition.name}
          elements={composition.elements}
          elementTypesById={elementTypesById}
          addElement={addElement}
          updateElement={updateElement}
          removeElement={removeElement}
          onExit={() => setScreen("main")}
        />
      )}
    </div>
  );
}
