import type { GeographicView } from "../geographic/GeographicMap";
import { StreamingAvailabilityContext } from "../streams/StreamingAvailability";
import React, { useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostApi,
  Notification,
  NotificationRenderer,
  NotificationDetailPanel,
  NotificationHistoryFilter,
  NotificationHistoryAssistant,
  RenderViewDefinition,
  ViewSettings,
} from "@toposync/plugin-api";

import {
  isAbortError,
  listStreamingCameraLiveViews,
  type Composition,
  type CompositionSummary,
  type NotificationsCount,
  type StreamingCameraLiveView,
} from "../../util/api";
import { i18n, resolveLocalizedString } from "../../util/i18n";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { FullscreenImageViewer, requestFullscreenImageViewer, type FullscreenImageViewerItem } from "../FullscreenImageViewer";
import { Icon } from "../Icon";
import { Viewport3D } from "../Viewport3D";
import { MainViewport2D } from "../main2d/MainViewport2D";
import { MainViewportVector2D } from "../main2d/MainViewportVector2D";
import { MainGeographicViewport } from "../geographic/MainGeographicViewport";
import {
  notificationImageItems,
  notificationDescription,
  notificationPriority,
  notificationThumbnailUrl,
  renderNotificationSpatialStatus,
  type NotificationImageItem,
} from "../notifications/pipelinesNotifications";
import { HistoryLocationFilter, useHistoryLocationOptions } from "../notifications/HistoryLocationFilter";
import { HistoryPeriodFilter } from "../notifications/HistoryPeriodFilter";
import { resolveNotificationPeriod, type NotificationPeriod, type NotificationTimeRange } from "../../util/notificationPeriod";
import { VisualHistoryPreparation } from "../notifications/VisualHistoryPreparation";
import { StreamsDashboard } from "../streams/StreamsDashboard";
import { PipelineInspectionPanel } from "../streams/PipelineInspectionPanel";
import { navigate, replace, usePathname } from "../router";

type Props = {
  compositionName: string;
  georeference: unknown;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  compositionLoaded: boolean;
  criticalExtensionsLoaded: boolean;
  allExtensionsLoaded: boolean;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  viewSettings: ViewSettings;
  notificationRenderers: NotificationRenderer[];
  notificationDetailPanels?: NotificationDetailPanel[];
  notificationHistoryFilters?: NotificationHistoryFilter[];
  historyAssistants?: NotificationHistoryAssistant[];
  historyRequest?: { selection: { id: string; value: string }; returnToNotification?: string } | null;
  onHistoryRequestHandled: () => void;
  onOpenHistoryNotification: (id: string) => Promise<void>;
  notificationsError?: boolean;
  notifications: Notification[];
  notificationsCount: NotificationsCount;
  notificationsHasMore: boolean;
  notificationsFilter: NotificationsFilter;
  notificationsSearchStatus?: import("../../util/api").NotificationSearchStatus;
  notificationsHistoryChanged?: boolean;
  activeNotificationId: string | null;
  activeNotification: Notification | null;
  notificationsLoading: boolean;
  renderViews: RenderViewDefinition[];
  onNotificationsFilterChange: React.Dispatch<React.SetStateAction<NotificationsFilter>>;
  onNotificationsSearchRefresh: () => void;
  onSelectNotification: (notificationId: string) => void;
  onNotificationDetailsOpenChange: (open: boolean) => void;
  onLoadMoreNotifications: () => void;
  onNotificationsViewed: () => void;
  api: HostApi;
  updateElement: (elementId: string, patch: CompositionElementPatch) => void;
  geographicView: React.MutableRefObject<GeographicView | null>;
  onEditComposition: (initialAction?: string, elementId?: string) => void;
  onOpenPipelines: () => void;
  onOpenSettings: () => void;
  onActivateComposition: (compositionId: string) => Promise<Composition>;
  onCreateComposition: (name: string) => Promise<Composition>;
  onRenameComposition: (compositionId: string, name: string) => Promise<Composition>;
  onDeleteComposition: (compositionId: string) => Promise<void>;
  onViewportReady: () => void;
};

function formatDateTimeShort(locale: string, iso: string | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  try {
    return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(d);
  } catch {
    return d.toLocaleString();
  }
}

type NotificationDetailField = {
  technical?: boolean;
  label: string;
  value: string;
};

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function asTrimmedString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asFiniteNumber(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value;
}

function formatDateTimeLong(locale: string, iso: string | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  try {
    return new Intl.DateTimeFormat(locale, {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(d);
  } catch {
    return d.toLocaleString();
  }
}

function formatTimestampMillis(locale: string, tsMillis: number | undefined): string | null {
  if (!tsMillis || !Number.isFinite(tsMillis)) return null;
  try {
    return new Intl.DateTimeFormat(locale, {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(tsMillis));
  } catch {
    return new Date(tsMillis).toLocaleString();
  }
}

function formatDurationVerbose(secondsRaw: unknown, locale: string): string | null {
  const totalSeconds = asFiniteNumber(secondsRaw);
  if (totalSeconds == null || totalSeconds < 0) return null;
  const roundedSeconds = Math.round(totalSeconds * 10) / 10;
  if (roundedSeconds < 60) return `${roundedSeconds.toLocaleString(locale, { maximumFractionDigits: 1 })}s`;
  const seconds = Math.floor(roundedSeconds);
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (remMins > 0 && secs > 0) return `${hours}h ${remMins}m ${secs}s`;
  if (remMins > 0) return `${hours}h ${remMins}m`;
  return secs > 0 ? `${hours}h ${secs}s` : `${hours}h`;
}

function notificationPayload(notification: Notification): Record<string, unknown> {
  return asRecord(notification.payload);
}

function notificationIsOpenRealtime(notification: Notification | null | undefined): boolean {
  if (!notification) return false;
  const payload = notificationPayload(notification);
  if (payload.source === "pipelines") return false;
  return asTrimmedString(payload.status).toLowerCase() === "open" && payload.realtime === true;
}

function notificationStartedMillis(notification: Notification): number | null {
  const payload = notificationPayload(notification);
  const event = asRecord(payload.event);
  const startedSeconds = asFiniteNumber(event.started_ts);
  if (startedSeconds != null && startedSeconds > 0) return startedSeconds * 1000;
  const createdAt = Date.parse(notification.createdAt ?? "");
  return Number.isFinite(createdAt) && createdAt > 0 ? createdAt : null;
}

function withLiveDuration(notification: Notification, nowMs: number): Notification {
  if (!notificationIsOpenRealtime(notification)) return notification;
  const startedMs = notificationStartedMillis(notification);
  if (startedMs == null) return notification;
  const payload = notificationPayload(notification);
  const event = asRecord(payload.event);
  const previous = asFiniteNumber(event.duration_seconds) ?? 0;
  const durationSeconds = Math.max(previous, (nowMs - startedMs) / 1000);
  return {
    ...notification,
    payload: {
      ...payload,
      event: {
        ...event,
        duration_seconds: durationSeconds,
      },
    },
  };
}

const NOTIFICATIONS_OPEN_STORAGE_KEY = "toposync.notifications_open.v1";
export const NOTIFICATIONS_FILTER_STORAGE_KEY = "toposync.notifications_filter.v2";

export type Priority = "low" | "medium" | "high";
const ALL_PRIORITIES: Priority[] = ["high", "medium", "low"];

export type NotificationsFilter = {
  period?: NotificationPeriod;
  timeRange?: NotificationTimeRange;
  extension?: { id: string; value: string };
  dataFilter?: Record<string, string>;
  semantic?: boolean | "visual";
  priorities: Priority[];
  types: string[]; // empty array = "all types"
  query: string;
};

export const DEFAULT_NOTIFICATIONS_FILTER: NotificationsFilter = {
  priorities: ["high"],
  types: [],
  query: "",
};

function loadNotificationsOpen(): boolean {
  if (typeof window === "undefined") return true;

  try {
    const raw = localStorage.getItem(NOTIFICATIONS_OPEN_STORAGE_KEY);
    if (raw === "0") return false;
    if (raw === "1") return true;
  } catch {
    // ignore
  }

  try {
    return !window.matchMedia("(max-width: 720px)").matches;
  } catch {
    return true;
  }
}

function shouldAutoCloseNotificationsAfterSelect(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.matchMedia("(max-width: 720px)").matches;
  } catch {
    return window.innerWidth <= 720;
  }
}

export function loadNotificationsFilter(): NotificationsFilter {
  if (typeof window === "undefined") return { ...DEFAULT_NOTIFICATIONS_FILTER };
  try {
    const raw = localStorage.getItem(NOTIFICATIONS_FILTER_STORAGE_KEY);
    if (!raw) return { ...DEFAULT_NOTIFICATIONS_FILTER };
    const parsed = JSON.parse(raw) as Partial<NotificationsFilter>;
    const priorities = Array.isArray(parsed.priorities)
      ? parsed.priorities.filter((p): p is Priority => p === "low" || p === "medium" || p === "high")
      : DEFAULT_NOTIFICATIONS_FILTER.priorities;
    const types = Array.isArray(parsed.types) ? parsed.types.filter((t): t is string => typeof t === "string" && t.length > 0) : [];
    const query = typeof parsed.query === "string" ? parsed.query : "";
    let period: NotificationPeriod | undefined;
    let timeRange: NotificationTimeRange | undefined;
    try {
      timeRange = resolveNotificationPeriod(parsed.period);
      period = timeRange ? parsed.period : undefined;
    } catch { /* Discard an invalid saved period, never invalid live input. */ }
    return {
      period, timeRange,
      priorities: priorities.length > 0 ? priorities : DEFAULT_NOTIFICATIONS_FILTER.priorities,
      types,
      query,
      extension: parsed.extension && typeof parsed.extension.id === "string" && parsed.extension.id.length <= 256 && typeof parsed.extension.value === "string" && parsed.extension.value.length <= 1024 ? parsed.extension : undefined,
      semantic: parsed.semantic === "visual" ? "visual" : parsed.semantic === true,
      dataFilter: Object.fromEntries(Object.entries(parsed.dataFilter ?? {}).filter(([key, value]) =>
        (key === "camera_id" || key === "area_id") && typeof value === "string" && value.length > 0 && value.length <= 512)),
    };
  } catch {
    return { ...DEFAULT_NOTIFICATIONS_FILTER };
  }
}

function isFilterRestrictive(filter: NotificationsFilter): boolean {
  if (filter.timeRange) return true;
  if (filter.priorities.length < ALL_PRIORITIES.length) return true;
  if (filter.extension) return true;
  if (filter.types.length > 0) return true;
  if (Object.keys(filter.dataFilter ?? {}).length > 0) return true;
  if (filter.query.trim().length > 0) return true;
  return false;
}

type BuiltinRenderMode = "3d" | "2d" | "vector2d" | "streams" | "camera-live" | "geographic";
type RenderMode = BuiltinRenderMode | string;

const CAMERA_LIVE_BASE_PATH = "/cameras/live";
const RENDER_MODE_STORAGE_KEY = "toposync.render_mode.v1";
const LAST_CAMERA_LIVE_VIEW_STORAGE_KEY = "toposync.streams.last_live_view_id.v1";
const STREAMS_OVERLAY_IDLE_MS = 2500;
const LIVE_VIEWS_REFRESH_MS = 15000;

const BUILTIN_RENDER_MODES = new Set<string>(["3d", "2d", "vector2d", "streams", "camera-live", "geographic"]);

function isBuiltinRenderMode(value: string): value is BuiltinRenderMode {
  return BUILTIN_RENDER_MODES.has(value);
}

function isLiveCameraRenderMode(value: string): boolean {
  return value === "streams" || value === "camera-live";
}

function normalizePathname(pathname: string): string {
  const raw = String(pathname || "/").trim() || "/";
  if (raw.length > 1 && raw.endsWith("/")) return raw.slice(0, -1);
  return raw;
}

function liveViewIdFromCameraLivePath(pathname: string): string | null {
  const raw = normalizePathname(pathname);
  if (raw === CAMERA_LIVE_BASE_PATH) return "";
  if (!raw.startsWith(`${CAMERA_LIVE_BASE_PATH}/`)) return null;
  const segment = raw.slice(CAMERA_LIVE_BASE_PATH.length + 1).split("/")[0] ?? "";
  if (!segment) return "";
  try {
    return decodeURIComponent(segment).trim();
  } catch {
    return segment.trim();
  }
}

function buildCameraLivePath(liveViewId: string): string {
  const normalized = String(liveViewId || "").trim();
  return normalized ? `${CAMERA_LIVE_BASE_PATH}/${encodeURIComponent(normalized)}` : CAMERA_LIVE_BASE_PATH;
}

function readLastCameraLiveViewId(): string {
  if (typeof window === "undefined") return "";
  try {
    return String(localStorage.getItem(LAST_CAMERA_LIVE_VIEW_STORAGE_KEY) || "").trim();
  } catch {
    return "";
  }
}

function writeLastCameraLiveViewId(liveViewId: string): void {
  if (typeof window === "undefined") return;
  const normalized = String(liveViewId || "").trim();
  if (!normalized) return;
  try {
    localStorage.setItem(LAST_CAMERA_LIVE_VIEW_STORAGE_KEY, normalized);
  } catch {
    // ignore
  }
}

function enabledLiveViewItems(liveViews: StreamingCameraLiveView[]): StreamingCameraLiveView[] {
  return liveViews.filter((item) => item && item.enabled !== false && String(item.id || "").trim());
}

function liveViewDisplayName(liveView: StreamingCameraLiveView | null | undefined, fallback = ""): string {
  if (!liveView) return fallback;
  return String(liveView.name || liveView.camera_id || liveView.id || fallback).trim() || fallback;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || "");
}

export function MainScreen({
  compositionName,
  georeference,
  compositions,
  activeCompositionId,
  compositionLoaded,
  criticalExtensionsLoaded,
  allExtensionsLoaded,
  elements,
  elementTypesById,
  viewSettings,
  notificationRenderers,
  notificationDetailPanels = [],
  notificationHistoryFilters = [],
  historyAssistants = [],
  historyRequest,
  onHistoryRequestHandled,
  onOpenHistoryNotification,
  notificationsError = false,
  notifications,
  notificationsCount,
  notificationsHasMore,
  notificationsFilter,
  notificationsSearchStatus,
  notificationsHistoryChanged,
  activeNotificationId,
  activeNotification: activeNotificationRaw,
  notificationsLoading,
  renderViews,
  onNotificationsFilterChange,
  onNotificationsSearchRefresh,
  onSelectNotification,
  onNotificationDetailsOpenChange,
  onLoadMoreNotifications,
  onNotificationsViewed,
  api,
  updateElement,
  geographicView,
  onEditComposition,
  onOpenPipelines,
  onOpenSettings,
  onActivateComposition,
  onCreateComposition,
  onRenameComposition,
  onDeleteComposition,
  onViewportReady,
}: Props): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const timeRange = notificationsFilter.timeRange ? {
    timezone: notificationsFilter.timeRange.timezone,
    created_after: notificationsFilter.timeRange.createdAfter, created_before: notificationsFilter.timeRange.createdBefore,
  } : notificationsSearchStatus?.time_range;
  const timezoneRequired = notificationsSearchStatus?.reason === "installation_timezone_required";
  const visualSearchMessage = [
    t("core.ui.notifications.filter.visual_summary", { limit: notificationsSearchStatus?.scope_limit ?? 250 }),
    (notificationsSearchStatus?.pending ?? 0) > 0 ? t("core.ui.notifications.filter.visual_pending", { count: notificationsSearchStatus?.pending }) : "",
    (notificationsSearchStatus?.unavailable_images ?? 0) > 0 ? t("core.ui.notifications.filter.visual_unavailable", { count: notificationsSearchStatus?.unavailable_images }) : "",
    (notificationsSearchStatus?.without_images ?? 0) > 0 ? t("core.ui.notifications.filter.visual_without_images", { count: notificationsSearchStatus?.without_images }) : "",
  ].filter(Boolean).join(" ");
  const timeFormatter = timeRange ? new Intl.DateTimeFormat(locale, {
    timeZone: timeRange.timezone, dateStyle: "short", timeStyle: "short",
  }) : null;
  const timeSearchMessage = timezoneRequired ? t("core.ui.notifications.filter.timezone_required")
    : timeRange && timeFormatter ? t("core.ui.notifications.filter.time_range", {
      start: timeFormatter.format(new Date(timeRange.created_after * 1000)),
      end: timeFormatter.format(new Date(timeRange.created_before * 1000)), timezone: timeRange.timezone,
    }) : null;
  const pathname = usePathname();
  const normalizedPathname = normalizePathname(pathname);
  const routeCameraLiveViewId = liveViewIdFromCameraLivePath(normalizedPathname);
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [isRenderViewSelectorOpen, setIsRenderViewSelectorOpen] = useState(false);
  const [isCameraLiveModalOpen, setIsCameraLiveModalOpen] = useState(false);
  const [activeElementId, setActiveElementId] = useState<string | null>(null);
  const [imageModal, setImageModal] = useState<{ url: string; title: string; subtitle?: string } | null>(null);
  const [historyAssistantId, setHistoryAssistantId] = useState<string | null>(null);
  const previousHistorySelection = useRef<string | null>(null);
  const [isNotificationDetailsOpen, setIsNotificationDetailsOpen] = useState(false);
  useLayoutEffect(() => {
    onNotificationDetailsOpenChange(isNotificationDetailsOpen);
    return () => onNotificationDetailsOpenChange(false);
  }, [isNotificationDetailsOpen, onNotificationDetailsOpenChange]);
  const [notificationImageIndex, setNotificationImageIndex] = useState(0);
  const [failedNotificationImageUrls, setFailedNotificationImageUrls] = useState<Set<string>>(() => new Set());
  const [fullscreenNotificationImageOpen, setFullscreenNotificationImageOpen] = useState(false);
  const [fullscreenNotificationImageIndex, setFullscreenNotificationImageIndex] = useState(0);
  const selectedNotificationRef = useRef<HTMLButtonElement | null>(null);
  const [notificationsExpanded, setNotificationsExpanded] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(() => loadNotificationsOpen());
  const notificationsToggleRef = useRef<HTMLButtonElement | null>(null);
  const detailsWereOpen = useRef(false);
  useEffect(() => {
    if (detailsWereOpen.current && !isNotificationDetailsOpen && !notificationsOpen) {
      notificationsToggleRef.current?.focus({ preventScroll: true });
    }
    detailsWereOpen.current = isNotificationDetailsOpen;
  }, [isNotificationDetailsOpen, notificationsOpen]);
  const filter = notificationsFilter;
  const setFilter = onNotificationsFilterChange;
  const [isFilterPopoverOpen, setIsFilterPopoverOpen] = useState(false);
  const [periodResetRevision, setPeriodResetRevision] = useState(0);
  const locations = useHistoryLocationOptions(isFilterPopoverOpen || Boolean(filter.dataFilter?.camera_id || filter.dataFilter?.area_id));
  const openNotificationSearch = () => {
    setFilter(previous => ({ ...previous, timeRange: resolveNotificationPeriod(previous.period) }));
    setIsFilterPopoverOpen(true);
  };
  const [historyReturnId, setHistoryReturnId] = useState<string | null>(null);
  const [historyReturnError, setHistoryReturnError] = useState(false);
  useEffect(() => {
    if (!historyRequest) return;
    setHistoryReturnId(historyRequest.returnToNotification ?? null);
    setHistoryReturnError(false);
    setFilter({ priorities: ALL_PRIORITIES, types: [], query: "", extension: historyRequest.selection });
    setNotificationsOpen(true); setIsNotificationDetailsOpen(false); setIsFilterPopoverOpen(false);
    onHistoryRequestHandled();
  }, [historyRequest]);
  const returnToHistoryOrigin = async () => {
    if (!historyReturnId) return;
    try { await onOpenHistoryNotification(historyReturnId); setIsNotificationDetailsOpen(true); setHistoryReturnId(null); }
    catch { setHistoryReturnError(true); }
  };

  const [notificationsNowMs, setNotificationsNowMs] = useState(() => Date.now());
  const filterButtonRef = useRef<HTMLButtonElement | null>(null);
  const [renderMode, setRenderMode] = useState<RenderMode>(() => {
    if (routeCameraLiveViewId !== null) return "camera-live";
    try {
      const saved = localStorage.getItem(RENDER_MODE_STORAGE_KEY);
      return saved && saved.trim() ? saved.trim() : "3d";
    } catch {
      return "3d";
    }
  });
  const streamingAvailability = useContext(StreamingAvailabilityContext);
  const [cameraLiveViews, setCameraLiveViews] = useState<StreamingCameraLiveView[]>([]);
  const [cameraLiveViewsLoading, setCameraLiveViewsLoading] = useState(true);
  const [cameraLiveViewsError, setCameraLiveViewsError] = useState<string | null>(null);
  const [selectedCameraLiveViewId, setSelectedCameraLiveViewId] = useState(() => {
    if (routeCameraLiveViewId !== null) return routeCameraLiveViewId;
    return readLastCameraLiveViewId();
  });
  const [streamsOverlayVisible, setStreamsOverlayVisible] = useState(true);
  useEffect(() => {
    if (renderMode === "geographic" && notificationsExpanded) selectedNotificationRef.current?.scrollIntoView({ block: "nearest" });
  }, [activeNotificationId, notificationsExpanded, renderMode]);
  const notificationScrollRef = useRef<HTMLDivElement | null>(null);
  const notificationSentinelRef = useRef<HTMLDivElement | null>(null);
  const streamsOverlayTimerRef = useRef<number | null>(null);
  const notificationPaginationUserEngagedRef = useRef(false);
  const previousRouteCameraLiveViewIdRef = useRef<string | null>(routeCameraLiveViewId);

  const clearStreamsOverlayTimer = useCallback(() => {
    const timerId = streamsOverlayTimerRef.current;
    if (timerId == null) return;
    window.clearTimeout(timerId);
    streamsOverlayTimerRef.current = null;
  }, []);

  const autoHideStreamsOverlay = isLiveCameraRenderMode(renderMode) && streamingAvailability !== "loading" && streamingAvailability !== "unavailable";

  const scheduleStreamsOverlayHide = useCallback(() => {
    clearStreamsOverlayTimer();
    if (!autoHideStreamsOverlay) {
      setStreamsOverlayVisible(true);
      return;
    }
    if (document.visibilityState !== "visible") {
      setStreamsOverlayVisible(false);
      return;
    }
    streamsOverlayTimerRef.current = window.setTimeout(() => {
      streamsOverlayTimerRef.current = null;
      if (document.activeElement?.closest(".streamsHud, .streamsTileOverlay, .streamsOverlayAutoHide")) return;
      setStreamsOverlayVisible(false);
    }, STREAMS_OVERLAY_IDLE_MS);
  }, [clearStreamsOverlayTimer, autoHideStreamsOverlay]);

  const hasOpenRealtimeNotifications = useMemo(
    () => notifications.some(notificationIsOpenRealtime) || notificationIsOpenRealtime(activeNotificationRaw),
    [activeNotificationRaw, notifications],
  );

  useEffect(() => {
    if (!hasOpenRealtimeNotifications) return;
    setNotificationsNowMs(Date.now());
    const handle = window.setInterval(() => {
      setNotificationsNowMs(Date.now());
    }, 1000);
    return () => {
      window.clearInterval(handle);
    };
  }, [hasOpenRealtimeNotifications]);

  const displayNotifications = useMemo(
    () => notifications.map((notification) => withLiveDuration(notification, notificationsNowMs)),
    [notifications, notificationsNowMs],
  );

  useEffect(() => {
    if (!autoHideStreamsOverlay) {
      clearStreamsOverlayTimer();
      setStreamsOverlayVisible(true);
      return;
    }

    const showOverlays = () => {
      setStreamsOverlayVisible(true);
      scheduleStreamsOverlayHide();
    };

    const onMouseMove = () => showOverlays();
    const onKeyDown = () => showOverlays();
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        showOverlays();
        return;
      }
      clearStreamsOverlayTimer();
      setStreamsOverlayVisible(false);
    };

    showOverlays();
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("pointerdown", showOverlays);
    window.addEventListener("focusin", showOverlays);
    window.addEventListener("keydown", onKeyDown);
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("pointerdown", showOverlays);
      window.removeEventListener("focusin", showOverlays);
      window.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearStreamsOverlayTimer();
    };
  }, [clearStreamsOverlayTimer, autoHideStreamsOverlay, scheduleStreamsOverlayHide]);

  const activeNotification = useMemo(
    () => (activeNotificationRaw ? withLiveDuration(activeNotificationRaw, notificationsNowMs) : null),
    [activeNotificationRaw, notificationsNowMs],
  );

  const selectedThumbnail = activeNotification ? notificationThumbnailUrl(activeNotification) : null;
  const activeNotificationImages = useMemo(() => {
    if (!activeNotification) return [];
    return notificationImageItems(activeNotification).filter((item) => !failedNotificationImageUrls.has(item.url.trim()));
  }, [activeNotification, failedNotificationImageUrls]);

  const activeNotificationImage = activeNotificationImages[notificationImageIndex] ?? null;

  const activeNotificationDetails = useMemo(() => {
    if (!activeNotification) return [] as NotificationDetailField[];
    const payload = asRecord(activeNotification.payload);
    const data = asRecord(payload.data);
    const event = asRecord(payload.event);

    const out: NotificationDetailField[] = [];
    const push = (label: string, valueRaw: unknown, technical = false) => {
      const value = asTrimmedString(valueRaw);
      if (!value) return;
      out.push({ label, value, technical });
    };

    const cameraName = asTrimmedString(data.camera_name);
    const cameraId = asTrimmedString(data.camera_id);
    const subject = asRecord(payload.subject) || asRecord(data.subject);
    const subjectId = asTrimmedString(payload.subject_id) || asTrimmedString(subject?.id);
    const subjectType = asTrimmedString(payload.subject_type) || asTrimmedString(subject?.type);
    const trackingId = asTrimmedString(payload.tracking_id) || asTrimmedString(data.tracking_id);
    const eventId = asTrimmedString(payload.event_id) || asTrimmedString(data.event_id);
    const eventCode = asTrimmedString(payload.event_code) || asTrimmedString(data.event_code);
    const duration = payload.source === "pipelines" && event.duration_seconds === 0
      ? t("core.ui.notifications.details.single_observation")
      : formatDurationVerbose(event.duration_seconds, locale);
    const createdAt = formatDateTimeLong(locale, activeNotification.createdAt);
    const updatedAt = formatDateTimeLong(locale, activeNotification.updatedAt);

    push(t("core.ui.notifications.details.meta.type", {}, "Type"), activeNotification.type, true);
    push(t("core.ui.notifications.details.meta.priority", {}, "Priority"), t(`core.ui.notifications.filter.priority.${notificationPriority(activeNotification)}`, {}, notificationPriority(activeNotification)));
    push(t("core.ui.notifications.details.meta.status", {}, "Status"), asTrimmedString(payload.status) ? t(`core.ui.notifications.details.status.${payload.status}`, {}, asTrimmedString(payload.status)) : "");
    push(t("core.ui.notifications.details.meta.lifecycle", {}, "Lifecycle"), payload.lifecycle, true);
    push(t("core.ui.notifications.details.meta.pipeline", {}, "Pipeline"), payload.pipeline_name, true);
    push(t("core.ui.notifications.details.meta.camera", {}, "Camera"), cameraName);
    if (!cameraName) push(t("core.ui.notifications.details.meta.camera", {}, "Camera"), cameraId, true);
    push(t("core.ui.notifications.details.meta.area", {}, "Area"), data.area_label);
    push(t("core.ui.notifications.details.meta.subject_id", {}, "Subject ID"), subjectId, true);
    push(t("core.ui.notifications.details.meta.subject_type", {}, "Subject type"), subjectType, true);
    push(t("core.ui.notifications.details.meta.event_id", {}, "Event ID"), eventId, true);
    push(t("core.ui.notifications.details.meta.event_code", {}, "Event code"), eventCode, true);
    push(t("core.ui.notifications.details.meta.tracking_id", {}, "Tracking ID"), trackingId, true);
    push(payload.source === "pipelines"
      ? t("core.ui.notifications.details.meta.observed_duration", {}, "Duration through last update")
      : t("core.ui.notifications.details.meta.duration", {}, "Duration"), duration);
    if (event.duration_status === "clock_changed") {
      push(t("core.ui.notifications.details.meta.duration", {}, "Duration"),
        t("core.ui.notifications.details.meta.clock_changed", {}, "Time reference changed; duration unavailable"));
    }
    if (event.duration_status === "invalid_interval") {
      push(t("core.ui.notifications.details.meta.duration", {}, "Duration"),
        t("core.ui.notifications.details.meta.invalid_interval", {}, "Invalid time interval; duration unavailable"));
    }
    push(t("core.ui.notifications.details.meta.created_at", {}, "Created"), createdAt);
    push(t("core.ui.notifications.details.meta.updated_at", {}, "Updated"), updatedAt);

    out.push({
      label: t("core.ui.notifications.details.meta.images", {}, "Images"),
      value: String(activeNotificationImages.length),
      technical: activeNotificationImages.length === 0,
    });
    return out;
  }, [activeNotification, activeNotificationImages.length, locale, t]);

  const activeNotificationSubtitle = useMemo(() => {
    if (!activeNotification) return null;
    const payload = asRecord(activeNotification.payload);
    const data = asRecord(payload.data);
    const camera = asTrimmedString(data.camera_name);
    const area = asTrimmedString(data.area_label);
    const parts = [camera, area].filter(Boolean);
    if (!parts.length) return null;
    return parts.join(" • ");
  }, [activeNotification]);

  const formatNotificationImageMeta = useCallback((item: NotificationImageItem): string | null => {
    const parts: string[] = [
      t(`core.ui.notifications.details.image_source.${item.source}`, {}, item.source),
    ];
    if (typeof item.confidence === "number") {
      parts.push(`${Math.round(item.confidence * 100)}%`);
    }
    const tsLabel = formatTimestampMillis(locale, item.publishedAtMs);
    if (tsLabel) parts.push(t("core.ui.notifications.details.image_published", { time: tsLabel }, `Published: ${tsLabel}`));
    return parts.join(" • ");
  }, [locale, t]);

  const activeNotificationImageMeta = useMemo(() => {
    if (!activeNotificationImage) return null;
    return formatNotificationImageMeta(activeNotificationImage);
  }, [activeNotificationImage, formatNotificationImageMeta]);

  const fullscreenNotificationImageItems = useMemo<FullscreenImageViewerItem[]>(
    () =>
      activeNotificationImages.map((item) => ({
        id: item.id,
        url: item.url,
        label: item.label,
        meta: formatNotificationImageMeta(item) ?? undefined,
      })),
    [activeNotificationImages, formatNotificationImageMeta],
  );

  const openFullscreenNotificationImage = useCallback((index: number) => {
    requestFullscreenImageViewer();
    setFullscreenNotificationImageIndex(index);
    setFullscreenNotificationImageOpen(true);
  }, []);

  const markNotificationImageFailed = useCallback((url: string) => {
    const key = url.trim();
    if (!key) return;
    setFailedNotificationImageUrls((prev) => {
      if (prev.has(key)) return prev;
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  }, []);

  const showPrevNotificationImage = useCallback(() => {
    setNotificationImageIndex((prev) => {
      const total = activeNotificationImages.length;
      if (total <= 1) return 0;
      return (prev - 1 + total) % total;
    });
  }, [activeNotificationImages.length]);

  const showNextNotificationImage = useCallback(() => {
    setNotificationImageIndex((prev) => {
      const total = activeNotificationImages.length;
      if (total <= 1) return 0;
      return (prev + 1) % total;
    });
  }, [activeNotificationImages.length]);

  const activeNotificationRenderer = useMemo(() => {
    if (!activeNotification) return null;
    return notificationRenderers.find((r) => r.type === activeNotification.type) ?? null;
  }, [activeNotification, notificationRenderers]);

  const activeElement = useMemo(
    () => (activeElementId ? elements.find((e) => e.id === activeElementId) ?? null : null),
    [activeElementId, elements],
  );
  const activeType = activeElement ? elementTypesById[activeElement.type] ?? null : null;

  const actionTitle = activeElement ? activeElement.name : t("core.ui.action");
  const isActionModalOpen = Boolean(activeElement);
  const enabledCameraLiveViews = useMemo(() => enabledLiveViewItems(cameraLiveViews), [cameraLiveViews]);
  const selectedCameraLiveView = useMemo(
    () =>
      selectedCameraLiveViewId
        ? enabledCameraLiveViews.find((item) => String(item.id || "").trim() === selectedCameraLiveViewId) ?? null
        : null,
    [enabledCameraLiveViews, selectedCameraLiveViewId],
  );
  const cameraLiveButtonLabel =
    liveViewDisplayName(selectedCameraLiveView, selectedCameraLiveViewId) ||
    (cameraLiveViewsLoading
      ? t("core.ui.loading", {}, "Loading...")
      : t("core.ui.camera_live.select", {}, "Select camera"));
  const selectCameraLiveView = useCallback((liveViewId: string, options: { remember?: boolean } = {}) => {
    const normalizedLiveViewId = String(liveViewId || "").trim();
    setSelectedCameraLiveViewId(normalizedLiveViewId);
    setRenderMode("camera-live");
    if (normalizedLiveViewId) {
      if (options.remember !== false) writeLastCameraLiveViewId(normalizedLiveViewId);
      navigate(buildCameraLivePath(normalizedLiveViewId));
    } else {
      navigate(CAMERA_LIVE_BASE_PATH);
    }
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(RENDER_MODE_STORAGE_KEY, renderMode);
    } catch {
      // ignore
    }
  }, [renderMode]);

  useEffect(() => {
    if (streamingAvailability === "loading" || streamingAvailability === "unavailable") {
      setCameraLiveViews([]);
      setCameraLiveViewsError(null);
      setCameraLiveViewsLoading(streamingAvailability === "loading");
      return;
    }
    let cancelled = false;
    let requestController: AbortController | null = null;

    const loadCameraLiveViews = async (showLoading: boolean) => {
      if (document.hidden) return;
      if (showLoading) setCameraLiveViewsLoading(true);
      requestController?.abort();
      const controller = new AbortController();
      requestController = controller;
      try {
        const payload = await listStreamingCameraLiveViews({ signal: controller.signal });
        if (cancelled || controller.signal.aborted) return;
        setCameraLiveViews(Array.isArray(payload) ? payload : []);
        setCameraLiveViewsError(null);
      } catch (error) {
        if (cancelled || controller.signal.aborted || isAbortError(error)) return;
        setCameraLiveViewsError(errorMessage(error));
      } finally {
        if (!cancelled && !controller.signal.aborted) setCameraLiveViewsLoading(false);
        if (requestController === controller) requestController = null;
      }
    };

    void loadCameraLiveViews(true);
    const intervalId = window.setInterval(() => {
      void loadCameraLiveViews(false);
    }, LIVE_VIEWS_REFRESH_MS);
    const onVisibilityChange = () => {
      if (document.hidden) requestController?.abort();
      else void loadCameraLiveViews(false);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      cancelled = true;
      requestController?.abort();
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [streamingAvailability]);

  useEffect(() => {
    const previousRouteCameraLiveViewId = previousRouteCameraLiveViewIdRef.current;
    previousRouteCameraLiveViewIdRef.current = routeCameraLiveViewId;
    if (routeCameraLiveViewId === null) return;
    if (renderMode !== "camera-live") {
      if (previousRouteCameraLiveViewId !== routeCameraLiveViewId) {
        setRenderMode("camera-live");
        setSelectedCameraLiveViewId(routeCameraLiveViewId);
      }
      return;
    }
    if (selectedCameraLiveViewId !== routeCameraLiveViewId) {
      setSelectedCameraLiveViewId(routeCameraLiveViewId);
    }
  }, [renderMode, routeCameraLiveViewId, selectedCameraLiveViewId]);

  useEffect(() => {
    if (renderMode === "camera-live" || routeCameraLiveViewId === null) return;
    replace("/");
  }, [renderMode, routeCameraLiveViewId]);

  useEffect(() => {
    if (renderMode !== "camera-live" || routeCameraLiveViewId !== null || cameraLiveViewsLoading) return;
    const rememberedLiveViewId = readLastCameraLiveViewId();
    const rememberedEnabled = rememberedLiveViewId
      ? enabledCameraLiveViews.find((item) => String(item.id || "").trim() === rememberedLiveViewId)
      : null;
    const selectedEnabled = selectedCameraLiveViewId
      ? enabledCameraLiveViews.find((item) => String(item.id || "").trim() === selectedCameraLiveViewId)
      : null;
    const nextLiveViewId =
      String(rememberedEnabled?.id || selectedEnabled?.id || enabledCameraLiveViews[0]?.id || rememberedLiveViewId || selectedCameraLiveViewId || "").trim();
    if (!nextLiveViewId) return;
    if (selectedCameraLiveViewId !== nextLiveViewId) setSelectedCameraLiveViewId(nextLiveViewId);
    replace(buildCameraLivePath(nextLiveViewId));
  }, [cameraLiveViewsLoading, enabledCameraLiveViews, renderMode, routeCameraLiveViewId, selectedCameraLiveViewId]);

  useEffect(() => {
    try {
      localStorage.setItem(NOTIFICATIONS_OPEN_STORAGE_KEY, notificationsOpen ? "1" : "0");
    } catch {
      // ignore
    }
  }, [notificationsOpen]);

  useEffect(() => {
    if (!notificationsOpen) return;
    if (notificationsCount.unread_total <= 0) return;
    onNotificationsViewed();
  }, [notificationsOpen, notificationsCount.unread_total, onNotificationsViewed]);

  useEffect(() => {
    notificationPaginationUserEngagedRef.current = false;
  }, [filter]);

  useEffect(() => {
    if (activeNotificationId) return;
    setIsNotificationDetailsOpen(false);
    setNotificationImageIndex(0);
    setFullscreenNotificationImageOpen(false);
    setFullscreenNotificationImageIndex(0);
  }, [activeNotificationId]);

  useEffect(() => {
    setNotificationImageIndex(0);
    setFullscreenNotificationImageOpen(false);
    setFullscreenNotificationImageIndex(0);
  }, [activeNotificationId]);

  useEffect(() => {
    if (notificationImageIndex < activeNotificationImages.length) return;
    setNotificationImageIndex(0);
  }, [activeNotificationImages.length, notificationImageIndex]);

  useEffect(() => {
    if (!isNotificationDetailsOpen) return;
    function onKeyDown(event: KeyboardEvent): void {
      const target = event.target;
      if (event.defaultPrevented || (target instanceof HTMLElement && (
        target.isContentEditable || target.closest('input, textarea, select, [role="textbox"], [role="combobox"], [role="listbox"], [role="slider"], [role="spinbutton"]')
      ))) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        showPrevNotificationImage();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        showNextNotificationImage();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isNotificationDetailsOpen, showNextNotificationImage, showPrevNotificationImage]);

  useEffect(() => {
    const root = notificationScrollRef.current;
    const sentinel = notificationSentinelRef.current;
    if (!root || !sentinel) return;

    const obs = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry?.isIntersecting) return;
        if (!notificationPaginationUserEngagedRef.current) return;
        onLoadMoreNotifications();
      },
      { root, rootMargin: "220px" },
    );

    obs.observe(sentinel);
    return () => obs.disconnect();
  }, [onLoadMoreNotifications]);

  const handleElementActivated = (elementId: string, intent?: "click" | "dblclick" | "longpress") => {
    const el = elements.find((e) => e.id === elementId) ?? null;
    const def = el ? elementTypesById[el.type] ?? null : null;

    if (!el || !def) return;

    if (intent === "dblclick" || intent === "longpress") {
      setActiveElementId(elementId);
      return;
    }

    if (intent === "click" && def.primaryAction) {
      Promise.resolve(def.primaryAction({ element: el, api, update: (patch) => updateElement(el.id, patch) }))
        .then((handled) => {
          if (!handled) setActiveElementId(elementId);
        })
        .catch((err) => {
          console.error(`[primaryAction:${el.type}]`, err);
          setActiveElementId(elementId);
        });
      return;
    }

    setActiveElementId(elementId);
  };

  const availableTypes = useMemo(() => {
    const seen = new Set<string>();
    for (const n of displayNotifications) {
      if (typeof n.type === "string" && n.type) seen.add(n.type);
    }
    return Array.from(seen).sort();
  }, [displayNotifications]);

  const visibleNotifications = displayNotifications;

  const filterRestrictive = useMemo(() => isFilterRestrictive(filter), [filter]);

  const badgeLabel = useMemo(() => {
    const unreadCount = filter.priorities.reduce(
      (acc, prio) => acc + (notificationsCount.unread_by_priority[prio] ?? 0),
      0,
    );
    if (unreadCount <= 0) return null;
    return unreadCount > 999 ? "999+" : String(unreadCount);
  }, [filter, notificationsCount]);

  const togglePriorityFilter = useCallback((prio: Priority) => {
    setFilter((prev) => {
      const has = prev.priorities.includes(prio);
      const next = has ? prev.priorities.filter((p) => p !== prio) : [...prev.priorities, prio];
      // Don't allow zero priorities — falls back to default.
      return { ...prev, priorities: next.length > 0 ? next : DEFAULT_NOTIFICATIONS_FILTER.priorities };
    });
  }, []);

  const toggleTypeFilter = useCallback((type: string) => {
    setFilter((prev) => {
      const has = prev.types.includes(type);
      const next = has ? prev.types.filter((t) => t !== type) : [...prev.types, type];
      return { ...prev, types: next };
    });
  }, []);

  const clearFilter = useCallback(() => {
    setFilter({ ...DEFAULT_NOTIFICATIONS_FILTER });
    setPeriodResetRevision(previous => previous + 1);
  }, []);
  const noteNotificationPaginationUserEngagement = useCallback(() => {
    notificationPaginationUserEngagedRef.current = true;
  }, []);

  const orderedRenderViews = useMemo(
    () =>
      [...renderViews]
        .filter((view) => view.id && !isBuiltinRenderMode(view.id))
        .sort((a, b) => (a.order ?? 1000) - (b.order ?? 1000) || a.id.localeCompare(b.id)),
    [renderViews],
  );
  const activeRenderView = orderedRenderViews.find((view) => view.id === renderMode) ?? null;

  useEffect(() => {
    setIsRenderViewSelectorOpen(false);
  }, [renderMode]);

  useEffect(() => {
    if (isBuiltinRenderMode(renderMode) || activeRenderView || !allExtensionsLoaded) return;
    setRenderMode("3d");
  }, [activeRenderView, allExtensionsLoaded, renderMode]);

  const renderViewText = useCallback(
    (value: RenderViewDefinition["name"] | RenderViewDefinition["description"] | undefined, fallback: string) => {
      if (!value) return fallback;
      if (typeof value === "string") return value;
      return t(value.key, value.params, value.fallback ?? fallback);
    },
    [t],
  );

  const renderModeLabel =
    renderMode === "geographic" ? t("core.map.map") : renderMode === "3d"
      ? t("core.ui.render_modal.option_3d.title", {}, "3D")
      : renderMode === "2d"
        ? t("core.ui.render_modal.option_2d.title", {}, "2D (Image)")
        : renderMode === "vector2d"
          ? t("core.ui.render_modal.option_vector2d.title", {}, "2D (Plan)")
          : renderMode === "streams"
            ? t("core.ui.render_modal.option_streams.title", {}, "Camera Grid")
            : renderMode === "camera-live"
              ? t("core.ui.render_modal.option_camera_live.title", {}, "Live Camera")
              : activeRenderView
                ? renderViewText(activeRenderView.name, activeRenderView.id)
                : t("core.ui.render_modal.option_loading.title", {}, "Loading view");
  const renderButtonLabel = renderModeLabel || t("core.ui.rendering");
  const compositionButtonLabel = compositionName.trim() || t("core.ui.composition");
  const viewportLoadingMessage =
    isLiveCameraRenderMode(renderMode)
      ? null
      : !compositionLoaded
        ? t("core.ui.viewport_loading.composition", {}, "Loading composition...")
        : !criticalExtensionsLoaded
          ? t("core.ui.viewport_loading.extensions", {}, "Loading extensions...")
          : !isBuiltinRenderMode(renderMode) && !activeRenderView && !allExtensionsLoaded
            ? t("core.ui.viewport_loading.extensions", {}, "Loading extensions...")
          : null;

  useEffect(() => {
    if (viewportLoadingMessage) return;
    onViewportReady();
  }, [onViewportReady, viewportLoadingMessage]);

  return (
    <div className={`screenRoot${renderMode === "geographic" ? " geographicScreen" : ""}${notificationsExpanded ? " notificationsExpanded" : ""}`}>
      {viewportLoadingMessage ? (
        <div className="viewportRoot mainViewportLoadingRoot">
          <div className="mainViewportLoadingCard">
            <div className="mainViewportLoadingSpinner" aria-hidden="true" />
            <div className="mainViewportLoadingText">{viewportLoadingMessage}</div>
          </div>
        </div>
      ) : renderMode === "3d" ? (
        <div className={`registeredRenderView notificationSpatialViewport${notificationsOpen ? " withNotifications" : ""}`}>
        <Viewport3D
          elements={elements}
          elementTypesById={elementTypesById}
          onElementActivated={handleElementActivated}
          viewSettings={viewSettings}
          compositionId={activeCompositionId}
          activeNotification={activeNotification}
          activeNotificationRenderer={activeNotificationRenderer}
          onOpenImage={(args) => {
            setImageModal({
              url: args.url,
              title: args.title ?? t("core.ui.image_preview"),
              subtitle: args.subtitle,
            });
          }}
        />
        </div>
      ) : (
        <>
          {renderMode === "geographic" ? (
            <div className={`registeredRenderView notificationSpatialViewport${notificationsOpen ? " withNotifications" : ""}`}>
            <MainGeographicViewport viewState={geographicView} compositionId={activeCompositionId} compositionName={compositionName} georeference={georeference}
              elements={elements} elementTypesById={elementTypesById} notifications={activeNotification && !notifications.some(item => item.id === activeNotification.id) ? [...notifications, activeNotification] : notifications}
              notificationRenderers={notificationRenderers} activeNotificationId={activeNotificationId}
              onActivateComposition={onActivateComposition} onElementActivated={handleElementActivated}
              onNotification={id => { onSelectNotification(id); setNotificationsOpen(true); setNotificationsExpanded(false); }}
              onImage={args => setImageModal({ ...args, title: args.title ?? t("core.ui.image_preview") })} />
            </div>
          ) : renderMode === "2d" ? (
            <div className={`registeredRenderView notificationSpatialViewport${notificationsOpen ? " withNotifications" : ""}`}>
            <MainViewport2D
              elements={elements}
              elementTypesById={elementTypesById}
              compositionId={activeCompositionId}
              onElementActivated={handleElementActivated}
              activeNotification={activeNotification}
              activeNotificationRenderer={activeNotificationRenderer}
            />
            </div>
          ) : renderMode === "vector2d" ? (
            <div className={`registeredRenderView notificationSpatialViewport${notificationsOpen ? " withNotifications" : ""}`}>
            <MainViewportVector2D
              elements={elements}
              elementTypesById={elementTypesById}
              compositionId={activeCompositionId}
              onElementActivated={handleElementActivated}
              activeNotification={activeNotification}
              activeNotificationRenderer={activeNotificationRenderer}
            />
            </div>
          ) : activeRenderView ? (
            <div className={`registeredRenderView${notificationsOpen ? " withNotifications" : ""}`}>
              {activeRenderView.render({
                onConfigureElement: (id, action) => onEditComposition(action, id),
                compositionId: activeCompositionId,
                compositionName,
                elements,
                elementTypesById,
                viewSettings,
                activeNotification,
                activeNotificationRenderer,
                onElementActivated: handleElementActivated,
                onOpenImage: (args) => {
                  setImageModal({
                    url: args.url,
                    title: args.title ?? t("core.ui.image_preview"),
                    subtitle: args.subtitle,
                  });
                },
              })}
            </div>
          ) : renderMode === "streams" ? (
            <StreamsDashboard uiVisible={streamsOverlayVisible} isActive={renderMode === "streams"} />
          ) : renderMode === "camera-live" ? (
            <StreamsDashboard
              uiVisible={streamsOverlayVisible}
              isActive={renderMode === "camera-live"}
              embedded={true}
              liveViewId={selectedCameraLiveViewId || "__toposync-no-camera-selected__"}
              defaultContext="large"
            />
          ) : (
            <div className="viewportRoot mainViewportLoadingRoot">
              <div className="mainViewportLoadingCard">
                <div className="mainViewportLoadingText">
                  {t("core.ui.render_modal.option_unavailable", {}, "This render view is not available.")}
                </div>
              </div>
            </div>
          )}
        </>
      )}

      <div
        className={[
          "overlayTopRight",
          isLiveCameraRenderMode(renderMode) ? "streamsOverlayAutoHide" : "",
          isLiveCameraRenderMode(renderMode) && !streamsOverlayVisible ? "isHidden" : "isVisible",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <button
          className="chipButton mainSelectorButton"
          type="button"
          aria-label={`${t("core.ui.rendering")}: ${renderButtonLabel}`}
          title={`${t("core.ui.rendering")}: ${renderButtonLabel}`}
          onClick={() => setIsRenderModalOpen(true)}
        >
          <span className="mainSelectorButtonText">{renderButtonLabel}</span>
        </button>
        {renderMode === "camera-live" ? (
          <button
            className="chipButton mainSelectorButton"
            type="button"
            aria-label={`${t("core.ui.camera_live.selector_label", {}, "Camera")}: ${cameraLiveButtonLabel}`}
            title={`${t("core.ui.camera_live.selector_label", {}, "Camera")}: ${cameraLiveButtonLabel}`}
            onClick={() => setIsCameraLiveModalOpen(true)}
          >
            <span className="mainSelectorButtonText">{cameraLiveButtonLabel}</span>
          </button>
        ) : activeRenderView?.toolbarSelector ? (
          activeRenderView.toolbarSelector.renderTrigger({ open: () => setIsRenderViewSelectorOpen(true) })
        ) : renderMode !== "streams" ? (
          <button
            className="chipButton mainSelectorButton"
            type="button"
            aria-label={`${t("core.ui.composition")}: ${compositionButtonLabel}`}
            title={`${t("core.ui.composition")}: ${compositionButtonLabel}`}
            onClick={() => setIsCompositionModalOpen(true)}
          >
            <span className="mainSelectorButtonText">{compositionButtonLabel}</span>
          </button>
        ) : null}
        <button
          className="iconButton"
          type="button"
          aria-label={t("core.ui.settings.aria")}
          onClick={onOpenSettings}
        >
          <Icon name="gear" />
        </button>
        <button
          className="iconButton"
          type="button"
          aria-label={t("core.ui.pipelines.title")}
          title={t("core.ui.pipelines.title")}
          onClick={onOpenPipelines}
        >
          <Icon name="diagram-project" />
        </button>
        {!isLiveCameraRenderMode(renderMode) ? <PipelineInspectionPanel /> : null}
        {!isLiveCameraRenderMode(renderMode) ? (
          <button
            className="iconButton iconButtonPrimary"
            type="button"
            aria-label={t("core.actions.edit")}
            title={t("core.actions.edit")}
            onClick={() => onEditComposition()}
          >
            <Icon name="pen-to-square" />
          </button>
        ) : null}
      </div>

      <div
        className={[
          "overlayLeft",
          notificationsOpen ? "isOpen" : "isCollapsed",
          isLiveCameraRenderMode(renderMode) ? "streamsOverlayAutoHide" : "",
          isLiveCameraRenderMode(renderMode) && !streamsOverlayVisible ? "isHidden" : "isVisible",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {notificationsOpen ? (
          <div className="rail railNotifications">
            <div className="railHeader">
              {renderMode === "geographic" && <button type="button" className="iconButton geographicListToggle" aria-expanded={notificationsExpanded}
                aria-label={t(notificationsExpanded ? "core.map.events_compact" : "core.map.events_expand")} onClick={() => setNotificationsExpanded(value => !value)}><Icon name={notificationsExpanded ? "chevron-down" : "chevron-up"} /></button>}
              <div className="railTitle">{t("core.ui.notifications")}</div>
              <div className="row">
                <button
                  className={["iconButton", isNotificationDetailsOpen ? "iconButtonPrimary" : ""].filter(Boolean).join(" ")}
                  type="button"
                  aria-label={t("core.ui.notifications.details.aria_open", {}, "Open selected notification details")}
                  title={t("core.ui.notifications.details.open", {}, "Open details")}
                  onClick={() => {
                    if (!activeNotification) return;
                    setNotificationImageIndex(0);
                    setIsNotificationDetailsOpen(true);
                  }}
                  disabled={!activeNotification}
                >
                  <Icon name="circle-info" />
                </button>



                <button
                  ref={filterButtonRef}
                  className={["iconButton", filterRestrictive ? "iconButtonPrimary" : ""].filter(Boolean).join(" ")}
                  type="button"
                  aria-label={t("core.ui.notifications.search_entry")}
                  title={t("core.ui.notifications.search_entry")}
                  aria-expanded={isFilterPopoverOpen}
                  onClick={openNotificationSearch}
                >
                  <Icon name="magnifying-glass" />
                </button>
                <button
                  className="iconButton notificationsCollapseButton"
                  type="button"
                  aria-label={t("core.ui.notifications.aria_close", {}, "Close notifications")}
                  title={t("core.ui.notifications.aria_close", {}, "Close notifications")}
                  onClick={() => setNotificationsOpen(false)}
                >
                  <Icon name="chevron-left" />
                </button>
              </div>
            </div>

                {historyReturnId || isFilterPopoverOpen ? <div className="notificationsSearchEntry">
                  {historyReturnId && <button type="button" className="chipButton" disabled={notificationsLoading} onClick={() => void returnToHistoryOrigin()}>{t("core.ui.notifications.return_to_original")}</button>}

                  {isFilterPopoverOpen ? (
                    <Modal open title={t("core.ui.notifications.search_entry")} onClose={() => setIsFilterPopoverOpen(false)} panelStyle={{ width: "min(560px, calc(100vw - 28px))" }}><div className="notificationsSearchContent">
                      <div className="notificationsFilterSection">
                        <label className="notificationsFilterSectionTitle" htmlFor="notificationsFilterSearch">
                          {t("core.ui.notifications.filter.search", {}, "Search")}
                        </label>
                        <input
                          id="notificationsFilterSearch"
                          type="search"
                          className="notificationsFilterSearch"
                          placeholder={t("core.ui.notifications.filter.search_placeholder", {}, "Words, camera or area")}
                          aria-describedby="notificationsSearchMode"
                          value={filter.query}
                          onChange={(e) => setFilter((prev) => ({ ...prev, query: e.target.value }))}
                        />
                      </div>

                      <label className="notificationsFilterSection">
                        <span className="notificationsFilterSectionTitle">{t("core.ui.notifications.filter.method")}</span>
                        <select className="notificationsFilterSearch" value={filter.semantic === "visual" ? "visual" : filter.semantic ? "text" : "literal"}
                          onChange={(event) => setFilter((previous) => ({ ...previous, semantic: event.target.value === "visual" ? "visual" : event.target.value === "text" }))}>
                          <option value="literal">{t("core.ui.notifications.filter.literal")}</option>
                          <option value="text">{t("core.ui.notifications.filter.semantic")}</option>
                          <option value="visual">{t("core.ui.notifications.filter.visual")}</option>
                        </select>
                      </label>
                      {filter.semantic === "visual" ? <VisualHistoryPreparation onReady={onNotificationsSearchRefresh} /> : null}
                      {(filter.extension || ((filter.semantic || timeRange) && filter.query.trim())) ? (
                        <button type="button" className="chipButton" disabled={notificationsLoading} onClick={onNotificationsSearchRefresh}>
                          {t("core.ui.notifications.filter.refresh_search")}
                        </button>
                      ) : null}
                      <p id="notificationsSearchMode" className="muted" role="status">
                        {filter.timeRange ? t("core.ui.notifications.period.precedence") : timezoneRequired ? t("core.ui.notifications.filter.timezone_required") : notificationsSearchStatus?.mode === "semantic_visual"
                          ? visualSearchMessage
                          : notificationsSearchStatus?.mode === "semantic_text"
                          ? t("core.ui.notifications.filter.semantic_scope", { limit: notificationsSearchStatus.scope_limit ?? 250, pending: notificationsSearchStatus.pending ?? 0 })
                          : notificationsSearchStatus?.status === "degraded"
                            ? t("core.ui.notifications.filter.semantic_fallback")
                            : t("core.ui.notifications.filter.search_mode")}
                      </p>


                      <div className="notificationsFilterSection">
                        <div className="notificationsFilterSectionTitle">
                          {t("core.ui.notifications.filter.priority", {}, "Priority")}
                        </div>
                        <div className="notificationsFilterChips">
                          {ALL_PRIORITIES.map((prio) => {
                            const enabled = filter.priorities.includes(prio);
                            const count = notificationsCount.by_priority[prio] ?? 0;
                            const label = t(`core.ui.notifications.filter.priority.${prio}`, {}, prio);
                            return (
                              <button
                                key={prio}
                                type="button"
                                className={["notificationsFilterChip", `prio_${prio}`, enabled ? "isEnabled" : ""].filter(Boolean).join(" ")}
                                onClick={() => togglePriorityFilter(prio)}
                                aria-pressed={enabled}
                              >
                                <span className={["notificationPriorityDot", prio === "high" ? "isHigh" : prio === "low" ? "isLow" : "isMedium"].join(" ")} aria-hidden="true" />
                                <span>{label}</span>
                                <span className="notificationsFilterChipCount">{count > 999 ? "999+" : count}</span>
                              </button>
                            );
                          })}
                        </div>
                      </div>

                      <span className="muted">{t("core.ui.notifications.location.global_totals")}</span>
                      {notificationHistoryFilters.map(provider => <React.Fragment key={provider.id}>{provider.render({
                        value: filter.extension?.id === provider.id ? filter.extension.value : null,
                        onChange: value => setFilter(previous => ({ ...previous, extension: value ? { id: provider.id, value } : undefined })),
                      })}</React.Fragment>)}
                      {filter.extension && !notificationHistoryFilters.some(provider => provider.id === filter.extension?.id) ? <button type="button" className="chipButton" onClick={() => setFilter(previous => ({ ...previous, extension: undefined }))}>{t("core.ui.notifications.filter.unavailable_selection")}</button> : null}
                      <HistoryPeriodFilter key={periodResetRevision} filter={filter} setFilter={setFilter} />
                      <HistoryLocationFilter filter={filter} setFilter={setFilter} locations={locations} />

                      {historyAssistants.map(assistant => <button type="button" className="chipButton" key={assistant.id} onClick={() => setHistoryAssistantId(assistant.id)}>{resolveLocalizedString(assistant.name)}</button>)}
                      {!historyAssistants.length ? <p className="muted">{t("core.ui.notifications.assistant_unavailable")}</p> : null}

                      {availableTypes.length > 1 ? (
                        <div className="notificationsFilterSection">
                          <div className="notificationsFilterSectionTitle">
                            {t("core.ui.notifications.filter.types", {}, "Types")}
                          </div>
                          <div className="notificationsFilterTypeList">
                            {availableTypes.map((type) => {
                              const enabled = filter.types.length === 0 || filter.types.includes(type);
                              return (
                                <label key={type} className="notificationsFilterTypeRow">
                                  <input
                                    type="checkbox"
                                    checked={enabled}
                                    onChange={() => toggleTypeFilter(type)}
                                  />
                                  <span className="notificationsFilterTypeLabel">{type}</span>
                                </label>
                              );
                            })}
                          </div>
                          {filter.types.length > 0 ? (
                            <button
                              type="button"
                              className="notificationsFilterLink"
                              onClick={() => setFilter((prev) => ({ ...prev, types: [] }))}
                            >
                              {t("core.ui.notifications.filter.types_all", {}, "Show all types")}
                            </button>
                          ) : null}
                        </div>
                      ) : null}

                      <div className="notificationsFilterFooter">
                        <button
                          type="button"
                          className="notificationsFilterLink"
                          onClick={clearFilter}
                          disabled={!filterRestrictive}
                        >
                          {t("core.ui.notifications.filter.reset", {}, "Reset to defaults")}
                        </button>
                      </div>
                    </div></Modal>
                  ) : null}
                </div> : null}

            {renderMode === "geographic" && <div className="geographicSelection">
              {activeNotification ? <>
                <button type="button" className="geographicSelectionDetails" onClick={() => setIsNotificationDetailsOpen(true)}>
                  {selectedThumbnail && !failedNotificationImageUrls.has(selectedThumbnail.trim()) && <img src={selectedThumbnail} alt="" onError={() => setFailedNotificationImageUrls(previous => new Set([...previous, selectedThumbnail.trim()]))} />}
                  <span><strong>{activeNotification.title}</strong><time>{formatDateTimeShort(locale, activeNotification.createdAt ?? activeNotification.updatedAt)}</time>
                    <small>{t(visibleNotifications.some(item => item.id === activeNotification.id) ? "core.map.selected_event" : "core.map.selected_outside_results")}</small></span>
                </button>
                {renderNotificationSpatialStatus(activeNotification)}
              </> : <button type="button" className="chipButton" onClick={() => setNotificationsExpanded(true)}>{t("core.map.select_event")}</button>}
              {filterRestrictive && <button type="button" className="geographicFilterSummary" onClick={openNotificationSearch}><Icon name="filter" /> {t("core.ui.notifications.active_filters")}</button>}
            </div>}
            {filterRestrictive ? <div className="notificationsActiveFilters" aria-label={t("core.ui.notifications.active_filters")}>
              <span className="notificationsActiveFiltersTitle">{t("core.ui.notifications.active_filters")}</span>
              <button type="button" className="chipButton notificationsActiveFilter" onClick={openNotificationSearch}>{t("core.ui.notifications.active_priorities", { priorities: filter.priorities.map(priority => t(`core.ui.notifications.filter.priority.${priority}`)).join(", ") || t("core.ui.notifications.filter.none") })}</button>
              {filter.query.trim() && <button type="button" className="chipButton notificationsActiveFilter" onClick={openNotificationSearch}>{t("core.ui.notifications.filter.search")}: {filter.query.trim()}</button>}
              {filter.period && <button type="button" className="chipButton notificationsActiveFilter" onClick={openNotificationSearch}>
                {t("core.ui.notifications.period.title")}: {t(`core.ui.notifications.period.${filter.period.preset}`)}
                {filter.period.preset === "custom" ? ` · ${filter.period.start} — ${filter.period.end}` : ""}
              </button>}
              {["camera_id", "area_id"].map((key, index) => filter.dataFilter?.[key] ? <button key={key} type="button" className="chipButton notificationsActiveFilter" onClick={openNotificationSearch}>
                {t(`core.ui.notifications.location.${index === 0 ? "camera" : "area"}`)}: {locations.options[index]?.find(option => option.value === filter.dataFilter?.[key])?.label || t("core.ui.notifications.location.missing")}
              </button> : null)}
              {filter.extension && <button type="button" className="chipButton notificationsActiveFilter" onClick={openNotificationSearch}>
                {notificationHistoryFilters.find(provider => provider.id === filter.extension?.id)?.renderSummary?.({ value: filter.extension.value }) ?? t("core.ui.notifications.extension_filter_active")}
              </button>}
              {filter.types.length > 0 && <button type="button" className="chipButton notificationsActiveFilter" onClick={openNotificationSearch}>{t("core.ui.notifications.filter.types")}: {filter.types.join(", ")}</button>}
            </div> : null}
            {historyReturnError ? <p role="alert" className="cardBody">{t("core.ui.notifications.original_unavailable")}</p> : null}
            {notificationsError ? <div className="cardBody" role="alert">{t("core.ui.notifications.filter.load_error")} <button type="button" className="chipButton" onClick={onNotificationsSearchRefresh}>{t("core.ui.notifications.location.retry")}</button></div> : null}
            {notificationsHistoryChanged ? <div className="notificationsSearchEntry" role="status">
              {t("core.ui.notifications.history_changed")} <button type="button" className="chipButton" disabled={notificationsLoading} onClick={onNotificationsSearchRefresh}>{t("core.ui.notifications.filter.refresh_search")}</button>
            </div> : null}
            {timeSearchMessage && (filter.query.trim() || filter.timeRange) ? <div className="notificationsSearchEntry" role="status">{timeSearchMessage}</div> : null}
            {notificationsSearchStatus && !notificationsError && filter.semantic && filter.query.trim() && !timezoneRequired ? (
              <div className="cardBody" role="status">
                {notificationsSearchStatus.mode === "semantic_visual"
                  ? visualSearchMessage
                  : notificationsSearchStatus.mode === "semantic_text"
                  ? t("core.ui.notifications.filter.semantic_summary", { limit: notificationsSearchStatus.scope_limit ?? 250, pending: notificationsSearchStatus.pending ?? 0 })
                  : notificationsSearchStatus.status === "degraded" ? t("core.ui.notifications.filter.semantic_fallback")
                    : t("core.ui.notifications.filter.search_mode")}
              </div>
            ) : null}
            <div
              className="railScroll"
              ref={notificationScrollRef}
              onPointerDown={noteNotificationPaginationUserEngagement}
              onTouchStart={noteNotificationPaginationUserEngagement}
              onWheel={noteNotificationPaginationUserEngagement}
            >
              {visibleNotifications.length === 0 && !notificationsLoading && !notificationsError ? (
                <div className="card">
                  <div className="cardBody" style={{ display: "grid", gap: 12 }}>
                    <p role="status" style={{ margin: 0 }}>{t(filter.query.trim() ? "core.ui.notifications.search_empty"
                      : filterRestrictive ? "core.ui.notifications.filtered_empty" : "core.ui.notifications_empty")}</p>
                    {filterRestrictive ? (
                      <button type="button" className="chipButton" style={{ justifySelf: "start", minHeight: 44 }}
                        onClick={() => setFilter({ priorities: [...ALL_PRIORITIES], types: [], query: "" })}>
                        {t("core.ui.notifications.show_all_events")}
                      </button>
                    ) : null}
                  </div>
                </div>
              ) : null}
              {visibleNotifications.map((n) => {
                const renderer = notificationRenderers.find((r) => r.type === n.type);
                const time = formatDateTimeShort(locale, n.createdAt ?? n.updatedAt);
                const title = n.title;
                const priority = notificationPriority(n);
                const priorityClass =
                  priority === "high" ? "isHigh" : priority === "low" ? "isLow" : "isMedium";
                const isActive = Boolean(activeNotificationId && n.id === activeNotificationId);
                const rawThumbUrl = notificationThumbnailUrl(n);
                const thumbUrl = rawThumbUrl && !failedNotificationImageUrls.has(rawThumbUrl.trim()) ? rawThumbUrl : null;
                return (
                  <button
                    className={["card", "cardButton", "notificationCard", isActive ? "isActive" : ""].filter(Boolean).join(" ")}
                    type="button"
                    key={n.id}
                    ref={isActive ? selectedNotificationRef : undefined}
                    onClick={() => {
                      onSelectNotification(n.id);
                      if (renderMode === "geographic") { setNotificationsExpanded(false); }
                      else if (shouldAutoCloseNotificationsAfterSelect()) {
                        setNotificationsOpen(false);
                        setNotificationImageIndex(0);
                        setIsNotificationDetailsOpen(true);
                      }
                    }}
                    onDoubleClick={() => {
                      onSelectNotification(n.id);
                      setNotificationImageIndex(0);
                      setIsNotificationDetailsOpen(true);
                    }}
                  >
                    <div className="notificationCardGrid">
                      <div className="notificationCardMain">
                        <div className="notificationCardHeader">
                          <div className="notificationCardTitleRow">
                            <span className={["notificationPriorityDot", priorityClass].join(" ")} aria-hidden="true" />
                            <div className="notificationCardTitle">{title}</div>
                          </div>
                          {time ? <div className="notificationCardTime">{time}</div> : null}
                        </div>

                        <div className="notificationCardBody">
                          {notificationDetailPanels.filter((panel) => panel.supports(n)).map((panel) => (
                            <React.Fragment key={panel.id}>{panel.renderSummary?.(n)}</React.Fragment>
                          ))}
                          {notificationDetailPanels.some(panel => panel.replacesRendererSummary && panel.supports(n)) ? renderNotificationSpatialStatus(n) : renderer ? (
                            <div className="notificationCardRenderer">{renderer.render(n)}</div>
                          ) : n.description ? (
                            <div className="notificationCardDesc">{n.description}</div>
                          ) : null}
                          {rawThumbUrl && !thumbUrl ? (
                            <div className="cardBody">{t("core.ui.notifications.image_unavailable")}</div>
                          ) : null}
                        </div>
                      </div>

                      {thumbUrl ? (
                        <img
                          className="notificationCardThumb"
                          src={thumbUrl}
                          alt=""
                          loading="lazy"
                          draggable={false}
                          onError={() => markNotificationImageFailed(thumbUrl)}
                        />
                      ) : null}
                    </div>
                  </button>
                );
              })}
              <div ref={notificationSentinelRef} />
              {notificationsLoading ? (
                <div className="card">
                  <div className="cardBody">{t("core.ui.loading")}</div>
                </div>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="notificationsCollapsedRail">
            <button
              className="iconButton notificationsToggleButton"
              ref={notificationsToggleRef}
              type="button"
              aria-label={t("core.ui.notifications.aria_open", {}, "Open notifications")}
              title={t("core.ui.notifications.aria_open", {}, "Open notifications")}
              onClick={() => setNotificationsOpen(true)}
            >
              <Icon name="bell" />
              {badgeLabel ? (
                <span className="notificationsToggleBadge">{badgeLabel}</span>
              ) : null}
            </button>
          </div>
        )}
      </div>

      {!isLiveCameraRenderMode(renderMode) && renderMode !== "geographic" && !viewportLoadingMessage && elements.length === 0 ? (
        <div className="emptyHint">
          <div className="card">
            <div className="cardTitle">{t("core.ui.empty_title")}</div>
            <div className="cardBody">{t("core.ui.empty_desc")}</div>
          </div>
        </div>
      ) : null}

      <Modal
        open={isRenderModalOpen}
        title={t("core.ui.render_modal.title")}
        onClose={() => setIsRenderModalOpen(false)}
      >
        <div className="choiceList">
          {(["space", "cameras", "panorama"] as const).map(group => {
            const views = orderedRenderViews.filter(view => (view.group ?? "space") === group);
            const builtins = group === "space" ? ["geographic", "3d", "2d", "vector2d"]
              : group === "cameras" && streamingAvailability !== "loading" && streamingAvailability !== "unavailable" ? ["streams", "camera-live"] : [];
            if (!views.length && !builtins.length) return null;
            return <React.Fragment key={group}>
              <h3 className="renderViewGroupTitle">{t(`core.map.${group === "panorama" ? "projection" : group === "cameras" ? "camera" : "space"}_group`)}</h3>
              {builtins.map(mode => <button key={mode} type="button" className={`choiceItem${renderMode === mode ? " isSelected" : ""}`}
                onClick={() => { setRenderMode(mode); setIsRenderModalOpen(false); }}>
                <span className="choiceTitle">{t(mode === "geographic" ? "core.map.map" : `core.ui.render_modal.option_${mode.replace("-", "_")}.title`)}</span>
                <span className="choiceDesc">{t(mode === "geographic" ? "core.map.render_description" : `core.ui.render_modal.option_${mode.replace("-", "_")}.desc`)}</span>
              </button>)}
              {views.map(view => {
                const availability = view.availability?.({ elements });
                return <div key={view.id} className="renderViewOption">
                  <button type="button" className={`choiceItem${renderMode === view.id ? " isSelected" : ""}`} disabled={availability?.available === false}
                    onClick={() => { setRenderMode(view.id); setIsRenderModalOpen(false); }}>
                    <span className="choiceTitle">{renderViewText(view.name, view.id)}</span>
                    <span className="choiceDesc">{renderViewText(view.description, "")}</span>
                  </button>
                  {availability && <div className="renderViewAvailability"><p className="muted">{renderViewText(availability.reason, "")}</p>
                    {!availability.available && <button className="chipButton" type="button" onClick={() => { setIsRenderModalOpen(false); onEditComposition(availability.initialAction, availability.elementId); }}>{t("core.actions.edit")}</button>}
                  </div>}
                </div>;
              })}
            </React.Fragment>;
          })}
        </div>
      </Modal>

      <CompositionSelectorModal
        requirePosition={renderMode === "geographic"}
        open={isCompositionModalOpen}
        compositions={compositions}
        activeCompositionId={activeCompositionId}
        onClose={() => setIsCompositionModalOpen(false)}
        onPosition={async id => { if (id !== activeCompositionId) await onActivateComposition(id); onEditComposition("georeference"); }}
        onActivate={onActivateComposition}
        onCreate={onCreateComposition}
        onRename={onRenameComposition}
        onDelete={onDeleteComposition}
      />

      {activeRenderView?.toolbarSelector ? (
        <Modal
          open={isRenderViewSelectorOpen}
          title={renderViewText(activeRenderView.toolbarSelector.title, renderButtonLabel)}
          onClose={() => setIsRenderViewSelectorOpen(false)}
        >
          {activeRenderView.toolbarSelector.renderContent({ close: () => setIsRenderViewSelectorOpen(false) })}
        </Modal>
      ) : null}

      <Modal
        open={isCameraLiveModalOpen}
        title={t("core.ui.camera_live.modal_title", {}, "Camera")}
        onClose={() => setIsCameraLiveModalOpen(false)}
      >
        <div className="choiceList">
          {cameraLiveViewsError ? <div className="errorText">{cameraLiveViewsError}</div> : null}
          {cameraLiveViewsLoading ? (
            <div className="choiceItem" aria-disabled="true">
              <div className="choiceTitle">{t("core.ui.loading", {}, "Loading...")}</div>
            </div>
          ) : null}
          {!cameraLiveViewsLoading && enabledCameraLiveViews.length === 0 ? (
            <div className="choiceItem" aria-disabled="true">
              <div className="choiceTitle">{t("core.ui.camera_live.empty_title", {}, "No live cameras")}</div>
              <div className="choiceDesc">
                {t("core.ui.camera_live.empty_desc", {}, "Create or enable a live view in Settings > Streams.")}
              </div>
            </div>
          ) : null}
          {enabledCameraLiveViews.map((liveView) => {
            const liveViewId = String(liveView.id || "").trim();
            const label = liveViewDisplayName(liveView, liveViewId);
            const description = String(liveView.camera_id || "").trim();
            return (
              <div
                key={liveViewId}
                className={["choiceItem", selectedCameraLiveViewId === liveViewId ? "isSelected" : ""].filter(Boolean).join(" ")}
                role="button"
                tabIndex={0}
                onClick={() => {
                  selectCameraLiveView(liveViewId, { remember: true });
                  setIsCameraLiveModalOpen(false);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    selectCameraLiveView(liveViewId, { remember: true });
                    setIsCameraLiveModalOpen(false);
                  }
                }}
              >
                <div className="choiceTitle">{label}</div>
                {description && description !== label ? <div className="choiceDesc">{description}</div> : null}
              </div>
            );
          })}
        </div>
      </Modal>

      {historyAssistantId ? <Modal open title={t("core.ui.notifications.assistant_title")} onClose={() => setHistoryAssistantId(null)} panelStyle={{ width: "min(760px, calc(100vw - 28px))" }}>
        {timeSearchMessage && <p>{timeSearchMessage}</p>}
        {historyAssistants.find(item => item.id === historyAssistantId)?.render({
          query: filter.query, dataFilter: filter.dataFilter, selection: filter.extension,
          priorities: filter.priorities, types: filter.types, visual: filter.semantic === "visual",
          createdAfter: filter.timeRange?.createdAfter, createdBefore: filter.timeRange?.createdBefore,
          onOpenNotification: async id => {
            previousHistorySelection.current = activeNotificationId;
            await onOpenHistoryNotification(id);
            setIsNotificationDetailsOpen(true);
          },
        }) ?? <p role="status">{t("core.ui.notifications.assistant_unavailable")}</p>}
      </Modal> : null}

      <Modal
        open={isNotificationDetailsOpen}
        title={t("core.ui.notifications.details.title", {}, "Notification details")}
        onClose={() => {
          setIsNotificationDetailsOpen(false);
          if (historyAssistantId && previousHistorySelection.current) onSelectNotification(previousHistorySelection.current);
          previousHistorySelection.current = null;
        }}
        panelClassName="notificationDetailsModalPanel"
        bodyClassName="notificationDetailsModalBody"
      >
        {activeNotification ? (
          <div className="notificationDetailsRoot">
            <div className="notificationDetailsHeader">
              <div className="notificationDetailsTitle">{activeNotification.title}</div>
              {activeNotificationSubtitle ? <div className="notificationDetailsSubtitle">{activeNotificationSubtitle}</div> : null}
              {activeNotification.description ? <div className="notificationDetailsDescription">{notificationDescription(activeNotification)}</div> : null}
              {renderNotificationSpatialStatus(activeNotification)}
            </div>

            {activeNotificationRenderer?.renderDetails ? (
              activeNotificationRenderer.renderDetails(activeNotification)
            ) : null}
            {notificationDetailPanels.filter((panel) => panel.supports(activeNotification)).map((panel) => (
              <React.Fragment key={panel.id}>{panel.render(activeNotification)}</React.Fragment>
            ))}

            {activeNotificationImage ? (
              <div className="notificationGallery">
                <div className="notificationGalleryStage">
                  <button
                    className="iconButton notificationGalleryNav"
                    type="button"
                    aria-label={t("core.ui.notifications.details.image_prev", {}, "Previous image")}
                    title={t("core.ui.notifications.details.image_prev", {}, "Previous image")}
                    onClick={showPrevNotificationImage}
                    disabled={activeNotificationImages.length <= 1}
                  >
                    <Icon name="chevron-left" />
                  </button>

                  <button
                    className="notificationGalleryImageButton"
                    type="button"
                    onClick={() => openFullscreenNotificationImage(notificationImageIndex)}
                    aria-label={t("core.ui.image_viewer.open", {}, "Open image fullscreen")}
                    title={t("core.ui.image_viewer.open", {}, "Open image fullscreen")}
                  >
                    <img
                      className="notificationGalleryImage"
                      src={activeNotificationImage.url}
                      alt=""
                      onError={() => markNotificationImageFailed(activeNotificationImage.url)}
                    />
                  </button>

                  <button
                    className="iconButton notificationGalleryNav"
                    type="button"
                    aria-label={t("core.ui.notifications.details.image_next", {}, "Next image")}
                    title={t("core.ui.notifications.details.image_next", {}, "Next image")}
                    onClick={showNextNotificationImage}
                    disabled={activeNotificationImages.length <= 1}
                  >
                    <Icon name="chevron-right" />
                  </button>
                </div>

                <div className="notificationGalleryCaption">
                  <span>{activeNotificationImage.label}</span>
                  <div className="notificationGalleryCounter">
                    {t(
                      "core.ui.notifications.details.image_counter",
                      { current: notificationImageIndex + 1, total: activeNotificationImages.length },
                      `${notificationImageIndex + 1} / ${activeNotificationImages.length}`,
                    )}
                  </div>
                </div>
                {activeNotificationImageMeta ? <div className="notificationGalleryMeta">{activeNotificationImageMeta}</div> : null}

                {activeNotificationImages.length > 1 ? (
                  <div className="notificationGalleryThumbs">
                    {activeNotificationImages.map((item, idx) => (
                      <button
                        key={item.id}
                        type="button"
                        className={["notificationGalleryThumbButton", idx === notificationImageIndex ? "isActive" : ""].filter(Boolean).join(" ")}
                        onClick={() => setNotificationImageIndex(idx)}
                        title={t("core.ui.notifications.details.image_counter", { current: idx + 1, total: activeNotificationImages.length })}
                        aria-label={t("core.ui.notifications.details.image_counter", { current: idx + 1, total: activeNotificationImages.length })}
                      >
                        <img
                          src={item.url}
                          alt=""
                          className="notificationGalleryThumbImage"
                          loading="lazy"
                          onError={() => markNotificationImageFailed(item.url)}
                        />
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : activeNotificationRenderer?.renderDetails || notificationDetailPanels.some(panel => panel.supports(activeNotification)) ? null : (
              <div className="cardBody">{t("core.ui.notifications.details.no_images", {}, "No images found for this detection.")}</div>
            )}

            <div className="notificationDetailsGrid">
              {activeNotificationDetails.filter(field => !field.technical).map((field) => (
                <div className="notificationDetailField" key={`${field.label}:${field.value}`}>
                  <div className="notificationDetailLabel">{field.label}</div>
                  <div className="notificationDetailValue">{field.value}</div>
                </div>
              ))}
            </div>

            {activeNotification.payload ? (
              <details className="notificationPayloadDetails">
                <summary>{t("core.ui.notifications.details.technical", {}, "Technical details")}</summary>
                <div className="notificationDetailsGrid">
                  {activeNotificationDetails.filter(field => field.technical).map(field => (
                    <div className="notificationDetailField" key={field.label}>
                      <div className="notificationDetailLabel">{field.label}</div>
                      <div className="notificationDetailValue">{field.value}</div>
                    </div>
                  ))}
                </div>
                <pre>{JSON.stringify(activeNotification.payload, null, 2)}</pre>
              </details>
            ) : null}
          </div>
        ) : (
          <div className="cardBody" role="status">{activeNotificationId ? t("core.ui.loading") : t("core.ui.notifications.details.no_selection", {}, "Select a notification to inspect it.")}</div>
        )}
      </Modal>

      <FullscreenImageViewer
        open={fullscreenNotificationImageOpen}
        items={fullscreenNotificationImageItems}
        index={fullscreenNotificationImageIndex}
        onIndexChange={setFullscreenNotificationImageIndex}
        onClose={() => setFullscreenNotificationImageOpen(false)}
        onImageError={markNotificationImageFailed}
      />

      <Modal
        open={isActionModalOpen}
        title={actionTitle}
        onClose={() => {
          setActiveElementId(null);
        }}
      >
        {activeElement ? (
          activeType?.renderActionModal ? (
            activeType.renderActionModal({
              element: activeElement,
              update: (patch) => updateElement(activeElement.id, patch),
              close: () => setActiveElementId(null),
              api,
            })
          ) : (
            <div className="cardBody">{t("core.ui.action_unavailable")}</div>
          )
        ) : null}
      </Modal>

      <Modal
        open={Boolean(imageModal)}
        title={imageModal?.title ?? t("core.ui.image_preview")}
        onClose={() => setImageModal(null)}
      >
        {imageModal?.subtitle ? <div className="cardMeta">{imageModal.subtitle}</div> : null}
        {imageModal?.url ? (
          <img
            src={imageModal.url}
            alt=""
            style={{
              width: "100%",
              maxHeight: "72vh",
              objectFit: "contain",
              borderRadius: "var(--radius-panel)",
              border: "1px solid var(--color-border-subtle)",
              background: "var(--color-surface-frost)",
              marginTop: imageModal.subtitle ? 12 : 0,
              display: "block",
            }}
          />
        ) : null}
      </Modal>
    </div>
  );
}
