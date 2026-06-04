import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostApi,
  Notification,
  NotificationRenderer,
  RenderViewDefinition,
  ViewSettings,
} from "@toposync/plugin-api";

import type { Composition, CompositionSummary, NotificationsCount } from "../../util/api";
import { i18n } from "../../util/i18n";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { FullscreenImageViewer, requestFullscreenImageViewer, type FullscreenImageViewerItem } from "../FullscreenImageViewer";
import { Icon } from "../Icon";
import { Viewport3D } from "../Viewport3D";
import { MainViewport2D } from "../main2d/MainViewport2D";
import { MainViewportVector2D } from "../main2d/MainViewportVector2D";
import {
  notificationImageItems,
  notificationPriority,
  notificationThumbnailUrl,
  type NotificationImageItem,
} from "../notifications/pipelinesNotifications";
import { StreamsDashboard } from "../streams/StreamsDashboard";

type Props = {
  compositionName: string;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  compositionLoaded: boolean;
  criticalExtensionsLoaded: boolean;
  allExtensionsLoaded: boolean;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  viewSettings: ViewSettings;
  notificationRenderers: NotificationRenderer[];
  notifications: Notification[];
  notificationsCount: NotificationsCount;
  notificationsHasMore: boolean;
  activeNotificationId: string | null;
  notificationsLoading: boolean;
  renderViews: RenderViewDefinition[];
  onSelectNotification: (notificationId: string) => void;
  onLoadMoreNotifications: () => void;
  onNotificationsViewed: () => void;
  api: HostApi;
  updateElement: (elementId: string, patch: CompositionElementPatch) => void;
  onEditComposition: () => void;
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

function formatDurationVerbose(secondsRaw: unknown): string | null {
  const totalSeconds = asFiniteNumber(secondsRaw);
  if (totalSeconds == null || totalSeconds < 0) return null;
  const seconds = Math.floor(totalSeconds);
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (remMins > 0 && secs > 0) return `${hours}h ${remMins}m ${secs}s`;
  if (remMins > 0) return `${hours}h ${remMins}m`;
  return secs > 0 ? `${hours}h ${secs}s` : `${hours}h`;
}

const NOTIFICATIONS_OPEN_STORAGE_KEY = "toposync.notifications_open.v1";
const NOTIFICATIONS_FILTER_STORAGE_KEY = "toposync.notifications_filter.v2";

type Priority = "low" | "medium" | "high";
const ALL_PRIORITIES: Priority[] = ["high", "medium", "low"];

type NotificationsFilter = {
  priorities: Priority[];
  types: string[]; // empty array = "all types"
  query: string;
};

const DEFAULT_FILTER: NotificationsFilter = {
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

function loadNotificationsFilter(): NotificationsFilter {
  if (typeof window === "undefined") return { ...DEFAULT_FILTER };
  try {
    const raw = localStorage.getItem(NOTIFICATIONS_FILTER_STORAGE_KEY);
    if (!raw) return { ...DEFAULT_FILTER };
    const parsed = JSON.parse(raw) as Partial<NotificationsFilter>;
    const priorities = Array.isArray(parsed.priorities)
      ? parsed.priorities.filter((p): p is Priority => p === "low" || p === "medium" || p === "high")
      : DEFAULT_FILTER.priorities;
    const types = Array.isArray(parsed.types) ? parsed.types.filter((t): t is string => typeof t === "string" && t.length > 0) : [];
    const query = typeof parsed.query === "string" ? parsed.query : "";
    return {
      priorities: priorities.length > 0 ? priorities : DEFAULT_FILTER.priorities,
      types,
      query,
    };
  } catch {
    return { ...DEFAULT_FILTER };
  }
}

function isFilterRestrictive(filter: NotificationsFilter): boolean {
  if (filter.priorities.length < ALL_PRIORITIES.length) return true;
  if (filter.types.length > 0) return true;
  if (filter.query.trim().length > 0) return true;
  return false;
}

type BuiltinRenderMode = "3d" | "2d" | "vector2d" | "streams";
type RenderMode = BuiltinRenderMode | string;

const RENDER_MODE_STORAGE_KEY = "toposync.render_mode.v1";
const STREAMS_OVERLAY_IDLE_MS = 2500;
type WindowWithIdleCallback = Window & {
  requestIdleCallback?: (callback: () => void, options?: { timeout?: number }) => number;
  cancelIdleCallback?: (handle: number) => void;
};

const BUILTIN_RENDER_MODES = new Set<string>(["3d", "2d", "vector2d", "streams"]);

function isBuiltinRenderMode(value: string): value is BuiltinRenderMode {
  return BUILTIN_RENDER_MODES.has(value);
}

export function MainScreen({
  compositionName,
  compositions,
  activeCompositionId,
  compositionLoaded,
  criticalExtensionsLoaded,
  allExtensionsLoaded,
  elements,
  elementTypesById,
  viewSettings,
  notificationRenderers,
  notifications,
  notificationsCount,
  notificationsHasMore,
  activeNotificationId,
  notificationsLoading,
  renderViews,
  onSelectNotification,
  onLoadMoreNotifications,
  onNotificationsViewed,
  api,
  updateElement,
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
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [activeElementId, setActiveElementId] = useState<string | null>(null);
  const [imageModal, setImageModal] = useState<{ url: string; title: string; subtitle?: string } | null>(null);
  const [isNotificationDetailsOpen, setIsNotificationDetailsOpen] = useState(false);
  const [notificationImageIndex, setNotificationImageIndex] = useState(0);
  const [fullscreenNotificationImageOpen, setFullscreenNotificationImageOpen] = useState(false);
  const [fullscreenNotificationImageIndex, setFullscreenNotificationImageIndex] = useState(0);
  const [notificationsOpen, setNotificationsOpen] = useState(() => loadNotificationsOpen());
  const [filter, setFilter] = useState<NotificationsFilter>(() => loadNotificationsFilter());
  const [isFilterPopoverOpen, setIsFilterPopoverOpen] = useState(false);
  const filterPopoverRef = useRef<HTMLDivElement | null>(null);
  const filterButtonRef = useRef<HTMLButtonElement | null>(null);
  const [renderMode, setRenderMode] = useState<RenderMode>(() => {
    try {
      const saved = localStorage.getItem(RENDER_MODE_STORAGE_KEY);
      return saved && saved.trim() ? saved.trim() : "3d";
    } catch {
      return "3d";
    }
  });
  const [streamsOverlayVisible, setStreamsOverlayVisible] = useState(true);
  const notificationScrollRef = useRef<HTMLDivElement | null>(null);
  const notificationSentinelRef = useRef<HTMLDivElement | null>(null);
  const streamsOverlayTimerRef = useRef<number | null>(null);
  const autoFilteredPageRequestedRef = useRef(false);

  const clearStreamsOverlayTimer = useCallback(() => {
    const timerId = streamsOverlayTimerRef.current;
    if (timerId == null) return;
    window.clearTimeout(timerId);
    streamsOverlayTimerRef.current = null;
  }, []);

  const scheduleStreamsOverlayHide = useCallback(() => {
    clearStreamsOverlayTimer();
    if (renderMode !== "streams") {
      setStreamsOverlayVisible(true);
      return;
    }
    if (document.visibilityState !== "visible") {
      setStreamsOverlayVisible(false);
      return;
    }
    streamsOverlayTimerRef.current = window.setTimeout(() => {
      streamsOverlayTimerRef.current = null;
      setStreamsOverlayVisible(false);
    }, STREAMS_OVERLAY_IDLE_MS);
  }, [clearStreamsOverlayTimer, renderMode]);

  useEffect(() => {
    if (renderMode !== "streams") {
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
    window.addEventListener("keydown", onKeyDown);
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearStreamsOverlayTimer();
    };
  }, [clearStreamsOverlayTimer, renderMode, scheduleStreamsOverlayHide]);

  const activeNotification = useMemo(() => {
    if (!activeNotificationId) return null;
    return notifications.find((n) => n.id === activeNotificationId) ?? null;
  }, [activeNotificationId, notifications]);

  const activeNotificationImages = useMemo(() => {
    if (!activeNotification) return [];
    return notificationImageItems(activeNotification);
  }, [activeNotification]);

  const activeNotificationImage = activeNotificationImages[notificationImageIndex] ?? null;

  const activeNotificationDetails = useMemo(() => {
    if (!activeNotification) return [] as NotificationDetailField[];
    const payload = asRecord(activeNotification.payload);
    const data = asRecord(payload.data);
    const event = asRecord(payload.event);

    const out: NotificationDetailField[] = [];
    const push = (label: string, valueRaw: unknown) => {
      const value = asTrimmedString(valueRaw);
      if (!value) return;
      out.push({ label, value });
    };

    const cameraName = asTrimmedString(data.camera_name);
    const cameraId = asTrimmedString(data.camera_id);
    const cameraLabel = [cameraName, cameraId].filter(Boolean).join(" • ");
    const trackingId = asTrimmedString(payload.tracking_id) || asTrimmedString(data.tracking_id);
    const eventId = asTrimmedString(payload.event_id) || asTrimmedString(data.event_id);
    const duration = formatDurationVerbose(event.duration_seconds);
    const createdAt = formatDateTimeLong(locale, activeNotification.createdAt);
    const updatedAt = formatDateTimeLong(locale, activeNotification.updatedAt);

    push(t("core.ui.notifications.details.meta.type", {}, "Type"), activeNotification.type);
    push(t("core.ui.notifications.details.meta.priority", {}, "Priority"), payload.priority);
    push(t("core.ui.notifications.details.meta.status", {}, "Status"), payload.status);
    push(t("core.ui.notifications.details.meta.lifecycle", {}, "Lifecycle"), payload.lifecycle);
    push(t("core.ui.notifications.details.meta.pipeline", {}, "Pipeline"), payload.pipeline_name);
    push(t("core.ui.notifications.details.meta.camera", {}, "Camera"), cameraLabel);
    push(t("core.ui.notifications.details.meta.area", {}, "Area"), data.area_label);
    push(t("core.ui.notifications.details.meta.tracking_id", {}, "Tracking ID"), trackingId);
    push(t("core.ui.notifications.details.meta.event_id", {}, "Event ID"), eventId);
    push(t("core.ui.notifications.details.meta.duration", {}, "Duration"), duration);
    push(t("core.ui.notifications.details.meta.created_at", {}, "Created"), createdAt);
    push(t("core.ui.notifications.details.meta.updated_at", {}, "Updated"), updatedAt);

    out.push({
      label: t("core.ui.notifications.details.meta.images", {}, "Images"),
      value: String(activeNotificationImages.length),
    });
    return out;
  }, [activeNotification, activeNotificationImages.length, locale, t]);

  const activeNotificationSubtitle = useMemo(() => {
    if (!activeNotification) return null;
    const payload = asRecord(activeNotification.payload);
    const data = asRecord(payload.data);
    const camera = asTrimmedString(data.camera_name) || asTrimmedString(data.camera_id);
    const area = asTrimmedString(data.area_label);
    const pipeline = asTrimmedString(payload.pipeline_name);
    const parts = [camera || pipeline, area].filter(Boolean);
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
    const tsLabel = formatTimestampMillis(locale, item.storedTsMs);
    if (tsLabel) parts.push(tsLabel);
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

  useEffect(() => {
    try {
      localStorage.setItem(RENDER_MODE_STORAGE_KEY, renderMode);
    } catch {
      // ignore
    }
  }, [renderMode]);

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
    try {
      localStorage.setItem(NOTIFICATIONS_FILTER_STORAGE_KEY, JSON.stringify(filter));
    } catch {
      // ignore
    }
  }, [filter]);

  useEffect(() => {
    autoFilteredPageRequestedRef.current = false;
  }, [filter]);

  useEffect(() => {
    if (activeNotification) return;
    setIsNotificationDetailsOpen(false);
    setNotificationImageIndex(0);
    setFullscreenNotificationImageOpen(false);
    setFullscreenNotificationImageIndex(0);
  }, [activeNotification]);

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
    for (const n of notifications) {
      if (typeof n.type === "string" && n.type) seen.add(n.type);
    }
    return Array.from(seen).sort();
  }, [notifications]);

  const matchesFilter = useCallback(
    (n: Notification): boolean => {
      const prio = notificationPriority(n);
      if (!filter.priorities.includes(prio)) return false;
      if (filter.types.length > 0 && !filter.types.includes(n.type)) return false;
      const q = filter.query.trim().toLowerCase();
      if (q) {
        const haystack = `${n.title ?? ""}\n${n.description ?? ""}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    },
    [filter],
  );

  const visibleNotifications = useMemo(() => {
    return notifications.filter((n) => {
      if (matchesFilter(n)) return true;
      // Always keep the actively-selected notification visible so the
      // selection isn't silently lost when the user tightens the filter.
      return Boolean(activeNotificationId && n.id === activeNotificationId);
    });
  }, [activeNotificationId, notifications, matchesFilter]);

  const filterRestrictive = useMemo(() => isFilterRestrictive(filter), [filter]);

  const filteredLoadedCount = useMemo(
    () => notifications.reduce((acc, n) => (matchesFilter(n) ? acc + 1 : acc), 0),
    [notifications, matchesFilter],
  );

  const badgeLabel = useMemo(() => {
    const unreadCount = filter.priorities.reduce(
      (acc, prio) => acc + (notificationsCount.unread_by_priority[prio] ?? 0),
      0,
    );
    if (unreadCount <= 0) return null;
    return unreadCount > 999 ? "999+" : String(unreadCount);
  }, [filter, notificationsCount]);

  const hiddenByFilterCount = useMemo(() => {
    if (!filterRestrictive) return 0;
    return notifications.length - filteredLoadedCount;
  }, [filterRestrictive, notifications.length, filteredLoadedCount]);

  // When a restrictive filter leaves few visible items but more pages exist,
  // pull one extra page during idle. Further pagination is left to scroll.
  useEffect(() => {
    if (!notificationsOpen) return;
    if (!filterRestrictive) return;
    if (notificationsLoading) return;
    if (!notificationsHasMore) return;
    if (filteredLoadedCount >= 12) return;
    if (autoFilteredPageRequestedRef.current) return;

    autoFilteredPageRequestedRef.current = true;
    const win = window as WindowWithIdleCallback;
    if (typeof win.requestIdleCallback === "function") {
      const handle = win.requestIdleCallback(() => onLoadMoreNotifications(), { timeout: 1500 });
      return () => win.cancelIdleCallback?.(handle);
    }
    const handle = window.setTimeout(() => onLoadMoreNotifications(), 180);
    return () => window.clearTimeout(handle);
  }, [notificationsOpen, filterRestrictive, notificationsLoading, notificationsHasMore, filteredLoadedCount, onLoadMoreNotifications]);

  // Close the filter popover when clicking outside.
  useEffect(() => {
    if (!isFilterPopoverOpen) return;
    function onPointerDown(event: MouseEvent | TouchEvent): void {
      const target = event.target as Node | null;
      if (!target) return;
      if (filterPopoverRef.current?.contains(target)) return;
      if (filterButtonRef.current?.contains(target)) return;
      setIsFilterPopoverOpen(false);
    }
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") setIsFilterPopoverOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [isFilterPopoverOpen]);

  const togglePriorityFilter = useCallback((prio: Priority) => {
    setFilter((prev) => {
      const has = prev.priorities.includes(prio);
      const next = has ? prev.priorities.filter((p) => p !== prio) : [...prev.priorities, prio];
      // Don't allow zero priorities — falls back to default.
      return { ...prev, priorities: next.length > 0 ? next : DEFAULT_FILTER.priorities };
    });
  }, []);

  const toggleTypeFilter = useCallback((type: string) => {
    setFilter((prev) => {
      const has = prev.types.includes(type);
      const next = has ? prev.types.filter((t) => t !== type) : [...prev.types, type];
      return { ...prev, types: next };
    });
  }, []);

  const clearFilter = useCallback(() => setFilter({ ...DEFAULT_FILTER }), []);

  const orderedRenderViews = useMemo(
    () =>
      [...renderViews]
        .filter((view) => view.id && !isBuiltinRenderMode(view.id))
        .sort((a, b) => (a.order ?? 1000) - (b.order ?? 1000) || a.id.localeCompare(b.id)),
    [renderViews],
  );
  const activeRenderView = orderedRenderViews.find((view) => view.id === renderMode) ?? null;

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
    renderMode === "3d"
      ? "3D"
      : renderMode === "2d"
        ? "2D"
        : renderMode === "vector2d"
          ? t("core.ui.render_modal.option_vector2d.title", {}, "2D (Vector)")
          : renderMode === "streams"
            ? t("core.ui.render_modal.option_streams.title", {}, "Streams")
            : activeRenderView
              ? renderViewText(activeRenderView.name, activeRenderView.id)
              : t("core.ui.render_modal.option_loading.title", {}, "Loading view");
  const renderButtonLabel = renderModeLabel || t("core.ui.rendering");
  const compositionButtonLabel = compositionName.trim() || t("core.ui.composition");
  const viewportLoadingMessage =
    renderMode === "streams"
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
    <div className="screenRoot">
      {viewportLoadingMessage ? (
        <div className="viewportRoot mainViewportLoadingRoot">
          <div className="mainViewportLoadingCard">
            <div className="mainViewportLoadingSpinner" aria-hidden="true" />
            <div className="mainViewportLoadingText">{viewportLoadingMessage}</div>
          </div>
        </div>
      ) : renderMode === "3d" ? (
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
      ) : (
        <>
          {renderMode === "2d" ? (
            <MainViewport2D
              elements={elements}
              elementTypesById={elementTypesById}
              compositionId={activeCompositionId}
              onElementActivated={handleElementActivated}
              activeNotification={activeNotification}
              activeNotificationRenderer={activeNotificationRenderer}
            />
          ) : renderMode === "vector2d" ? (
            <MainViewportVector2D
              elements={elements}
              elementTypesById={elementTypesById}
              compositionId={activeCompositionId}
              onElementActivated={handleElementActivated}
              activeNotification={activeNotification}
              activeNotificationRenderer={activeNotificationRenderer}
            />
          ) : activeRenderView ? (
            <>
              {activeRenderView.render({
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
            </>
          ) : renderMode === "streams" ? (
            <StreamsDashboard uiVisible={streamsOverlayVisible} isActive={renderMode === "streams"} />
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
          renderMode === "streams" ? "streamsOverlayAutoHide" : "",
          renderMode === "streams" && !streamsOverlayVisible ? "isHidden" : "isVisible",
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
        {renderMode !== "streams" ? (
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
        {renderMode !== "streams" ? (
          <button
            className="iconButton iconButtonPrimary"
            type="button"
            aria-label={t("core.actions.edit")}
            title={t("core.actions.edit")}
            onClick={onEditComposition}
          >
            <Icon name="pen-to-square" />
          </button>
        ) : null}
      </div>

      <div
        className={[
          "overlayLeft",
          notificationsOpen ? "isOpen" : "isCollapsed",
          renderMode === "streams" ? "streamsOverlayAutoHide" : "",
          renderMode === "streams" && !streamsOverlayVisible ? "isHidden" : "isVisible",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {notificationsOpen ? (
          <div className="rail railNotifications">
            <div className="railHeader">
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

                <div className="notificationsFilterAnchor">
                  <button
                    ref={filterButtonRef}
                    className={["iconButton", filterRestrictive ? "iconButtonPrimary" : ""].filter(Boolean).join(" ")}
                    type="button"
                    aria-label={t("core.ui.notifications.filter.aria", {}, "Filter notifications")}
                    title={t("core.ui.notifications.filter.aria", {}, "Filter notifications")}
                    aria-expanded={isFilterPopoverOpen}
                    onClick={() => setIsFilterPopoverOpen((prev) => !prev)}
                  >
                    <Icon name="filter" />
                  </button>

                  {isFilterPopoverOpen ? (
                    <div className="notificationsFilterPopover" ref={filterPopoverRef} role="dialog">
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

                      <div className="notificationsFilterSection">
                        <label className="notificationsFilterSectionTitle" htmlFor="notificationsFilterSearch">
                          {t("core.ui.notifications.filter.search", {}, "Search")}
                        </label>
                        <input
                          id="notificationsFilterSearch"
                          type="search"
                          className="notificationsFilterSearch"
                          placeholder={t("core.ui.notifications.filter.search_placeholder", {}, "Title or description")}
                          value={filter.query}
                          onChange={(e) => setFilter((prev) => ({ ...prev, query: e.target.value }))}
                        />
                      </div>

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
                    </div>
                  ) : null}
                </div>

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

            <div className="railScroll" ref={notificationScrollRef}>
              {visibleNotifications.length === 0 ? (
                <div className="card">
                  <div className="cardBody">
                    {filterRestrictive && hiddenByFilterCount > 0
                      ? t(
                          "core.ui.notifications.filtered_empty",
                          { count: hiddenByFilterCount },
                          "{{count}} notifications hidden by filter.",
                        )
                      : t("core.ui.notifications_empty")}
                  </div>
                  {filterRestrictive ? (
                    <button type="button" className="notificationsFilterLink" onClick={clearFilter}>
                      {t("core.ui.notifications.filter.reset", {}, "Reset to defaults")}
                    </button>
                  ) : null}
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
                const thumbUrl = notificationThumbnailUrl(n);
                return (
                  <button
                    className={["card", "cardButton", "notificationCard", isActive ? "isActive" : ""].filter(Boolean).join(" ")}
                    type="button"
                    key={n.id}
                    onClick={() => {
                      onSelectNotification(n.id);
                      if (shouldAutoCloseNotificationsAfterSelect()) setNotificationsOpen(false);
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
                          {renderer ? (
                            <div className="notificationCardRenderer">{renderer.render(n)}</div>
                          ) : n.description ? (
                            <div className="notificationCardDesc">{n.description}</div>
                          ) : n.payload ? (
                            <div className="notificationCardDesc">{JSON.stringify(n.payload)}</div>
                          ) : null}
                        </div>
                      </div>

                      {thumbUrl ? (
                        <img className="notificationCardThumb" src={thumbUrl} alt="" loading="lazy" draggable={false} />
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

      {renderMode !== "streams" && !viewportLoadingMessage && elements.length === 0 ? (
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
          <div
            className={["choiceItem", renderMode === "3d" ? "isSelected" : ""].filter(Boolean).join(" ")}
            role="button"
            tabIndex={0}
            onClick={() => {
              setRenderMode("3d");
              setIsRenderModalOpen(false);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                setRenderMode("3d");
                setIsRenderModalOpen(false);
              }
            }}
          >
            <div className="choiceTitle">{t("core.ui.render_modal.option_3d.title")}</div>
            <div className="choiceDesc">{t("core.ui.render_modal.option_3d.desc")}</div>
          </div>
          <div
            className={["choiceItem", renderMode === "2d" ? "isSelected" : ""].filter(Boolean).join(" ")}
            role="button"
            tabIndex={0}
            onClick={() => {
              setRenderMode("2d");
              setIsRenderModalOpen(false);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                setRenderMode("2d");
                setIsRenderModalOpen(false);
              }
            }}
          >
            <div className="choiceTitle">{t("core.ui.render_modal.option_2d.title")}</div>
            <div className="choiceDesc">{t("core.ui.render_modal.option_2d.desc")}</div>
          </div>
          <div
            className={["choiceItem", renderMode === "vector2d" ? "isSelected" : ""].filter(Boolean).join(" ")}
            role="button"
            tabIndex={0}
            onClick={() => {
              setRenderMode("vector2d");
              setIsRenderModalOpen(false);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                setRenderMode("vector2d");
                setIsRenderModalOpen(false);
              }
            }}
          >
            <div className="choiceTitle">{t("core.ui.render_modal.option_vector2d.title", {}, "2D (Vector)")}</div>
            <div className="choiceDesc">
              {t("core.ui.render_modal.option_vector2d.desc", {}, "Lightweight vector plan with live controls and cached effects.")}
            </div>
          </div>
          <div
            className={["choiceItem", renderMode === "streams" ? "isSelected" : ""].filter(Boolean).join(" ")}
            role="button"
            tabIndex={0}
            onClick={() => {
              setRenderMode("streams");
              setIsRenderModalOpen(false);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                setRenderMode("streams");
                setIsRenderModalOpen(false);
              }
            }}
          >
            <div className="choiceTitle">{t("core.ui.render_modal.option_streams.title", {}, "Streams")}</div>
            <div className="choiceDesc">
              {t("core.ui.render_modal.option_streams.desc", {}, "Display configured transmissions in a paged live dashboard.")}
            </div>
          </div>
          {orderedRenderViews.map((view) => {
            const title = renderViewText(view.name, view.id);
            const desc = renderViewText(view.description, "");
            return (
              <div
                key={view.id}
                className={["choiceItem", renderMode === view.id ? "isSelected" : ""].filter(Boolean).join(" ")}
                role="button"
                tabIndex={0}
                onClick={() => {
                  setRenderMode(view.id);
                  setIsRenderModalOpen(false);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    setRenderMode(view.id);
                    setIsRenderModalOpen(false);
                  }
                }}
              >
                <div className="choiceTitle">{title}</div>
                {desc ? <div className="choiceDesc">{desc}</div> : null}
              </div>
            );
          })}
        </div>
      </Modal>

      <CompositionSelectorModal
        open={isCompositionModalOpen}
        compositions={compositions}
        activeCompositionId={activeCompositionId}
        onClose={() => setIsCompositionModalOpen(false)}
        onActivate={onActivateComposition}
        onCreate={onCreateComposition}
        onRename={onRenameComposition}
        onDelete={onDeleteComposition}
      />

      <Modal
        open={isNotificationDetailsOpen}
        title={t("core.ui.notifications.details.title", {}, "Notification details")}
        onClose={() => setIsNotificationDetailsOpen(false)}
        panelClassName="notificationDetailsModalPanel"
        bodyClassName="notificationDetailsModalBody"
      >
        {activeNotification ? (
          <div className="notificationDetailsRoot">
            <div className="notificationDetailsHeader">
              <div className="notificationDetailsTitle">{activeNotification.title}</div>
              {activeNotificationSubtitle ? <div className="notificationDetailsSubtitle">{activeNotificationSubtitle}</div> : null}
              {activeNotification.description ? <div className="notificationDetailsDescription">{activeNotification.description}</div> : null}
            </div>

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
                    <img className="notificationGalleryImage" src={activeNotificationImage.url} alt="" />
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
                  <div className="notificationGalleryLabel">{activeNotificationImage.label}</div>
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
                        title={item.label}
                        aria-label={item.label}
                      >
                        <img src={item.url} alt="" className="notificationGalleryThumbImage" loading="lazy" />
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="cardBody">{t("core.ui.notifications.details.no_images", {}, "No images found for this detection.")}</div>
            )}

            <div className="notificationDetailsGrid">
              {activeNotificationDetails.map((field) => (
                <div className="notificationDetailField" key={`${field.label}:${field.value}`}>
                  <div className="notificationDetailLabel">{field.label}</div>
                  <div className="notificationDetailValue">{field.value}</div>
                </div>
              ))}
            </div>

            {activeNotification.payload ? (
              <details className="notificationPayloadDetails">
                <summary>{t("core.ui.notifications.details.payload", {}, "Payload")}</summary>
                <pre>{JSON.stringify(activeNotification.payload, null, 2)}</pre>
              </details>
            ) : null}
          </div>
        ) : (
          <div className="cardBody">{t("core.ui.notifications.details.no_selection", {}, "Select a notification to inspect it.")}</div>
        )}
      </Modal>

      <FullscreenImageViewer
        open={fullscreenNotificationImageOpen}
        items={fullscreenNotificationImageItems}
        index={fullscreenNotificationImageIndex}
        onIndexChange={setFullscreenNotificationImageIndex}
        onClose={() => setFullscreenNotificationImageOpen(false)}
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
