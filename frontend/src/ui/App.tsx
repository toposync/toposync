import type { GeographicView } from "./geographic/GeographicMap";
import { StreamingAvailabilityBoundary, StreamingAvailabilityContext, type StreamingAvailability } from "./streams/StreamingAvailability";
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  ElementType,
  FileDropHandler,
  Notification,
  NotificationRenderer,
  NotificationDetailPanel,
  NotificationHistoryFilter,
  NotificationHistoryAssistant,
  PipelineOperatorPanel,
  PipelineReviewPanel,
  RenderViewDefinition,
  SettingsPanel,
  ThemeDefinition,
  ToposyncHost,
  GraphicsQuality,
  ViewSettings,
  WallHeightPreset,
} from "@toposync/plugin-api";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import {
  activateComposition,
  createComposition,
  deleteAccessUser,
  deleteComposition,
  listAccessUsers,
  fetchExtensions,
  getComposition,
  getDevice,
  getNotification,
  getNotificationsCount,
  getSettings,
  isAbortError,
  listCompositions,
  listNotifications,
  markNotificationsViewed,
  emitEvent,
  patchExtensionSettings,
  setInstallationTimezone,
  patchAccessUser,
  putComposition,
  patchCompositionMeta,
  renameComposition,
  createAccessUser,
  startAccessUserPairing,
} from "../util/api";
import type { AppSettings, AuthUser, NotificationsCount } from "../util/api";
import { i18n, resolveLocalizedString } from "../util/i18n";
import { resolveNotificationPeriod } from "../util/notificationPeriod";
import { loadRemoteActivate } from "../util/moduleFederation";
import { markToposyncPerformance } from "../util/performance";
import {
  applyTheme,
  applyUserVisualPreferences,
  DEFAULT_THEME_ID,
  isBuiltinThemeId,
  loadThemeId,
  loadViewport3DBackground,
  saveThemeId,
  saveViewport3DBackground,
  THEME_DEFAULT_ACCENT_INTENSITY,
  THEME_DEFAULT_TRANSPARENCY,
  type BuiltinThemeId,
  type Viewport3DBackground,
} from "../util/theme";
import { getPreviousPathname, navigate, replace, usePathname } from "./router";
import { Viewport2D } from "./Viewport2D";
import { NavigableViewport } from "./NavigableViewport";
import { ImageMaskEditor } from "./screens/pipelines/editor/panels/ImageDrawModals";
import { CropRectangleDrawModal } from "./screens/pipelines/editor/panels/ImageDrawModals";
import { createMeasurementLineElementType } from "./editor/measurementLineElementType";
import { builtinNotificationRenderers, notificationPriority } from "./notifications/pipelinesNotifications";
import { attachEphemeralImage, createEphemeralImageSession, withoutEphemeralImage, type EphemeralImageLease } from "./notifications/ephemeralImage";
import { CompositionEditorScreen } from "./screens/CompositionEditorScreen";
import {
  MainScreen,
  NOTIFICATIONS_FILTER_STORAGE_KEY,
  loadNotificationsFilter,
  type NotificationsFilter,
} from "./screens/MainScreen";
import { PipelinesScreen } from "./screens/PipelinesScreen";
import { ProcessingServersScreen } from "./screens/ProcessingServersScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { AccessScreen } from "./screens/AccessScreen";
import { StreamsDashboard, type StreamsDashboardContext } from "./streams/StreamsDashboard";
import { StreamTransportDebugScreen } from "./streams/StreamTransportDebugScreen";

type ExtensionRecord = {
  api_prefixes?: string[];
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
  meta?: Record<string, unknown>;
};

const SAVE_DEBOUNCE_MS = 400;
const NOTIFICATIONS_QUERY_DEBOUNCE_MS = 250;
const VIEW_SETTINGS_STORAGE_KEY = "toposync.view.v1";
const RENDER_MODE_STORAGE_KEY = "toposync.render_mode.v1";
const SPATIAL_VIDEO_EXTENSION_ID = "com.toposync.spatial_video";
const HISTORY_LIMIT = 120;
type RenderViewSettingsMap = Record<string, Record<string, unknown>>;
type WindowWithIdleCallback = Window & {
  requestIdleCallback?: (callback: () => void, options?: { timeout?: number }) => number;
  cancelIdleCallback?: (handle: number) => void;
};

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

function loadRenderViewSettings(): RenderViewSettingsMap {
  const rec = loadViewSettingsRecord();
  const raw = rec.render_view_settings;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: RenderViewSettingsMap = {};
  for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
    if (value && typeof value === "object" && !Array.isArray(value)) out[id] = value as Record<string, unknown>;
  }
  return out;
}

function saveViewSettings(
  preset: WallHeightPreset,
  ghostWalls: boolean,
  graphicsQuality: GraphicsQuality,
  renderViewSettings: RenderViewSettingsMap,
): void {
  try {
    localStorage.setItem(
      VIEW_SETTINGS_STORAGE_KEY,
      JSON.stringify({
        wall_height_preset: preset,
        ghost_walls: ghostWalls,
        graphics_quality: graphicsQuality,
        render_view_settings: renderViewSettings,
      }),
    );
  } catch {
    // ignore
  }
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

function parseIsoMillis(iso: string | undefined): number {
  if (!iso) return 0;
  const ts = Date.parse(iso);
  return Number.isFinite(ts) ? ts : 0;
}

function notificationCreatedMillis(notification: Notification): number {
  return parseIsoMillis(notification.createdAt);
}

function compareNotificationsByCreatedDesc(left: Notification, right: Notification): number {
  const rightTs = notificationCreatedMillis(right);
  const leftTs = notificationCreatedMillis(left);
  if (leftTs !== rightTs) {
    return rightTs - leftTs;
  }
  const rightUpdated = parseIsoMillis(right.updatedAt);
  const leftUpdated = parseIsoMillis(left.updatedAt);
  if (leftUpdated !== rightUpdated) {
    return rightUpdated - leftUpdated;
  }
  return asString(right.id, "").localeCompare(asString(left.id, ""));
}

function sortNotificationIdsByCreatedDesc(
  ids: Iterable<string>,
  byId: Record<string, Notification>,
): string[] {
  return Array.from(new Set(ids))
    .filter((id) => Boolean(byId[id]))
    .sort((leftId, rightId) => compareNotificationsByCreatedDesc(byId[leftId], byId[rightId]));
}

function notificationMatchesDataFilter(notification: Notification, filter: NotificationsFilter): boolean {
  if (filter.timeRange) {
    const created = Date.parse(notification.createdAt ?? "") / 1000;
    if (!Number.isFinite(created) || created < filter.timeRange.createdAfter || created >= filter.timeRange.createdBefore) return false;
  }
  const data = asRecord(asRecord(notification.payload).data);
  return Object.entries(filter.dataFilter ?? {}).every(([path, expected]) => {
    let value: unknown = data;
    for (const key of path.split(".")) {
      const record = asRecord(value);
      value = Object.prototype.hasOwnProperty.call(record, key) ? record[key] : undefined;
    }
    return typeof value === "string" && value === expected;
  });
}

function notificationMatchesFilter(notification: Notification, filter: NotificationsFilter): boolean {
  if (!filter.priorities.includes(notificationPriority(notification))) return false;
  if (!notificationMatchesDataFilter(notification, filter)) return false;
  if (filter.types.length > 0 && !filter.types.includes(notification.type)) return false;
  const normalize = (value: string) => value.toLowerCase().normalize("NFD").replace(/\p{Mn}/gu, "");
  const terms = normalize(filter.query).trim().split(/\s+/).filter(Boolean);
  const data = asRecord(asRecord(notification.payload).data);
  const source = asRecord(data.source);
  const text = normalize([notification.title, notification.description, data.camera_name, data.area_label,
    source.source_name]
    .filter((value): value is string => typeof value === "string").join(" "));
  return terms.every((term) => text.includes(term));
}

function stringArraysEqual(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((value, index) => value === right[index]);
}

function notificationFilterFacetsEqual(left: NotificationsFilter, right: NotificationsFilter): boolean {
  return left.timeRange?.createdAfter === right.timeRange?.createdAfter && left.timeRange?.createdBefore === right.timeRange?.createdBefore
    && left.timeRange?.timezone === right.timeRange?.timezone && left.period?.preset === right.period?.preset
    && left.period?.start === right.period?.start && left.period?.end === right.period?.end
    && left.extension?.id === right.extension?.id && left.extension?.value === right.extension?.value
    && stringArraysEqual(left.priorities, right.priorities) && stringArraysEqual(left.types, right.types)
    && Object.keys(left.dataFilter ?? {}).length === Object.keys(right.dataFilter ?? {}).length
    && Object.entries(left.dataFilter ?? {}).every(([key, value]) => right.dataFilter?.[key] === value);
}

function notificationFiltersEqual(left: NotificationsFilter, right: NotificationsFilter): boolean {
  return notificationFilterFacetsEqual(left, right) && left.query === right.query && (left.semantic || false) === (right.semantic || false);
}

function isOpenRealtimeNotification(notification: Notification | null | undefined): boolean {
  if (!notification) return false;
  const payload = asRecord(notification.payload);
  return asString(payload.status, "").trim().toLowerCase() === "open" && payload.realtime === true;
}

function defaultComposition(): Composition {
  return { id: "ground", name: "Térreo", elements: [] };
}

function extensionIdFromElementType(type: string): string | null {
  const parts = type.split(".").filter(Boolean);
  if (parts.length < 3) return null;
  if (parts[0] !== "com" || parts[1] !== "toposync") return null;
  return parts.slice(0, 3).join(".");
}

function loadSavedRenderMode(): string {
  try {
    return localStorage.getItem(RENDER_MODE_STORAGE_KEY)?.trim() || "3d";
  } catch {
    return "3d";
  }
}

function scheduleIdle(callback: () => void, timeout = 1200): () => void {
  const win = window as WindowWithIdleCallback;
  if (typeof win.requestIdleCallback === "function") {
    const handle = win.requestIdleCallback(callback, { timeout });
    return () => win.cancelIdleCallback?.(handle);
  }
  const handle = window.setTimeout(callback, 120);
  return () => window.clearTimeout(handle);
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

type AppProps = {
  authUser: AuthUser | null;
  authMode: string;
  onLogout: () => Promise<void>;
};

type NotificationsState = {
  byId: Record<string, Notification>;
  visibleIds: string[];
  resolvedOnServer?: boolean;
};

function notificationHistoryRefreshMode(filter: NotificationsFilter, resolvedOnServer: boolean): "manual" | "automatic" | "none" {
  if (filter.semantic && filter.query.trim()) return "manual";
  return filter.extension || resolvedOnServer ? "automatic" : "none";
}

function mergeNotificationPage(
  prev: NotificationsState,
  pageNotifications: readonly Notification[],
  options: { replaceVisible: boolean; filter: NotificationsFilter; resolvedOnServer?: boolean; ranked?: boolean },
): NotificationsState {
  const byId = { ...prev.byId };
  for (const raw of pageNotifications) {
    const notification = withoutEphemeralImage(raw);
    const existing = byId[notification.id];
    byId[notification.id] = existing ? { ...withoutEphemeralImage(existing), ...notification } : notification;
  }

  const pageIds = pageNotifications.map((notification) => notification.id);
  const ranked = options.ranked ?? Boolean(options.filter.semantic && options.filter.query.trim());
  if (options.resolvedOnServer || options.filter.extension) return {
    byId, resolvedOnServer: true,
    visibleIds: ranked ? pageIds : sortNotificationIdsByCreatedDesc(
      options.replaceVisible ? pageIds : [...prev.visibleIds, ...pageIds], byId),
  };
  if (ranked) return { byId, visibleIds: pageIds };
  const pageIdSet = new Set(pageIds);
  const retainedVisibleIds = options.replaceVisible
    ? prev.visibleIds.filter((id) => {
        if (pageIdSet.has(id)) return false;
        const notification = byId[id];
        return notification ? notificationMatchesFilter(notification, options.filter) : false;
      })
    : prev.visibleIds;
  const nextIds = [...retainedVisibleIds, ...pageIds];

  return {
    byId,
    visibleIds: sortNotificationIdsByCreatedDesc(nextIds, byId),
  };
}

function upsertNotificationInState(
  prev: NotificationsState,
  next: Notification,
  filter: NotificationsFilter,
): NotificationsState {
  next = withoutEphemeralImage(next);
  if (filter.extension) {
    const previous = prev.byId[next.id];
    if (previous && Object.keys({ ...previous, ...next }).every(key => JSON.stringify((previous as unknown as Record<string, unknown>)[key]) === JSON.stringify((next as unknown as Record<string, unknown>)[key]))) return prev;
    const byId = { ...prev.byId };
    delete byId[next.id];
    return { ...prev, byId, visibleIds: prev.visibleIds.filter(id => id !== next.id) };
  }
  const existing = prev.byId[next.id];
  const merged = existing ? { ...withoutEphemeralImage(existing), ...next } : next;
  const byId = { ...prev.byId, [next.id]: merged };
  if (!notificationMatchesDataFilter(merged, filter)) return { ...prev, byId, visibleIds: prev.visibleIds.filter(id => id !== next.id) };
  // A relevância depende do modelo; eventos novos não entram por coincidência literal.
  if (prev.resolvedOnServer || (filter.semantic && filter.query.trim())) return { ...prev, byId };
  const visible = new Set(prev.visibleIds);
  if (notificationMatchesFilter(merged, filter)) {
    visible.add(next.id);
  } else {
    visible.delete(next.id);
  }
  return {
    byId,
    visibleIds: sortNotificationIdsByCreatedDesc(visible, byId),
  };
}

export function App({ authUser, authMode, onLogout }: AppProps): React.ReactElement {
  const pathname = usePathname();
  const [screen, setScreen] = useState<Screen>("main");
  const geographicView = useRef<GeographicView | null>(null);
  const editorReturnPath = useRef<string | null>(null);
  const [editorReturnElementId, setEditorReturnElementId] = useState<string | null>(null);
  const [editorInitialAction, setEditorInitialAction] = useState<string | undefined>(undefined);
  const [elementTypesById, setElementTypesById] = useState<Record<string, ElementType>>({});
  const [historyRequest, setHistoryRequest] = useState<{ selection: { id: string; value: string }; returnToNotification?: string } | null>(null);
  const [historyAssistants, setHistoryAssistants] = useState<Record<string, NotificationHistoryAssistant>>({});
  const [notificationHistoryFilters, setNotificationHistoryFilters] = useState<Record<string, NotificationHistoryFilter>>({});
  const [notificationsError, setNotificationsError] = useState(false);
  const [notificationDetailPanelsById, setNotificationDetailPanelsById] = useState<Record<string, NotificationDetailPanel>>({});
  const [notificationRenderersById, setNotificationRenderersById] = useState<Record<string, NotificationRenderer>>({});
  const [editorToolsById, setEditorToolsById] = useState<Record<string, EditorTool>>({});
  const [fileDropHandlers, setFileDropHandlers] = useState<FileDropHandler[]>([]);
  const [settingsPanelsById, setSettingsPanelsById] = useState<Record<string, SettingsPanel>>({});
  const [pipelineReviewPanels, setPipelineReviewPanels] = useState<Record<string, PipelineReviewPanel>>({});
  const [pipelineOperatorPanelsByOperatorId, setPipelineOperatorPanelsByOperatorId] = useState<Record<string, PipelineOperatorPanel>>({});
  const [renderViewsById, setRenderViewsById] = useState<Record<string, RenderViewDefinition>>({});
  const [themesById, setThemesById] = useState<Record<string, ThemeDefinition>>({});
  const [notificationsState, setNotificationsState] = useState<NotificationsState>({
    byId: {},
    visibleIds: [],
  });
  const [notificationsFilter, setNotificationsFilter] = useState<NotificationsFilter>(() => loadNotificationsFilter());
  const [notificationsRequestFilter, setNotificationsRequestFilter] = useState<NotificationsFilter>(notificationsFilter);
  const [notificationsSearchStatus, setNotificationsSearchStatus] = useState<import("../util/api").NotificationSearchStatus>();
  const [notificationsCursor, setNotificationsCursor] = useState<number | null>(null);
  const [notificationsHasMore, setNotificationsHasMore] = useState(true);
  const [notificationsLoading, setNotificationsLoading] = useState(false);
  const [notificationsSyncRevision, setNotificationsSyncRevision] = useState(0);
  const [notificationsHistoryChanged, setNotificationsHistoryChanged] = useState(false);
  const notificationsResolvedOnServerRef = useRef(false);
  const notificationsLoadedFilterRef = useRef<NotificationsFilter | null>(null);
  useLayoutEffect(() => { notificationsResolvedOnServerRef.current = Boolean(notificationsState.resolvedOnServer); }, [notificationsState.resolvedOnServer]);
  const [notificationsCount, setNotificationsCount] = useState<NotificationsCount>({
    total: 0,
    by_priority: { low: 0, medium: 0, high: 0 },
    unread_total: 0,
    unread_by_priority: { low: 0, medium: 0, high: 0 },
  });
  const [activeNotificationId, setActiveNotificationId] = useState<string | null>(null);
  const [historySource, setHistorySource] = useState<{ id: string; notification: Notification | null } | null>(null);
  const historySourceRef = useRef(historySource);
  const historySourceRead = useRef<AbortController | null>(null);
  useLayoutEffect(() => { historySourceRef.current = historySource; }, [historySource]);
  useEffect(() => () => historySourceRead.current?.abort(), []);
  // Pixels exist only in this selected-detail lease, never notificationsState.
  const [activeImageLease, setActiveImageLease] = useState<EphemeralImageLease | null>(null);
  const activeImageSessionRef = useRef<ReturnType<typeof createEphemeralImageSession> | null>(null);
  const lastUserInteractionTsRef = useRef<number>(Date.now());
  const hasManualNotificationSelectionRef = useRef(false);
  const notificationDetailsOpenRef = useRef(false);
  const setNotificationDetailsOpen = useCallback((open: boolean) => {
    notificationDetailsOpenRef.current = open;
  }, []);
  const markNotificationsViewedInFlightRef = useRef<Promise<void> | null>(null);
  const notificationsFilterRef = useRef<NotificationsFilter>(notificationsRequestFilter);
  const notificationsListRequestRef = useRef(0);
  const notificationsListAbortRef = useRef<AbortController | null>(null);
  const notificationsMoreAbortRef = useRef<AbortController | null>(null);
  const notificationsCountAbortRef = useRef<AbortController | null>(null);
  const activeNotificationFetchAbortRef = useRef<AbortController | null>(null);
  const activeNotificationRefreshAbortRef = useRef<AbortController | null>(null);
  const mainViewportReadyMarkedRef = useRef(false);
  const extensionRecordsPromiseRef = useRef<Promise<ExtensionRecord[]> | null>(null);
  const activatedExtensionIdsRef = useRef<Set<string>>(new Set());
  const extensionActivationPromisesRef = useRef<Map<string, Promise<void>>>(new Map());
  const [composition, setComposition] = useState<Composition>(() => defaultComposition());
  const compositionRef = useRef<Composition>(composition);
  const persistedCompositionRef = useRef<Composition | null>(null);
  const [compositions, setCompositions] = useState<Array<{ id: string; name: string }>>([]);
  const [activeCompositionId, setActiveCompositionId] = useState<string>("ground");
  const [compositionLoaded, setCompositionLoaded] = useState(false);
  const [criticalExtensionsLoaded, setCriticalExtensionsLoaded] = useState(false);
  const [allExtensionsLoaded, setAllExtensionsLoaded] = useState(false);
  const [streamingAvailability, setStreamingAvailability] = useState<StreamingAvailability>("loading");
  const [extensionActivationDiagnostics, setExtensionActivationDiagnostics] = useState<Record<string, string>>({});
  const [mainViewportReady, setMainViewportReady] = useState(false);
  const [backendAvailable, setBackendAvailable] = useState(false);
  const [wallHeightPreset, setWallHeightPreset] = useState<WallHeightPreset>(() => loadWallHeightPreset());
  const [ghostWalls, setGhostWalls] = useState<boolean>(() => loadGhostWalls());
  const [graphicsQuality, setGraphicsQuality] = useState<GraphicsQuality>(() => loadGraphicsQuality());
  const [renderViewSettings, setRenderViewSettings] = useState<RenderViewSettingsMap>(() => loadRenderViewSettings());
  const [themeId, setThemeId] = useState<string>(() => loadThemeId());
  const [viewport3dBackground, setViewport3dBackground] = useState<Viewport3DBackground>(() => loadViewport3DBackground());
  const [settings, setSettings] = useState<AppSettings>({ core: {}, extensions: {} });

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

  useLayoutEffect(() => {
    try {
      localStorage.setItem(NOTIFICATIONS_FILTER_STORAGE_KEY, JSON.stringify(notificationsFilter));
    } catch {
      // ignore
    }
  }, [notificationsFilter]);

  useLayoutEffect(() => {
    notificationsFilterRef.current = notificationsRequestFilter;
  }, [notificationsRequestFilter]);

  const notifications = useMemo(
    () => notificationsState.visibleIds.map((id) => notificationsState.byId[id]).filter((item): item is Notification => Boolean(item)),
    [notificationsState],
  );

  const selectedNotification = activeNotificationId && historySource?.id === activeNotificationId ? historySource.notification : activeNotificationId ? notificationsState.byId[activeNotificationId] ?? null : null;
  const activeNotification = useMemo(() => attachEphemeralImage(
    selectedNotification, backendAvailable ? activeImageLease : null,
  ), [selectedNotification, backendAvailable, activeImageLease]);

  useLayoutEffect(() => { setActiveImageLease(null); }, [activeNotificationId, backendAvailable]);

  const abortNotificationListRequests = useCallback(() => {
    notificationsListAbortRef.current?.abort();
    notificationsListAbortRef.current = null;
    notificationsMoreAbortRef.current?.abort();
    notificationsMoreAbortRef.current = null;
  }, []);

  useEffect(() => {
    return () => {
      abortNotificationListRequests();
      notificationsCountAbortRef.current?.abort();
      notificationsCountAbortRef.current = null;
      activeNotificationFetchAbortRef.current?.abort();
      activeNotificationFetchAbortRef.current = null;
      activeNotificationRefreshAbortRef.current?.abort();
      activeNotificationRefreshAbortRef.current = null;
    };
  }, [abortNotificationListRequests]);

  useEffect(() => {
    const currentRequestFilter = notificationsFilterRef.current;
    if (notificationFiltersEqual(currentRequestFilter, notificationsFilter)) return;

    abortNotificationListRequests();
    if (backendAvailable && mainViewportReady) setNotificationsLoading(true);

    if (!notificationFilterFacetsEqual(currentRequestFilter, notificationsFilter)) {
      setNotificationsRequestFilter(notificationsFilter);
      return;
    }

    const handle = window.setTimeout(() => {
      setNotificationsRequestFilter(notificationsFilter);
    }, NOTIFICATIONS_QUERY_DEBOUNCE_MS);

    return () => window.clearTimeout(handle);
  }, [abortNotificationListRequests, backendAvailable, mainViewportReady, notificationsFilter]);

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
      renderViewSettings,
    }),
    [ghostWalls, graphicsQuality, renderViewSettings, wallHeightPreset],
  );

  useEffect(() => {
    saveViewSettings(wallHeightPreset, ghostWalls, graphicsQuality, renderViewSettings);
  }, [ghostWalls, graphicsQuality, renderViewSettings, wallHeightPreset]);

  const patchRenderViewSettings = useCallback((viewId: string, patch: Record<string, unknown>) => {
    setRenderViewSettings((prev) => ({
      ...prev,
      [viewId]: {
        ...(prev[viewId] ?? {}),
        ...(patch ?? {}),
      },
    }));
  }, []);

  useEffect(() => {
    saveThemeId(themeId);
  }, [themeId]);

  useEffect(() => {
    saveViewport3DBackground(viewport3dBackground);
  }, [viewport3dBackground]);

  useEffect(() => {
    applyUserVisualPreferences({
      transparency: THEME_DEFAULT_TRANSPARENCY,
      accentIntensity: THEME_DEFAULT_ACCENT_INTENSITY,
      viewport3dBackground,
    });
  }, [viewport3dBackground]);

  const resolvedTheme = useMemo((): { baseThemeId: BuiltinThemeId; overridesTheme: ThemeDefinition | null } => {
    if (isBuiltinThemeId(themeId)) {
      return { baseThemeId: themeId, overridesTheme: null };
    }
    const overridesTheme = themesById[themeId] ?? null;
    if (overridesTheme && isBuiltinThemeId(overridesTheme.id)) {
      return { baseThemeId: overridesTheme.id, overridesTheme };
    }
    return { baseThemeId: DEFAULT_THEME_ID, overridesTheme };
  }, [themeId, themesById]);

  useEffect(() => {
    applyTheme(resolvedTheme.baseThemeId, resolvedTheme.overridesTheme);
  }, [resolvedTheme.baseThemeId, resolvedTheme.overridesTheme]);

  const themeOptions = useMemo<ThemeDefinition[]>(() => {
    const builtinThemes: ThemeDefinition[] = [
      {
        id: "topo-day",
        name: { key: "core.ui.settings.theme.topo_day", fallback: "Topo Day" },
        description: { key: "core.ui.settings.theme.topo_day_desc", fallback: "Paper background with crisp frost surfaces." },
      },
      {
        id: "topo-night",
        name: { key: "core.ui.settings.theme.topo_night", fallback: "Topo Night" },
        description: { key: "core.ui.settings.theme.topo_night_desc", fallback: "Deep contrast with controlled glass depth." },
      },
    ];
    const customThemes = Object.values(themesById).filter((theme) => !isBuiltinThemeId(theme.id) && theme.id !== "default");
    return [
      ...builtinThemes,
      ...customThemes,
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

  const host: ToposyncHost = useMemo(
    () => ({
      registerElementType(elementType) {
        setElementTypesById((prev) => ({ ...prev, [elementType.type]: elementType }));
      },
      registerNotificationHistoryAssistant(assistant) {
        setHistoryAssistants(previous => ({ ...previous, [assistant.id]: assistant }));
      },
      registerNotificationHistoryFilter(filter) {
        setNotificationHistoryFilters(previous => ({ ...previous, [filter.id]: filter }));
      },
      registerNotificationDetailPanel(panel) {
        setNotificationDetailPanelsById((prev) => ({ ...prev, [panel.id]: panel }));
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
      registerPipelineReviewPanel(panel) {
        setPipelineReviewPanels(previous => ({ ...previous, [panel.id]: panel }));
      },
      registerPipelineOperatorPanel(panel) {
        setPipelineOperatorPanelsByOperatorId((prev) => ({ ...prev, [panel.operatorId]: panel }));
      },
      registerRenderView(view) {
        setRenderViewsById((prev) => ({ ...prev, [view.id]: view }));
      },
      registerTheme(theme) {
        setThemesById((prev) => ({ ...prev, [theme.id]: theme }));
      },
      api: {
        emitEvent,
        getDevice,
        fetch: (input, init) => fetch(resolveToposyncUrl(input), init),
        resolveUrl: resolveToposyncUrl,
      },
      i18n,
      ui: {
        openNotificationHistory: (selection, returnToNotification) => {
          setHistoryRequest({ selection, returnToNotification });
          navigate("/");
        },
        NavigableViewport,
        ImageMaskEditor,
        CameraRegionEditor: ({ open, cameraId, sourceId, values, onChange, onClose, onSnapshotBindingChange, onConfirm }) => sourceId ? (
          <CropRectangleDrawModal open={open} onClose={onClose} units="percent" values={values} onChange={onChange}
            snapshotSource={{ kind: "camera", cameraId, sourceId }} onSnapshotBindingChange={onSnapshotBindingChange} onConfirm={onConfirm} />
        ) : null,
        Viewport2DReplica: ({
          session,
          className,
          style,
          initialFit,
          initialCenter,
          initialScale,
          initialSpan,
          interactionMode,
          minScale,
          maxScale,
          displayRotationDegrees,
        }) => {
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
                interactionMode={interactionMode ?? "navigate"}
                activeToolSession={session ?? null}
                enableKeyboardShortcuts={false}
                toolSnapToGrid={false}
                initialFit={initialFit}
                initialCenter={initialCenter}
                initialScale={initialScale}
                initialSpan={initialSpan}
                minScale={minScale}
                maxScale={maxScale}
                displayRotationDegrees={displayRotationDegrees}
              />
            </div>
          );
        },
        LiveViewPlayer: ({ cameraId, liveViewId, sourceId, active, controls, onFrame, context, className, style }) => {
          const normalizedContext: StreamsDashboardContext =
            context === "thumbnail" ||
            context === "large" ||
            context === "fullscreen" ||
            context === "pip" ||
            context === "ptz"
              ? context
              : "large";
          return (
            <div className={className} style={{ position: "relative", minHeight: 0, ...style }}>
              <StreamsDashboard
                uiVisible={true}
                isActive={active !== false}
                embedded={true}
                cameraId={cameraId}
                liveViewId={liveViewId}
                defaultContext={normalizedContext}
                sourceId={sourceId}
                controls={controls}
                onFrame={onFrame}
              />
            </div>
          );
        },
      },
    }),
    [],
  );

  useEffect(() => {
    host.registerElementType(createMeasurementLineElementType());
  }, [host]);

  useEffect(() => {
    let cancelled = false;

    async function hydrate() {
      try {
        const [index, fromBackend] = await Promise.all([listCompositions(), getComposition()]);
        if (cancelled) return;

        setCompositions(index.compositions);
        setActiveCompositionId(index.active_composition_id);

        compositionRef.current = fromBackend;
        // Loading a server snapshot is not an edit and must not schedule a write.
        persistedCompositionRef.current = fromBackend;
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
      } catch (err) {
        console.error("Failed to load composition from backend", err);
        setActiveCompositionId(compositionRef.current.id || "ground");
        setBackendAvailable(false);
      } finally {
        if (!cancelled) {
          markToposyncPerformance("composition-loaded", {
            compositionId: compositionRef.current.id,
            elements: compositionRef.current.elements.length,
          });
          setCompositionLoaded(true);
        }
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

  const upsertNotification = useCallback((next: Notification, _op: "insert" | "update") => {
    activeImageSessionRef.current?.observe(next);
    const persistable = withoutEphemeralImage(next);
    setHistorySource(previous => previous?.id === next.id && previous.notification ? { id: next.id, notification: persistable } : previous);
    setNotificationsState((prev) => upsertNotificationInState(prev, persistable, notificationsFilterRef.current));
  }, []);

  const historyEndpoint = (filter: NotificationsFilter): string | undefined => {
    if (!filter.extension) return undefined;
    const provider = notificationHistoryFilters[filter.extension.id];
    if (!provider) throw new Error("Notification selection unavailable");
    const endpoint = provider.endpoint(filter.extension.value);
    if (!endpoint) throw new Error("Notification selection unavailable");
    return endpoint;
  };

  const refreshHistorySource = useCallback(async () => {
    const id = historySourceRef.current?.id;
    if (!id) return;
    historySourceRead.current?.abort();
    const controller = new AbortController(); historySourceRead.current = controller;
    setHistorySource(previous => previous?.id === id ? { id, notification: null } : previous);
    try {
      const notification = await getNotification(id, { signal: controller.signal });
      if (notification.id !== id) throw new Error("Unexpected notification response");
      if (!controller.signal.aborted) setHistorySource(previous => previous?.id === id ? { id, notification: withoutEphemeralImage(notification) } : previous);
    } catch {
      if (!controller.signal.aborted) {
        setHistorySource(previous => previous?.id === id ? null : previous);
        setActiveNotificationId(previous => previous === id ? null : previous);
        setNotificationsState(previous => {
          const { [id]: removed, ...byId } = previous.byId;
          return { byId, visibleIds: previous.visibleIds.filter(value => value !== id) };
        });
      }
    }
  }, []);

  useEffect(() => {
    const refresh = () => { void refreshHistorySource(); };
    const subscriptions = Object.values(notificationHistoryFilters).map(provider => provider.subscribe?.(refresh));
    window.addEventListener("focus", refresh);
    return () => { subscriptions.forEach(unsubscribe => unsubscribe?.()); window.removeEventListener("focus", refresh); };
  }, [notificationHistoryFilters, refreshHistorySource]);

  useEffect(() => {
    const provider = notificationsRequestFilter.extension && notificationHistoryFilters[notificationsRequestFilter.extension.id];
    if (!provider) return;
    const invalidate = () => {
      abortNotificationListRequests();
      setNotificationsState({ byId: {}, visibleIds: [] });
      setNotificationsRequestFilter(previous => ({ ...previous }));
    };
    const unsubscribe = provider.subscribe?.(invalidate);
    window.addEventListener("focus", invalidate);
    return () => { unsubscribe?.(); window.removeEventListener("focus", invalidate); };
  }, [notificationHistoryFilters, notificationsRequestFilter.extension, abortNotificationListRequests]);

  const activeNotificationIsOpenRealtime = useMemo(() => {
    return isOpenRealtimeNotification(activeNotification);
  }, [activeNotification]);

  useEffect(() => {
    if (!backendAvailable) return;
    notificationsCountAbortRef.current?.abort();
    const controller = new AbortController();
    notificationsCountAbortRef.current = controller;
    void (async () => {
      try {
        const count = await getNotificationsCount({ signal: controller.signal });
        if (controller.signal.aborted) return;
        setNotificationsCount(count);
      } catch (err) {
        if (isAbortError(err)) return;
        console.error("Failed to load notifications count", err);
      } finally {
        if (notificationsCountAbortRef.current === controller) notificationsCountAbortRef.current = null;
      }
    })();
    return () => {
      controller.abort();
    };
  }, [backendAvailable, notificationsSyncRevision]);

  useEffect(() => {
    if (!backendAvailable) return;
    if (!mainViewportReady || (notificationsRequestFilter.extension && !notificationHistoryFilters[notificationsRequestFilter.extension.id] && !allExtensionsLoaded)) {
      setNotificationsLoading(true);
      return;
    }

    abortNotificationListRequests();
    const controller = new AbortController();
    notificationsListAbortRef.current = controller;
    const requestId = notificationsListRequestRef.current + 1;
    notificationsListRequestRef.current = requestId;
    setNotificationsLoading(true);
    setNotificationsCursor(null);
    setNotificationsHistoryChanged(false);
    setNotificationsSearchStatus(undefined);
    setNotificationsError(false);
    setNotificationsHasMore(true);
    if (!notificationsLoadedFilterRef.current || !notificationFiltersEqual(notificationsLoadedFilterRef.current, notificationsRequestFilter)) {
      setNotificationsState((prev) => notificationsRequestFilter.extension ? { byId: {}, visibleIds: [] } : ({ ...prev, visibleIds: [] }));
    }

    void (async () => {
      try {
        const page = await listNotifications({
          before: null,
          limit: 40,
          priorities: notificationsRequestFilter.priorities,
          types: notificationsRequestFilter.types,
          query: notificationsRequestFilter.query,
          semantic: notificationsRequestFilter.semantic,
          createdAfter: notificationsRequestFilter.timeRange?.createdAfter,
          createdBefore: notificationsRequestFilter.timeRange?.createdBefore,
          dataFilter: notificationsRequestFilter.dataFilter,
          historyEndpoint: historyEndpoint(notificationsRequestFilter),
          signal: controller.signal,
        });
        if (controller.signal.aborted || notificationsListRequestRef.current !== requestId) return;
        notificationsLoadedFilterRef.current = notificationsRequestFilter;
        markToposyncPerformance("notifications-page-loaded", {
          page: "initial",
          count: page.notifications?.length ?? 0,
          hasMore: page.next_cursor != null,
        });
        setNotificationsState((prev) =>
          mergeNotificationPage(prev, page.notifications ?? [], {
            replaceVisible: true,
            filter: notificationsRequestFilter,
            ranked: page.search?.mode === "semantic_visual" || page.search?.mode === "semantic_text",
            resolvedOnServer: Boolean((notificationsRequestFilter.timeRange && notificationsRequestFilter.query.trim()) || page.search?.time_range || page.search?.reason === "installation_timezone_required"),
          }),
        );
        setNotificationsSearchStatus(page.search);
        setNotificationsCursor(page.next_cursor ?? null);
        setNotificationsHasMore(page.next_cursor != null);
      } catch (err) {
        if (isAbortError(err) || controller.signal.aborted || notificationsListRequestRef.current !== requestId) return;
        setNotificationsError(true);
        setNotificationsHasMore(false);
        console.error("Failed to load notifications", err);
      } finally {
        if (notificationsListAbortRef.current === controller) notificationsListAbortRef.current = null;
        if (!controller.signal.aborted && notificationsListRequestRef.current === requestId) setNotificationsLoading(false);
      }
    })();

    return () => {
      controller.abort();
    };
  }, [abortNotificationListRequests, backendAvailable, mainViewportReady, notificationsRequestFilter, notificationHistoryFilters, allExtensionsLoaded, notificationsSyncRevision]);

  useEffect(() => {
    if (activeNotificationId && (notificationsState.byId[activeNotificationId] || historySource?.id === activeNotificationId)) return;
    if (notifications.length > 0) {
      setActiveNotificationId(notifications[0].id);
      return;
    }
    if (activeNotificationId) setActiveNotificationId(null);
  }, [activeNotificationId, historySource, notifications, notificationsState.byId]);

  useEffect(() => {
    if (!backendAvailable) return;

    let closed = false;
    // This global stream is intentionally separate from the selected-notification stream below.
    // The list/cache must keep receiving global realtime updates while details track one item.
    const es = new EventSource(resolveToposyncUrl("/api/notifications/stream"));
    const clearHistory = () => {
      abortNotificationListRequests();
      activeNotificationFetchAbortRef.current?.abort();
      activeNotificationRefreshAbortRef.current?.abort();
      historySourceRead.current?.abort();
      activeImageSessionRef.current?.clear();
      setNotificationsState({ byId: {}, visibleIds: [] });
      setHistorySource(null);
      setActiveNotificationId(null);
    };
    const activityChanged = () => { clearHistory(); resync(); };
    window.addEventListener("toposync:activity-changed", activityChanged);
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const resync = (force = true) => {
      if (closed) return;
      const mode = notificationHistoryRefreshMode(notificationsFilterRef.current, notificationsResolvedOnServerRef.current);
      if (mode === "manual") { setNotificationsHistoryChanged(true); return; }
      if (!force && mode === "none") return;
      if (refreshTimer !== undefined) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = undefined;
        if (!closed && notificationHistoryRefreshMode(notificationsFilterRef.current, notificationsResolvedOnServerRef.current) !== "manual") {
          setNotificationsSyncRevision(value => value + 1);
        }
      }, 500);
    };
    es.onopen = () => resync();
    es.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data ?? "{}") as { op?: string; notification?: Notification };
        if (parsed.op === "resync" || parsed.op === "invalidate") { activityChanged(); return; }
        const op = parsed.op === "update" ? "update" : "insert";
        const notif = parsed.notification;
        if (!notif || typeof notif.id !== "string") return;
        upsertNotification(notif, op);
        resync(false);

        if (op === "insert") {
          const bucket = notificationPriority(notif);
          setNotificationsCount((prev) => ({
            total: prev.total + 1,
            by_priority: { ...prev.by_priority, [bucket]: prev.by_priority[bucket] + 1 },
            unread_total: prev.unread_total + 1,
            unread_by_priority: {
              ...prev.unread_by_priority,
              [bucket]: prev.unread_by_priority[bucket] + 1,
            },
          }));

          const now = Date.now();
          const idle = now - lastUserInteractionTsRef.current > 12_000;
          const allowAuto = !notificationDetailsOpenRef.current && (!hasManualNotificationSelectionRef.current || idle);
          if (allowAuto) setActiveNotificationId(notif.id);
        }
      } catch (err) {
        console.warn("Failed to parse notifications SSE", err);
      }
    };
    es.onerror = (err) => {
      if (closed) return;
      clearHistory();
      console.warn("Notifications SSE error", err);
    };

    return () => {
      closed = true;
      clearTimeout(refreshTimer);
      window.removeEventListener("toposync:activity-changed", activityChanged);
      es.close();
    };
  }, [backendAvailable, upsertNotification, abortNotificationListRequests]);

  useEffect(() => {
    if (!backendAvailable) return;
    if (!activeNotificationId) return;

    let closed = false;
    const imageSession = createEphemeralImageSession(activeNotificationId, (lease) => {
      if (!closed) setActiveImageLease(lease);
    });
    activeImageSessionRef.current = imageSession;
    activeNotificationFetchAbortRef.current?.abort();
    const controller = new AbortController();
    activeNotificationFetchAbortRef.current = controller;
    let refreshPending = false;
    let refreshRequested = false;
    const refreshSelectedNotification = () => {
      if (closed) return;
      if (refreshPending) { refreshRequested = true; return; }
      refreshPending = true;
      refreshRequested = false;
      void getNotification(activeNotificationId, { signal: controller.signal })
      .then((notif) => {
        if (controller.signal.aborted) return;
        upsertNotification(notif, "update");
      })
      .catch((err) => {
        if (isAbortError(err)) return;
        console.warn("Failed to fetch active notification", err);
      })
      .finally(() => {
        refreshPending = false;
        if (activeNotificationFetchAbortRef.current === controller) activeNotificationFetchAbortRef.current = null;
        if (refreshRequested) refreshSelectedNotification();
      });
    };
    refreshSelectedNotification();

    // Keep this selected-notification stream separate from the global list stream above.
    // Changing selection should only replace this stream, not the global notification stream.
    const es = new EventSource(resolveToposyncUrl(`/api/notifications/${encodeURIComponent(activeNotificationId)}/stream?include_ephemeral_image=true`));
    // Reconnect/open never restores the previous image, even for the same capture.
    es.onopen = () => { if (!closed) imageSession.clear(); };
    es.onmessage = (ev) => {
      if (closed) return;
      try {
        if (typeof ev.data !== "string" || ev.data.length > 512 * 1024) { imageSession.clear(); refreshSelectedNotification(); return; }
        const parsed = JSON.parse(ev.data ?? "{}") as { op?: string; notification?: Notification };
        if (parsed.op === "invalidate") {
          imageSession.clear();
          window.dispatchEvent(new Event("toposync:activity-changed"));
          return;
        }
        if (parsed.op === "resync") { imageSession.clear(); refreshSelectedNotification(); return; }
        const op = parsed.op === "update" ? "update" : "insert";
        const notif = parsed.notification;
        if (!notif || typeof notif.id !== "string" || notif.id !== activeNotificationId) { imageSession.clear(); return; }
        imageSession.receive(notif);
        upsertNotification(notif, op);
      } catch (err) {
        imageSession.clear();
        console.warn("Failed to parse notification detail SSE");
      }
    };
    es.onerror = (err) => {
      if (closed) return;
      imageSession.clear();
      console.warn("Notification detail SSE error", err);
    };

    return () => {
      controller.abort();
      closed = true;
      imageSession.dispose();
      if (activeImageSessionRef.current === imageSession) activeImageSessionRef.current = null;
      setActiveImageLease((current) => current?.notificationId === activeNotificationId ? null : current);
      es.close();
    };
  }, [activeNotificationId, backendAvailable, upsertNotification]);

  useEffect(() => {
    if (!backendAvailable) return;
    if (!activeNotificationId) return;
    if (!activeNotificationIsOpenRealtime) return;

    const refreshActiveNotification = () => {
      activeNotificationRefreshAbortRef.current?.abort();
      const controller = new AbortController();
      activeNotificationRefreshAbortRef.current = controller;
      void getNotification(activeNotificationId, { signal: controller.signal })
        .then((notif) => {
          if (controller.signal.aborted) return;
          upsertNotification(notif, "update");
        })
        .catch((err) => {
          if (isAbortError(err)) return;
          console.warn("Failed to refresh active notification", err);
        })
        .finally(() => {
          if (activeNotificationRefreshAbortRef.current === controller) activeNotificationRefreshAbortRef.current = null;
        });
    };

    const handle = window.setInterval(refreshActiveNotification, 2500);
    return () => {
      window.clearInterval(handle);
      activeNotificationRefreshAbortRef.current?.abort();
      activeNotificationRefreshAbortRef.current = null;
    };
  }, [activeNotificationId, activeNotificationIsOpenRealtime, backendAvailable, upsertNotification]);

  useEffect(() => {
    if (!compositionLoaded) return;
    if (!backendAvailable) return;

    if (composition === persistedCompositionRef.current) return;
    // An edit invalidates that snapshot, including an undo back to the loaded value.
    persistedCompositionRef.current = null;
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

  const updateInstallationTimezone = useCallback(async (timezone: string | null): Promise<void> => {
    const saved = await setInstallationTimezone(timezone);
    setSettings((previous) => ({ ...previous, core: { ...previous.core, timezone: saved.timezone } }));
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

  const criticalExtensionIds = useMemo(() => {
    const ids = new Set<string>();
    for (const element of composition.elements) {
      const extensionId = extensionIdFromElementType(element.type);
      if (extensionId) ids.add(extensionId);
    }

    const savedRenderMode = loadSavedRenderMode();
    if (savedRenderMode === "spatial_video" || savedRenderMode === "spatial_video_3d") {
      ids.add(SPATIAL_VIDEO_EXTENSION_ID);
    }

    return Array.from(ids).sort((left, right) => left.localeCompare(right));
  }, [composition.elements]);

  const criticalExtensionKey = criticalExtensionIds.join("|");

  const getFrontendExtensions = useCallback(async (): Promise<ExtensionRecord[]> => {
    if (!extensionRecordsPromiseRef.current) {
      extensionRecordsPromiseRef.current = fetchExtensions();
    }
    let exts: ExtensionRecord[];
    try {
      exts = await extensionRecordsPromiseRef.current;
    } catch (error) {
      // Catalog access is not a prerequisite for access to a streaming API.
      setStreamingAvailability("unknown");
      throw error;
    }
    setStreamingAvailability(exts.some(ext => ext.api_prefixes?.includes("/api/streams")) ? "available" : "unavailable");
    return exts.filter((ext) => ext.frontend && ext.frontend.kind === "module-federation");
  }, []);

  const activateFrontendExtension = useCallback(
    async (ext: ExtensionRecord): Promise<void> => {
      if (activatedExtensionIdsRef.current.has(ext.id)) return;
      const existing = extensionActivationPromisesRef.current.get(ext.id);
      if (existing) return existing;

      const frontend = ext.frontend;
      if (!frontend) return;

      const promise = (async () => {
        const activate = await loadRemoteActivate(frontend.remote_entry_url, frontend.scope, frontend.module);
        await activate(host);
        activatedExtensionIdsRef.current.add(ext.id);
        setExtensionActivationDiagnostics((prev) => {
          if (!prev[ext.id]) return prev;
          const next = { ...prev };
          delete next[ext.id];
          return next;
        });
      })();

      extensionActivationPromisesRef.current.set(ext.id, promise);
      try {
        await promise;
      } finally {
        if (!activatedExtensionIdsRef.current.has(ext.id)) {
          extensionActivationPromisesRef.current.delete(ext.id);
        }
      }
    },
    [host],
  );

  useEffect(() => {
    if (!compositionLoaded) return;

    let cancelled = false;
    let cancelIdle: (() => void) | null = null;

    async function run() {
      setCriticalExtensionsLoaded(false);
      setAllExtensionsLoaded(false);
      try {
        const frontendExts = await getFrontendExtensions();
        const criticalIds = new Set(criticalExtensionIds);
        const criticalFrontendExts = frontendExts.filter((ext) => criticalIds.has(ext.id));
        await Promise.all(
          criticalFrontendExts.map(async (ext) => {
            try {
              await activateFrontendExtension(ext);
            } catch (err) {
              if (cancelled) return;
              setExtensionActivationDiagnostics((prev) => ({
                ...prev,
                [ext.id]: err instanceof Error ? err.message : String(err),
              }));
              console.error(`[extension:${ext.id}]`, err);
            }
          }),
        );
        if (cancelled) return;
        markToposyncPerformance("critical-extensions-loaded", {
          critical: criticalFrontendExts.length,
          total: frontendExts.length,
        });
        setCriticalExtensionsLoaded(true);

        cancelIdle = scheduleIdle(() => {
          void (async () => {
            await Promise.all(
              frontendExts.map(async (ext) => {
                try {
                  await activateFrontendExtension(ext);
                } catch (err) {
                  if (cancelled) return;
                  setExtensionActivationDiagnostics((prev) => ({
                    ...prev,
                    [ext.id]: err instanceof Error ? err.message : String(err),
                  }));
                  console.error(`[extension:${ext.id}]`, err);
                }
              }),
            );
            if (cancelled) return;
            markToposyncPerformance("all-extensions-loaded", { total: frontendExts.length });
            setAllExtensionsLoaded(true);
          })();
        }, 1800);
      } catch (err) {
        if (!cancelled) console.error("Failed to load extensions", err);
        if (!cancelled) {
          setCriticalExtensionsLoaded(true);
          setAllExtensionsLoaded(true);
        }
      }
    }

    void run();
    return () => {
      cancelled = true;
      cancelIdle?.();
    };
  }, [activateFrontendExtension, compositionLoaded, criticalExtensionKey, getFrontendExtensions]);

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

    const requestId = notificationsListRequestRef.current;
    const filter = notificationsFilterRef.current;
    notificationsMoreAbortRef.current?.abort();
    const controller = new AbortController();
    notificationsMoreAbortRef.current = controller;
    setNotificationsLoading(true);
    try {
      const page = await listNotifications({
        before: notificationsCursor,
        limit: 40,
        priorities: filter.priorities,
        types: filter.types,
        query: filter.query,
        dataFilter: filter.dataFilter,
        historyEndpoint: historyEndpoint(filter),
        semantic: filter.semantic,
        createdAfter: filter.timeRange?.createdAfter,
        createdBefore: filter.timeRange?.createdBefore,
        signal: controller.signal,
      });
      if (controller.signal.aborted || notificationsListRequestRef.current !== requestId) return;
      markToposyncPerformance("notifications-page-loaded", {
        page: "next",
        count: page.notifications.length,
        hasMore: page.next_cursor != null,
      });
      setNotificationsCursor(page.next_cursor ?? null);
      setNotificationsHasMore(page.next_cursor != null);
      if (page.notifications.length === 0) return;
      setNotificationsState((prev) =>
        mergeNotificationPage(prev, page.notifications, {
          replaceVisible: false,
          filter,
          ranked: page.search?.mode === "semantic_visual" || page.search?.mode === "semantic_text",
          resolvedOnServer: Boolean((filter.timeRange && filter.query.trim()) || page.search?.time_range || page.search?.reason === "installation_timezone_required"),
        }),
      );
    } catch (err) {
      if (isAbortError(err) || controller.signal.aborted || notificationsListRequestRef.current !== requestId) return;
      setNotificationsState(previous => filter.extension ? { byId: {}, visibleIds: [] } : ({ ...previous, visibleIds: [] }));
      setNotificationsHasMore(false);
      setNotificationsCursor(null);
      setNotificationsError(true);
      console.error("Failed to load more notifications", err);
    } finally {
      if (notificationsMoreAbortRef.current === controller) notificationsMoreAbortRef.current = null;
      if (!controller.signal.aborted && notificationsListRequestRef.current === requestId) setNotificationsLoading(false);
    }
  }, [backendAvailable, notificationsCursor, notificationsHasMore, notificationsLoading, notificationHistoryFilters]);

  const markNotificationsAsViewed = useCallback(() => {
    if (!backendAvailable) return;
    if (markNotificationsViewedInFlightRef.current) return;

    const promise = (async () => {
      try {
        const count = await markNotificationsViewed();
        setNotificationsCount(count);
      } catch (err) {
        console.error("Failed to mark notifications viewed", err);
      } finally {
        markNotificationsViewedInFlightRef.current = null;
      }
    })();

    markNotificationsViewedInFlightRef.current = promise;
  }, [backendAvailable]);

  const selectNotification = useCallback((notificationId: string) => {
    setHistorySource(previous => previous?.id === notificationId ? previous : null);
    setActiveNotificationId(notificationId);
    hasManualNotificationSelectionRef.current = true;
    lastUserInteractionTsRef.current = Date.now();
  }, []);

  const openHistoryNotification = useCallback(async (id: string) => {
    const notification = await getNotification(id);
    if (notification.id !== id) throw new Error("Unexpected notification response");
    setNotificationsState(previous => ({ ...previous,
      byId: { ...previous.byId, [id]: withoutEphemeralImage(notification) },
    }));
    selectNotification(id);
    setHistorySource({ id, notification: withoutEphemeralImage(notification) });
  }, [selectNotification]);

  const markMainViewportReady = useCallback(() => {
    if (!mainViewportReadyMarkedRef.current) {
      mainViewportReadyMarkedRef.current = true;
      markToposyncPerformance("first-viewport-mounted", {
        compositionId: compositionRef.current.id,
        elements: compositionRef.current.elements.length,
      });
    }
    setMainViewportReady(true);
  }, []);

  const normalizedPathname = useMemo(() => {
    const raw = pathname || "/";
    if (raw.length > 1 && raw.endsWith("/")) return raw.slice(0, -1);
    return raw;
  }, [pathname]);

  const lastNonSettingsPathRef = useRef<string>("/");
  useEffect(() => {
    if (screen === "main" && !normalizedPathname.startsWith("/settings")) lastNonSettingsPathRef.current = normalizedPathname;
  }, [normalizedPathname, screen]);

  const openSettings = useCallback(() => navigate("/settings"), []);
  const openPipelinesSettings = useCallback(() => navigate("/settings/pipelines"), []);
  const openProcessingServersSettings = useCallback(() => navigate("/settings/processing-servers"), []);
  const openAccessSettings = useCallback(() => navigate("/settings/access"), []);
  const openCompositionEditorFromSettings = useCallback((elementId?: string, initialAction?: string) => {
    editorReturnPath.current = normalizedPathname;
    setEditorReturnElementId(elementId ?? null);
    setEditorInitialAction(initialAction);
    replace("/");
    setScreen("editor");
  }, [normalizedPathname]);

  useEffect(() => {
    if (normalizedPathname.startsWith("/settings")) {
      setEditorReturnElementId(null);
      setEditorInitialAction(undefined);
    }
  }, [normalizedPathname]);

  const closeSettings = useCallback(() => replace(lastNonSettingsPathRef.current || "/"), []);

  const closeSettingsChild = useCallback(() => {
    const prev = getPreviousPathname();
    if (prev && prev.startsWith("/settings")) {
      window.history.back();
      return;
    }
    replace("/settings");
  }, []);

  return (
    <StreamingAvailabilityContext.Provider value={streamingAvailability}>
    <div className="appShell">
      {normalizedPathname.startsWith("/streams/debug") ? (
        <StreamingAvailabilityBoundary><StreamTransportDebugScreen /></StreamingAvailabilityBoundary>
      ) : normalizedPathname.startsWith("/settings/pipelines") ? (
        <PipelinesScreen
          onClose={closeSettingsChild}
          onOpenProcessingServers={openProcessingServersSettings}
          operatorPanels={pipelineOperatorPanelsByOperatorId}
          reviewPanels={Object.values(pipelineReviewPanels)}
          assistants={Object.values(settingsPanelsById).filter(panel => panel.pipelineAssistant)}
        />
      ) : normalizedPathname.startsWith("/settings/processing-servers") ? (
        <ProcessingServersScreen
          onClose={closeSettingsChild}
          canManageProvisioning={Boolean(authMode === "bypass" || (authUser && (authUser.role === "owner" || authUser.role === "admin")))}
        />
      ) : normalizedPathname.startsWith("/settings/access") ? (
        <AccessScreen
          authUser={authUser}
          authMode={authMode}
          onClose={closeSettingsChild}
          onLogout={onLogout}
          listAccessUsers={listAccessUsers}
          createAccessUser={createAccessUser}
          startAccessUserPairing={startAccessUserPairing}
          patchAccessUser={patchAccessUser}
          deleteAccessUser={deleteAccessUser}
        />
      ) : normalizedPathname.startsWith("/settings") ? (
        <SettingsScreen
          operatorPanels={pipelineOperatorPanelsByOperatorId}
          backendAvailable={backendAvailable}
          api={host.api}
          wallHeightPreset={wallHeightPreset}
          ghostWalls={ghostWalls}
          graphicsQuality={graphicsQuality}
          onSetWallHeightPreset={setWallHeightPreset}
          onSetGhostWalls={setGhostWalls}
          onSetGraphicsQuality={setGraphicsQuality}
          renderViews={Object.values(renderViewsById)}
          renderViewSettings={renderViewSettings}
          onPatchRenderViewSettings={patchRenderViewSettings}
          panels={Object.values(settingsPanelsById)}
          themes={themeOptions}
          themeId={themeId}
          onSetThemeId={setThemeId}
          viewport3dBackground={viewport3dBackground}
          onSetViewport3dBackground={setViewport3dBackground}
          settings={settings}
          onPatchExtensionSettings={updateExtensionSettings}
          onSetInstallationTimezone={updateInstallationTimezone}
          extensionActivationDiagnostics={extensionActivationDiagnostics}
          onOpenPipelines={openPipelinesSettings}
          onOpenProcessingServers={openProcessingServersSettings}
          onOpenAccess={openAccessSettings}
          compositions={compositions}
          activeCompositionId={activeCompositionId}
          onActivateComposition={activateCompositionById}
          onCreateComposition={createNewComposition}
          onRenameComposition={renameExistingComposition}
          onDeleteComposition={deleteExistingComposition}
          onOpenCompositionEditor={openCompositionEditorFromSettings}
          canManageAccess={Boolean(authMode === "bypass" || (authUser && (authUser.role === "owner" || authUser.role === "admin")))}
          authUser={authUser}
          onLogout={onLogout}
          onClose={closeSettings}
        />
      ) : screen === "main" ? (
        <MainScreen
          compositionName={composition.name}
          georeference={composition.meta?.georeference}
          compositions={compositions}
          activeCompositionId={activeCompositionId}
          compositionLoaded={compositionLoaded}
          criticalExtensionsLoaded={criticalExtensionsLoaded}
          allExtensionsLoaded={allExtensionsLoaded}
          elements={composition.elements}
          elementTypesById={elementTypesById}
          viewSettings={viewSettings}
          notificationRenderers={notificationRenderers}
          notificationHistoryFilters={Object.values(notificationHistoryFilters)}
          historyAssistants={Object.values(historyAssistants)}
          historyRequest={historyRequest}
          onHistoryRequestHandled={() => setHistoryRequest(null)}
          onOpenHistoryNotification={openHistoryNotification}
          notificationsError={notificationsError}
          notificationDetailPanels={Object.values(notificationDetailPanelsById)}
          notifications={notifications}
          notificationsCount={notificationsCount}
          notificationsHasMore={notificationsHasMore}
          notificationsFilter={notificationsFilter}
          notificationsSearchStatus={notificationsSearchStatus}
          notificationsHistoryChanged={notificationsHistoryChanged}
          activeNotificationId={activeNotificationId}
          activeNotification={activeNotification}
          notificationsLoading={notificationsLoading}
          renderViews={Object.values(renderViewsById)}
          onNotificationsFilterChange={setNotificationsFilter}
          onNotificationsSearchRefresh={() => {
            const next = { ...notificationsFilter, timeRange: resolveNotificationPeriod(notificationsFilter.period) };
            setNotificationsFilter(next); setNotificationsRequestFilter(next);
          }}
          onSelectNotification={selectNotification}
          onNotificationDetailsOpenChange={setNotificationDetailsOpen}
          onLoadMoreNotifications={loadMoreNotifications}
          onNotificationsViewed={markNotificationsAsViewed}
          api={host.api}
          updateElement={updateElement}
          geographicView={geographicView}
          onEditComposition={(initialAction, elementId) => { editorReturnPath.current = null; setEditorReturnElementId(elementId ?? null); setEditorInitialAction(initialAction); setScreen("editor"); }}
          onOpenPipelines={openPipelinesSettings}
          onOpenSettings={openSettings}
          onActivateComposition={activateCompositionById}
          onCreateComposition={createNewComposition}
          onRenameComposition={renameExistingComposition}
          onDeleteComposition={deleteExistingComposition}
          onViewportReady={markMainViewportReady}
        />
      ) : screen === "editor" ? (
        <CompositionEditorScreen
          initialEditingElementId={editorReturnElementId}
          initialEditingAction={editorInitialAction}
          activeNotification={activeNotification}
          notificationRenderers={notificationRenderers}
          georeference={composition.meta?.georeference}
          onSaveGeoreference={async (georeference, expected) => {
            const snapshot = compositionRef.current;
            await patchCompositionMeta(snapshot.id, { georeference }, { georeference: expected ?? null });
            recordHistoryBeforeChange(snapshot);
            setComposition(current => {
              if (current.id !== snapshot.id) return current;
              const next = { ...current, meta: { ...current.meta, georeference } };
              if (current === snapshot) persistedCompositionRef.current = next;
              return next;
            });
            setCompositionRevision(value => value + 1);
          }}
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
          onExit={() => {
            setEditorReturnElementId(null);
            setEditorInitialAction(undefined);
            setScreen("main");
            if (editorReturnPath.current) replace(editorReturnPath.current);
            editorReturnPath.current = null;
          }}
          onOpenSettings={openSettings}
          onOpenAutomatic={targetId => navigate(`/settings?${new URLSearchParams({ panel: "__automatic__", automatic_target: targetId })}`)}
          onActivateComposition={activateCompositionById}
          onCreateComposition={createNewComposition}
          onRenameComposition={renameExistingComposition}
          onDeleteComposition={deleteExistingComposition}
        />
      ) : null}
    </div>
    </StreamingAvailabilityContext.Provider>
  );
}
