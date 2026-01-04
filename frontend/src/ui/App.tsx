import React, { useCallback, useEffect, useMemo, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  ElementType,
  Notification,
  NotificationRenderer,
  TopoSyncHost,
  Vector3,
  ViewSettings,
  WallHeightPreset,
} from "@toposync/plugin-api";

import {
  activateComposition,
  createComposition,
  deleteComposition,
  fetchExtensions,
  getComposition,
  getDevice,
  listCompositions,
  emitEvent,
  putComposition,
  renameComposition,
} from "../util/api";
import { i18n, resolveLocalizedString } from "../util/i18n";
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
const VIEW_SETTINGS_STORAGE_KEY = "toposync.view.v1";

function isWallHeightPreset(value: unknown): value is WallHeightPreset {
  return value === "low" || value === "medium" || value === "high";
}

function wallHeightForPreset(preset: WallHeightPreset): number {
  if (preset === "low") return 0.6;
  if (preset === "medium") return 1.4;
  return 2.7;
}

function loadWallHeightPreset(): WallHeightPreset {
  try {
    const raw = localStorage.getItem(VIEW_SETTINGS_STORAGE_KEY);
    if (!raw) return "high";
    const obj = JSON.parse(raw);
    const rec = asRecord(obj);
    const preset = rec.wall_height_preset;
    return isWallHeightPreset(preset) ? preset : "high";
  } catch {
    return "high";
  }
}

function saveWallHeightPreset(preset: WallHeightPreset): void {
  try {
    localStorage.setItem(VIEW_SETTINGS_STORAGE_KEY, JSON.stringify({ wall_height_preset: preset }));
  } catch {
    // ignore
  }
}

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
  const [editorToolsById, setEditorToolsById] = useState<Record<string, EditorTool>>({});
  const [notifications] = useState<Notification[]>([]);
  const [composition, setComposition] = useState<Composition>(() => defaultComposition());
  const [compositions, setCompositions] = useState<Array<{ id: string; name: string }>>([]);
  const [activeCompositionId, setActiveCompositionId] = useState<string>("ground");
  const [compositionLoaded, setCompositionLoaded] = useState(false);
  const [backendAvailable, setBackendAvailable] = useState(false);
  const [wallHeightPreset, setWallHeightPreset] = useState<WallHeightPreset>(() => loadWallHeightPreset());

  const [compositionRevision, setCompositionRevision] = useState(0);

  const notificationRenderers = useMemo(
    () => Object.values(notificationRenderersById),
    [notificationRenderersById],
  );

  const viewSettings: ViewSettings = useMemo(
    () => ({
      wallHeightPreset,
      wallHeight: wallHeightForPreset(wallHeightPreset),
    }),
    [wallHeightPreset],
  );

  useEffect(() => {
    saveWallHeightPreset(wallHeightPreset);
  }, [wallHeightPreset]);

  const host: TopoSyncHost = useMemo(
    () => ({
      registerElementType(elementType) {
        setElementTypesById((prev) => ({ ...prev, [elementType.type]: elementType }));
      },
      registerNotificationRenderer(renderer) {
        setNotificationRenderersById((prev) => ({ ...prev, [renderer.id]: renderer }));
      },
      registerEditorTool(tool) {
        setEditorToolsById((prev) => ({ ...prev, [tool.id]: tool }));
      },
      api: {
        emitEvent,
        getDevice,
      },
      i18n,
    }),
    [],
  );

  useEffect(() => {
    let cancelled = false;

    async function hydrate() {
      try {
        const [index, fromBackend] = await Promise.all([listCompositions(), getComposition()]);
        if (cancelled) return;

        setCompositions(index.compositions);
        setActiveCompositionId(index.active_composition_id);

        const legacy = loadLegacyComposition();
        if (fromBackend.elements.length === 0 && legacy && legacy.elements.length > 0) {
          const saved = await putComposition(legacy);
          if (cancelled) return;
          setComposition(saved);
          setActiveCompositionId(saved.id);
          setCompositions((prev) => {
            const exists = prev.some((c) => c.id === saved.id);
            const next = exists ? prev.map((c) => (c.id === saved.id ? { id: saved.id, name: saved.name } : c)) : [...prev, { id: saved.id, name: saved.name }];
            return next;
          });
          setBackendAvailable(true);
          try {
            localStorage.removeItem(LEGACY_STORAGE_KEY);
          } catch {
            // ignore
          }
        } else {
          setComposition(fromBackend);
          setActiveCompositionId(fromBackend.id);
          setCompositions((prev) => {
            const exists = prev.some((c) => c.id === fromBackend.id);
            const next = exists
              ? prev.map((c) => (c.id === fromBackend.id ? { id: fromBackend.id, name: fromBackend.name } : c))
              : [...prev, { id: fromBackend.id, name: fromBackend.name }];
            return next;
          });
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
        setCompositions((prev) => (legacy ? [{ id: legacy.id, name: legacy.name }] : prev));
        setActiveCompositionId(legacy?.id ?? "ground");
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
  }, [backendAvailable, composition, compositionLoaded, compositionRevision]);

  const flushSave = useCallback(async (): Promise<void> => {
    if (!compositionLoaded) return;
    if (!backendAvailable) return;
    try {
      await putComposition(composition);
    } catch (err) {
      console.error("Failed to save composition to backend", err);
    }
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

  const createElement = useCallback(
    (typeId: string, init: Partial<Omit<CompositionElement, "id" | "type">> = {}): string | null => {
      const def = elementTypesById[typeId];
      if (!def) return null;

      const id = newId();
      setComposition((prev) => {
        const idx = prev.elements.length;
        const col = idx % 4;
        const row = Math.floor(idx / 4);

        const base: CompositionElement = {
          id,
          type: typeId,
          name: resolveLocalizedString(def.name),
          position: { x: (col - 1.5) * 1.3, y: 0, z: (row - 1.5) * 1.3 },
          rotation: { x: 0, y: 0, z: 0 },
          props: { ...(def.defaultProps ?? {}) },
        };

        const next: CompositionElement = {
          ...base,
          ...init,
          position: { ...base.position, ...(init.position ?? {}) },
          rotation: { ...base.rotation, ...(init.rotation ?? {}) },
          props: { ...base.props, ...(init.props ?? {}) },
        };

        return { ...prev, elements: [...prev.elements, next] };
      });
      setCompositionRevision((v) => v + 1);
      return id;
    },
    [elementTypesById],
  );

  const updateElement = useCallback((elementId: string, patch: CompositionElementPatch) => {
    setComposition((prev) => ({
      ...prev,
      elements: prev.elements.map((el) => (el.id === elementId ? mergeElement(el, patch) : el)),
    }));
    setCompositionRevision((v) => v + 1);
  }, []);

  const removeElement = useCallback((elementId: string) => {
    setComposition((prev) => ({ ...prev, elements: prev.elements.filter((el) => el.id !== elementId) }));
    setCompositionRevision((v) => v + 1);
  }, []);

  const activateCompositionById = useCallback(
    async (compositionId: string): Promise<Composition> => {
      await flushSave();
      const next = await activateComposition(compositionId);
      setComposition(next);
      setActiveCompositionId(next.id);
      return next;
    },
    [flushSave],
  );

  const createNewComposition = useCallback(
    async (name: string): Promise<Composition> => {
      await flushSave();
      const next = await createComposition(name);
      setComposition(next);
      setActiveCompositionId(next.id);
      setCompositions((prev) => [...prev, { id: next.id, name: next.name }]);
      return next;
    },
    [flushSave],
  );

  const renameExistingComposition = useCallback(async (compositionId: string, name: string): Promise<Composition> => {
    const updated = await renameComposition(compositionId, name);
    setCompositions((prev) => prev.map((c) => (c.id === compositionId ? { id: c.id, name: updated.name } : c)));
    setComposition((prev) => (prev.id === compositionId ? { ...prev, name: updated.name } : prev));
    return updated;
  }, []);

  const deleteExistingComposition = useCallback(
    async (compositionId: string): Promise<void> => {
      const res = await deleteComposition(compositionId);
      setCompositions(res.compositions);
      setActiveCompositionId(res.active_composition_id);
      setComposition(res.active_composition);
    },
    [],
  );

  return (
    <div className="appShell">
      {screen === "main" ? (
        <MainScreen
          compositionName={composition.name}
          compositions={compositions}
          activeCompositionId={activeCompositionId}
          elements={composition.elements}
          elementTypesById={elementTypesById}
          viewSettings={viewSettings}
          onSetWallHeightPreset={setWallHeightPreset}
          notificationRenderers={notificationRenderers}
          notifications={notifications}
          api={host.api}
          updateElement={updateElement}
          onEditComposition={() => setScreen("editor")}
          onActivateComposition={activateCompositionById}
          onCreateComposition={createNewComposition}
          onRenameComposition={renameExistingComposition}
          onDeleteComposition={deleteExistingComposition}
        />
      ) : (
        <CompositionEditorScreen
          compositionName={composition.name}
          compositions={compositions}
          activeCompositionId={activeCompositionId}
          elements={composition.elements}
          elementTypesById={elementTypesById}
          createElement={createElement}
          editorTools={Object.values(editorToolsById)}
          updateElement={updateElement}
          removeElement={removeElement}
          onExit={() => setScreen("main")}
          onActivateComposition={activateCompositionById}
          onCreateComposition={createNewComposition}
          onRenameComposition={renameExistingComposition}
          onDeleteComposition={deleteExistingComposition}
        />
      )}
    </div>
  );
}
