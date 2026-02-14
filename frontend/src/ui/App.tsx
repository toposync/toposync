import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  ElementType,
  FileDropHandler,
  Notification,
  NotificationRenderer,
  SettingsPanel,
  ThemeDefinition,
  TopoSyncHost,
  Vector3,
  GraphicsQuality,
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
  getNotification,
  getSettings,
  listCompositions,
  listNotifications,
  emitEvent,
  patchExtensionSettings,
  putComposition,
  renameComposition,
} from "../util/api";
import type { AppSettings } from "../util/api";
import { i18n, resolveLocalizedString } from "../util/i18n";
import { loadRemoteActivate } from "../util/moduleFederation";
import { applyTheme, loadThemeId, saveThemeId } from "../util/theme";
import { SettingsModal } from "./SettingsModal";
import { Viewport2D } from "./Viewport2D";
import { builtinNotificationRenderers } from "./notifications/pipelinesNotifications";
import { CompositionEditorScreen } from "./screens/CompositionEditorScreen";
import { MainScreen } from "./screens/MainScreen";
import { PipelinesScreen } from "./screens/PipelinesScreen";

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

type Screen = "main" | "editor" | "pipelines";

type Composition = {
  id: string;
  name: string;
  elements: CompositionElement[];
};

const LEGACY_STORAGE_KEY = "toposync.composition.v1";
const SAVE_DEBOUNCE_MS = 400;
const VIEW_SETTINGS_STORAGE_KEY = "toposync.view.v1";
const HISTORY_LIMIT = 120;

function isWallHeightPreset(value: unknown): value is WallHeightPreset {
  return value === "low" || value === "medium" || value === "high";
}

function isGraphicsQuality(value: unknown): value is GraphicsQuality {
  return value === "simplified" || value === "detailed";
}

function wallHeightForPreset(preset: WallHeightPreset): number {
  if (preset === "low") return 0.6;
  if (preset === "medium") return 1.4;
  return 2.7;
}

function loadViewSettingsRecord(): Record<string, unknown> {
  try {
    const raw = localStorage.getItem(VIEW_SETTINGS_STORAGE_KEY);
    if (!raw) return {};
    return asRecord(JSON.parse(raw));
  } catch {
    return {};
  }
}

function loadWallHeightPreset(): WallHeightPreset {
  const rec = loadViewSettingsRecord();
  const preset = rec.wall_height_preset;
  return isWallHeightPreset(preset) ? preset : "high";
}

function loadGhostWalls(): boolean {
  const rec = loadViewSettingsRecord();
  return rec.ghost_walls === true;
}

function loadGraphicsQuality(): GraphicsQuality {
  const rec = loadViewSettingsRecord();
  const raw = rec.graphics_quality;
  return isGraphicsQuality(raw) ? raw : "simplified";
}

function saveViewSettings(preset: WallHeightPreset, ghostWalls: boolean, graphicsQuality: GraphicsQuality): void {
  try {
    localStorage.setItem(
      VIEW_SETTINGS_STORAGE_KEY,
      JSON.stringify({ wall_height_preset: preset, ghost_walls: ghostWalls, graphics_quality: graphicsQuality }),
    );
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

function isCompositionSnapshot(v: unknown): v is Composition {
  if (!v || typeof v !== "object" || Array.isArray(v)) return false;
  const rec = v as Record<string, unknown>;
  return typeof rec.id === "string" && typeof rec.name === "string" && Array.isArray(rec.elements);
}

function asVector3(v: unknown, fallback: Vector3): Vector3 {
  const obj = asRecord(v);
  return {
    x: asNumber(obj.x, fallback.x),
    y: asNumber(obj.y, fallback.y),
    z: asNumber(obj.z, fallback.z),
  };
}

function parseIsoMillis(iso: string | undefined): number {
  if (!iso) return 0;
  const ts = Date.parse(iso);
  return Number.isFinite(ts) ? ts : 0;
}

function notificationActivityMillis(notification: Notification): number {
  return Math.max(parseIsoMillis(notification.updatedAt), parseIsoMillis(notification.createdAt));
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
  const [fileDropHandlers, setFileDropHandlers] = useState<FileDropHandler[]>([]);
  const [settingsPanelsById, setSettingsPanelsById] = useState<Record<string, SettingsPanel>>({});
  const [themesById, setThemesById] = useState<Record<string, ThemeDefinition>>({});
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [notificationsCursor, setNotificationsCursor] = useState<number | null>(null);
  const [notificationsHasMore, setNotificationsHasMore] = useState(true);
  const [notificationsLoading, setNotificationsLoading] = useState(false);
  const [activeNotificationId, setActiveNotificationId] = useState<string | null>(null);
  const lastUserInteractionTsRef = useRef<number>(Date.now());
  const hasManualNotificationSelectionRef = useRef(false);
  const [composition, setComposition] = useState<Composition>(() => defaultComposition());
  const compositionRef = useRef<Composition>(composition);
  const [compositions, setCompositions] = useState<Array<{ id: string; name: string }>>([]);
  const [activeCompositionId, setActiveCompositionId] = useState<string>("ground");
  const [compositionLoaded, setCompositionLoaded] = useState(false);
  const [backendAvailable, setBackendAvailable] = useState(false);
  const [wallHeightPreset, setWallHeightPreset] = useState<WallHeightPreset>(() => loadWallHeightPreset());
  const [ghostWalls, setGhostWalls] = useState<boolean>(() => loadGhostWalls());
  const [graphicsQuality, setGraphicsQuality] = useState<GraphicsQuality>(() => loadGraphicsQuality());
  const [themeId, setThemeId] = useState<string>(() => loadThemeId());
  const [settings, setSettings] = useState<AppSettings>({ core: {}, extensions: {} });
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);

  const [compositionRevision, setCompositionRevision] = useState(0);

  const screenRef = useRef<Screen>(screen);
  const historyGroupRef = useRef<{ depth: number; snapshot: Composition | null; changed: boolean }>({
    depth: 0,
    snapshot: null,
    changed: false,
  });
  const [undoStack, setUndoStack] = useState<Composition[]>([]);
  const [redoStack, setRedoStack] = useState<Composition[]>([]);
  const undoStackRef = useRef<Composition[]>([]);
  const redoStackRef = useRef<Composition[]>([]);

  useLayoutEffect(() => {
    screenRef.current = screen;
  }, [screen]);

  useLayoutEffect(() => {
    compositionRef.current = composition;
  }, [composition]);

  useLayoutEffect(() => {
    undoStackRef.current = undoStack;
  }, [undoStack]);

  useLayoutEffect(() => {
    redoStackRef.current = redoStack;
  }, [redoStack]);

  const resetHistory = useCallback(() => {
    historyGroupRef.current = { depth: 0, snapshot: null, changed: false };
    undoStackRef.current = [];
    redoStackRef.current = [];
    setUndoStack([]);
    setRedoStack([]);
  }, []);

  useEffect(() => {
    resetHistory();
  }, [resetHistory, screen, composition.id]);

  const notificationRenderers = useMemo(
    () => [...Object.values(notificationRenderersById), ...builtinNotificationRenderers],
    [notificationRenderersById],
  );

  const viewSettings: ViewSettings = useMemo(
    () => ({
      wallHeightPreset,
      wallHeight: wallHeightForPreset(wallHeightPreset),
      ghostWalls,
      graphicsQuality,
    }),
    [ghostWalls, graphicsQuality, wallHeightPreset],
  );

  useEffect(() => {
    saveViewSettings(wallHeightPreset, ghostWalls, graphicsQuality);
  }, [ghostWalls, graphicsQuality, wallHeightPreset]);

  useEffect(() => {
    saveThemeId(themeId);
  }, [themeId]);

  useEffect(() => {
    const theme = themeId === "default" ? null : themesById[themeId] ?? null;
    applyTheme(theme);
  }, [themeId, themesById]);

  const themeOptions = useMemo<ThemeDefinition[]>(() => {
    return [
      {
        id: "default",
        name: { key: "core.ui.settings.theme.default", fallback: "Default" },
        description: { key: "core.ui.settings.theme.default_desc", fallback: "Toposync default theme." },
      },
      ...Object.values(themesById),
    ];
  }, [themesById]);

  const elementTypesRef = useRef<Record<string, ElementType>>(elementTypesById);
  useLayoutEffect(() => {
    elementTypesRef.current = elementTypesById;
  }, [elementTypesById]);

  const compositionStore = useMemo(() => {
    const listeners = new Set<() => void>();
    return {
      getSnapshot: () => compositionRef.current,
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      notify: () => {
        for (const listener of listeners) listener();
      },
    };
  }, []);

  useEffect(() => {
    compositionStore.notify();
  }, [composition, compositionRevision, compositionStore]);

  const elementTypesStore = useMemo(() => {
    const listeners = new Set<() => void>();
    return {
      getSnapshot: () => elementTypesRef.current,
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      notify: () => {
        for (const listener of listeners) listener();
      },
    };
  }, []);

  useEffect(() => {
    elementTypesStore.notify();
  }, [elementTypesById, elementTypesStore]);

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
      registerFileDropHandler(handler) {
        setFileDropHandlers((prev) => {
          const idx = prev.findIndex((h) => h.id === handler.id);
          if (idx === -1) return [...prev, handler];
          const next = prev.slice();
          next[idx] = handler;
          return next;
        });
      },
      registerSettingsPanel(panel) {
        setSettingsPanelsById((prev) => ({ ...prev, [panel.id]: panel }));
      },
      registerTheme(theme) {
        setThemesById((prev) => ({ ...prev, [theme.id]: theme }));
      },
      api: {
        emitEvent,
        getDevice,
      },
      i18n,
      ui: {
        Viewport2DReplica: ({ session, className, style }) => {
          const currentComposition = useSyncExternalStore(
            compositionStore.subscribe,
            compositionStore.getSnapshot,
            compositionStore.getSnapshot,
          );
          const currentElementTypes = useSyncExternalStore(
            elementTypesStore.subscribe,
            elementTypesStore.getSnapshot,
            elementTypesStore.getSnapshot,
          );

          return (
            <div className={className} style={{ position: "relative", ...style }}>
              <Viewport2D
                elements={currentComposition.elements}
                elementTypesById={currentElementTypes}
                interactionMode="navigate"
                activeToolSession={session ?? null}
                enableKeyboardShortcuts={false}
                toolSnapToGrid={false}
              />
            </div>
          );
        },
      },
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
            const loaded = await getSettings();
            if (!cancelled) setSettings(loaded);
          } catch (err) {
            console.error("Failed to load settings from backend", err);
          }
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
          try {
            const loaded = await getSettings();
            if (!cancelled) setSettings(loaded);
          } catch (err) {
            console.error("Failed to load settings from backend", err);
          }
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
    const bump = () => {
      lastUserInteractionTsRef.current = Date.now();
    };
    window.addEventListener("pointerdown", bump, { capture: true });
    window.addEventListener("keydown", bump, { capture: true });
    window.addEventListener("wheel", bump, { capture: true, passive: true });
    window.addEventListener("touchstart", bump, { capture: true, passive: true });
    return () => {
      window.removeEventListener("pointerdown", bump, true);
      window.removeEventListener("keydown", bump, true);
      window.removeEventListener("wheel", bump, true);
      window.removeEventListener("touchstart", bump, true);
    };
  }, []);

  const upsertNotification = useCallback((next: Notification, op: "insert" | "update") => {
    setNotifications((prev) => {
      const idx = prev.findIndex((n) => n.id === next.id);
      if (idx === -1) return [next, ...prev];

      const prevEntry = prev[idx];
      const merged = { ...prevEntry, ...next };
      const out = prev.slice();
      out[idx] = merged;

      const shouldBump =
        idx > 0 &&
        (op === "insert" ||
          notificationActivityMillis(merged) > notificationActivityMillis(prevEntry) ||
          (prevEntry.imageUrl ?? "") !== (merged.imageUrl ?? ""));

      if (shouldBump) {
        out.splice(idx, 1);
        return [merged, ...out];
      }

      return out;
    });
  }, []);

  useEffect(() => {
    if (!backendAvailable) return;
    let cancelled = false;
    setNotificationsLoading(true);
    void (async () => {
      try {
        const page = await listNotifications(null, 40);
        if (cancelled) return;
        setNotifications(page.notifications ?? []);
        setNotificationsCursor(page.next_cursor ?? null);
        setNotificationsHasMore(page.next_cursor != null);
      } catch (err) {
        console.error("Failed to load notifications", err);
      } finally {
        if (!cancelled) setNotificationsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [backendAvailable]);

  useEffect(() => {
    if (notifications.length === 0) {
      if (activeNotificationId) setActiveNotificationId(null);
      return;
    }
    if (activeNotificationId && notifications.some((n) => n.id === activeNotificationId)) return;
    setActiveNotificationId(notifications[0].id);
  }, [activeNotificationId, notifications]);

  useEffect(() => {
    if (!backendAvailable) return;

    const es = new EventSource("/api/notifications/stream");
    es.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data ?? "{}") as { op?: string; notification?: Notification };
        const op = parsed.op === "update" ? "update" : "insert";
        const notif = parsed.notification;
        if (!notif || typeof notif.id !== "string") return;
        upsertNotification(notif, op);

        if (op === "insert") {
          const now = Date.now();
          const idle = now - lastUserInteractionTsRef.current > 12_000;
          const allowAuto = !hasManualNotificationSelectionRef.current || idle;
          if (allowAuto) setActiveNotificationId(notif.id);
        }
      } catch (err) {
        console.warn("Failed to parse notifications SSE", err);
      }
    };
    es.onerror = (err) => {
      console.warn("Notifications SSE error", err);
    };

    return () => es.close();
  }, [backendAvailable, upsertNotification]);

  useEffect(() => {
    if (!backendAvailable) return;
    if (!activeNotificationId) return;

    let cancelled = false;
    void getNotification(activeNotificationId)
      .then((notif) => {
        if (cancelled) return;
        upsertNotification(notif, "update");
      })
      .catch((err) => {
        console.warn("Failed to fetch active notification", err);
      });

    const es = new EventSource(`/api/notifications/${encodeURIComponent(activeNotificationId)}/stream`);
    es.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data ?? "{}") as { op?: string; notification?: Notification };
        const op = parsed.op === "update" ? "update" : "insert";
        const notif = parsed.notification;
        if (!notif || typeof notif.id !== "string") return;
        upsertNotification(notif, op);
      } catch (err) {
        console.warn("Failed to parse notification detail SSE", err);
      }
    };
    es.onerror = (err) => {
      console.warn("Notification detail SSE error", err);
    };

    return () => {
      cancelled = true;
      es.close();
    };
  }, [activeNotificationId, backendAvailable, upsertNotification]);

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

  const recordHistoryBeforeChange = useCallback((snapshot: Composition) => {
    if (screenRef.current !== "editor") return;

    const group = historyGroupRef.current;
    if (group.depth > 0) {
      if (!group.snapshot) group.snapshot = snapshot;
      group.changed = true;
      redoStackRef.current = [];
      setRedoStack([]);
      return;
    }

    const nextUndo = [...undoStackRef.current, snapshot];
    if (nextUndo.length > HISTORY_LIMIT) nextUndo.splice(0, nextUndo.length - HISTORY_LIMIT);
    undoStackRef.current = nextUndo;
    setUndoStack(nextUndo);
    redoStackRef.current = [];
    setRedoStack([]);
  }, []);

  const beginUndoGroup = useCallback(() => {
    if (screenRef.current !== "editor") return;
    const group = historyGroupRef.current;
    group.depth += 1;
    if (group.depth === 1) {
      group.snapshot = compositionRef.current;
      group.changed = false;
    }
  }, []);

  const endUndoGroup = useCallback(() => {
    if (screenRef.current !== "editor") return;
    const group = historyGroupRef.current;
    if (group.depth <= 0) return;
    group.depth -= 1;
    if (group.depth !== 0) return;

    if (group.changed && group.snapshot && isCompositionSnapshot(group.snapshot)) {
      const nextUndo = [...undoStackRef.current, group.snapshot];
      if (nextUndo.length > HISTORY_LIMIT) nextUndo.splice(0, nextUndo.length - HISTORY_LIMIT);
      undoStackRef.current = nextUndo;
      setUndoStack(nextUndo);
      redoStackRef.current = [];
      setRedoStack([]);
    }

    group.snapshot = null;
    group.changed = false;
  }, []);

  const undo = useCallback(() => {
    if (screenRef.current !== "editor") return;
    historyGroupRef.current = { depth: 0, snapshot: null, changed: false };

    const stack = undoStackRef.current;
    if (stack.length === 0) return;

    let idx = stack.length - 1;
    while (idx >= 0 && !isCompositionSnapshot(stack[idx])) idx -= 1;
    if (idx < 0) {
      resetHistory();
      return;
    }

    const snapshot = stack[idx];
    const nextUndo = stack.slice(0, idx);

    const current = compositionRef.current;
    const nextRedo = isCompositionSnapshot(current) ? [...redoStackRef.current, current] : [...redoStackRef.current];
    if (nextRedo.length > HISTORY_LIMIT) nextRedo.splice(0, nextRedo.length - HISTORY_LIMIT);

    undoStackRef.current = nextUndo;
    redoStackRef.current = nextRedo;
    setUndoStack(nextUndo);
    setRedoStack(nextRedo);

    compositionRef.current = snapshot;
    setComposition(snapshot);
    setCompositionRevision((v) => v + 1);
  }, []);

  const redo = useCallback(() => {
    if (screenRef.current !== "editor") return;
    historyGroupRef.current = { depth: 0, snapshot: null, changed: false };

    const stack = redoStackRef.current;
    if (stack.length === 0) return;

    let idx = stack.length - 1;
    while (idx >= 0 && !isCompositionSnapshot(stack[idx])) idx -= 1;
    if (idx < 0) {
      resetHistory();
      return;
    }

    const snapshot = stack[idx];
    const nextRedo = stack.slice(0, idx);

    const current = compositionRef.current;
    const nextUndo = isCompositionSnapshot(current) ? [...undoStackRef.current, current] : [...undoStackRef.current];
    if (nextUndo.length > HISTORY_LIMIT) nextUndo.splice(0, nextUndo.length - HISTORY_LIMIT);

    undoStackRef.current = nextUndo;
    redoStackRef.current = nextRedo;
    setUndoStack(nextUndo);
    setRedoStack(nextRedo);

    compositionRef.current = snapshot;
    setComposition(snapshot);
    setCompositionRevision((v) => v + 1);
  }, []);

  const updateExtensionSettings = useCallback(
    async (extensionId: string, patch: Record<string, unknown>): Promise<Record<string, unknown>> => {
      if (!backendAvailable) {
        const current = settings.extensions?.[extensionId] ?? {};
        const merged = { ...current, ...(patch ?? {}) };
        setSettings((prev) => ({ ...prev, extensions: { ...prev.extensions, [extensionId]: merged } }));
        return merged;
      }

      const next = await patchExtensionSettings(extensionId, patch);
      setSettings((prev) => ({ ...prev, extensions: { ...prev.extensions, [extensionId]: next } }));
      return next;
    },
    [backendAvailable, settings.extensions],
  );

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
      recordHistoryBeforeChange(compositionRef.current);
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
    [elementTypesById, recordHistoryBeforeChange],
  );

  const updateElement = useCallback((elementId: string, patch: CompositionElementPatch) => {
    recordHistoryBeforeChange(compositionRef.current);
    setComposition((prev) => {
      return {
        ...prev,
        elements: prev.elements.map((el) => (el.id === elementId ? mergeElement(el, patch) : el)),
      };
    });
    setCompositionRevision((v) => v + 1);
  }, [recordHistoryBeforeChange]);

  const removeElement = useCallback((elementId: string) => {
    recordHistoryBeforeChange(compositionRef.current);
    setComposition((prev) => {
      return { ...prev, elements: prev.elements.filter((el) => el.id !== elementId) };
    });
    setCompositionRevision((v) => v + 1);
  }, [recordHistoryBeforeChange]);

  const reorderElements = useCallback(
    (nextElements: CompositionElement[]) => {
      const current = compositionRef.current.elements;
      const sameOrder =
        nextElements.length === current.length && nextElements.every((el, idx) => el.id === current[idx]?.id);
      if (sameOrder) return;

      recordHistoryBeforeChange(compositionRef.current);
      setComposition((prev) => ({ ...prev, elements: nextElements }));
      setCompositionRevision((v) => v + 1);
    },
    [recordHistoryBeforeChange],
  );

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

  const loadMoreNotifications = useCallback(async (): Promise<void> => {
    if (!backendAvailable) return;
    if (notificationsLoading) return;
    if (!notificationsHasMore) return;

    setNotificationsLoading(true);
    try {
      const page = await listNotifications(notificationsCursor, 40);
      setNotificationsCursor(page.next_cursor ?? null);
      setNotificationsHasMore(page.next_cursor != null);
      if (page.notifications.length === 0) return;
      setNotifications((prev) => {
        const existing = new Set(prev.map((n) => n.id));
        const toAdd = page.notifications.filter((n) => !existing.has(n.id));
        return toAdd.length > 0 ? [...prev, ...toAdd] : prev;
      });
    } catch (err) {
      console.error("Failed to load more notifications", err);
    } finally {
      setNotificationsLoading(false);
    }
  }, [backendAvailable, notificationsCursor, notificationsHasMore, notificationsLoading]);

  const selectNotification = useCallback((notificationId: string) => {
    setActiveNotificationId(notificationId);
    hasManualNotificationSelectionRef.current = true;
    lastUserInteractionTsRef.current = Date.now();
  }, []);

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
          notificationRenderers={notificationRenderers}
          notifications={notifications}
          activeNotificationId={activeNotificationId}
          notificationsLoading={notificationsLoading}
          onSelectNotification={selectNotification}
          onLoadMoreNotifications={loadMoreNotifications}
          api={host.api}
          updateElement={updateElement}
          onEditComposition={() => setScreen("editor")}
          onOpenPipelines={() => setScreen("pipelines")}
          onOpenSettings={() => setIsSettingsOpen(true)}
          onActivateComposition={activateCompositionById}
          onCreateComposition={createNewComposition}
          onRenameComposition={renameExistingComposition}
          onDeleteComposition={deleteExistingComposition}
        />
      ) : screen === "editor" ? (
        <CompositionEditorScreen
          compositionName={composition.name}
          compositions={compositions}
          activeCompositionId={activeCompositionId}
          elements={composition.elements}
          elementTypesById={elementTypesById}
          api={host.api}
          fileDropHandlers={fileDropHandlers}
          createElement={createElement}
          editorTools={Object.values(editorToolsById)}
          updateElement={updateElement}
          reorderElements={reorderElements}
          removeElement={removeElement}
          onBeginUndoGroup={beginUndoGroup}
          onEndUndoGroup={endUndoGroup}
          onUndo={undo}
          onRedo={redo}
          onExit={() => setScreen("main")}
          onOpenSettings={() => setIsSettingsOpen(true)}
          onActivateComposition={activateCompositionById}
          onCreateComposition={createNewComposition}
          onRenameComposition={renameExistingComposition}
          onDeleteComposition={deleteExistingComposition}
        />
      ) : (
        <PipelinesScreen onClose={() => setScreen("main")} />
      )}

      <SettingsModal
        open={isSettingsOpen}
        backendAvailable={backendAvailable}
        api={host.api}
        wallHeightPreset={wallHeightPreset}
        ghostWalls={ghostWalls}
        graphicsQuality={graphicsQuality}
        onSetWallHeightPreset={setWallHeightPreset}
        onSetGhostWalls={setGhostWalls}
        onSetGraphicsQuality={setGraphicsQuality}
        panels={Object.values(settingsPanelsById)}
        themes={themeOptions}
        themeId={themeId}
        onSetThemeId={setThemeId}
        settings={settings}
        onPatchExtensionSettings={updateExtensionSettings}
        onClose={() => setIsSettingsOpen(false)}
      />
    </div>
  );
}
