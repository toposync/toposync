import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostApi,
  Notification,
  NotificationRenderer,
  ViewSettings,
} from "@toposync/plugin-api";

import type { Composition, CompositionSummary } from "../../util/api";
import { i18n } from "../../util/i18n";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { Icon } from "../Icon";
import { Viewport3D } from "../Viewport3D";
import { MainViewport2D } from "../main2d/MainViewport2D";
import { notificationImageItems, notificationPriority, notificationThumbnailUrl } from "../notifications/pipelinesNotifications";

type Props = {
  compositionName: string;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  viewSettings: ViewSettings;
  notificationRenderers: NotificationRenderer[];
  notifications: Notification[];
  activeNotificationId: string | null;
  notificationsLoading: boolean;
  onSelectNotification: (notificationId: string) => void;
  onLoadMoreNotifications: () => void;
  api: HostApi;
  updateElement: (elementId: string, patch: CompositionElementPatch) => void;
  onEditComposition: () => void;
  onOpenPipelines: () => void;
  onOpenSettings: () => void;
  onActivateComposition: (compositionId: string) => Promise<Composition>;
  onCreateComposition: (name: string) => Promise<Composition>;
  onRenameComposition: (compositionId: string, name: string) => Promise<Composition>;
  onDeleteComposition: (compositionId: string) => Promise<void>;
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
const NOTIFICATIONS_SHOW_LOW_STORAGE_KEY = "toposync.notifications_show_low.v1";

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

function loadNotificationsShowLow(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const raw = localStorage.getItem(NOTIFICATIONS_SHOW_LOW_STORAGE_KEY);
    return raw === "1";
  } catch {
    return false;
  }
}

export function MainScreen({
  compositionName,
  compositions,
  activeCompositionId,
  elements,
  elementTypesById,
  viewSettings,
  notificationRenderers,
  notifications,
  activeNotificationId,
  notificationsLoading,
  onSelectNotification,
  onLoadMoreNotifications,
  api,
  updateElement,
  onEditComposition,
  onOpenPipelines,
  onOpenSettings,
  onActivateComposition,
  onCreateComposition,
  onRenameComposition,
  onDeleteComposition,
}: Props): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [activeElementId, setActiveElementId] = useState<string | null>(null);
  const [imageModal, setImageModal] = useState<{ url: string; title: string; subtitle?: string } | null>(null);
  const [isNotificationDetailsOpen, setIsNotificationDetailsOpen] = useState(false);
  const [notificationImageIndex, setNotificationImageIndex] = useState(0);
  const [notificationsOpen, setNotificationsOpen] = useState(() => loadNotificationsOpen());
  const [showLowPriority, setShowLowPriority] = useState(() => loadNotificationsShowLow());
  const [renderMode, setRenderMode] = useState<"3d" | "2d">(() => {
    try {
      const saved = localStorage.getItem("toposync.render_mode.v1");
      return saved === "2d" ? "2d" : "3d";
    } catch {
      return "3d";
    }
  });
  const notificationScrollRef = useRef<HTMLDivElement | null>(null);
  const notificationSentinelRef = useRef<HTMLDivElement | null>(null);

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

  const activeNotificationImageMeta = useMemo(() => {
    if (!activeNotificationImage) return null;
    const parts: string[] = [
      t(`core.ui.notifications.details.image_source.${activeNotificationImage.source}`, {}, activeNotificationImage.source),
    ];
    if (typeof activeNotificationImage.confidence === "number") {
      parts.push(`${Math.round(activeNotificationImage.confidence * 100)}%`);
    }
    const tsLabel = formatTimestampMillis(locale, activeNotificationImage.storedTsMs);
    if (tsLabel) parts.push(tsLabel);
    return parts.join(" • ");
  }, [activeNotificationImage, locale, t]);

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
      localStorage.setItem("toposync.render_mode.v1", renderMode);
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
    try {
      localStorage.setItem(NOTIFICATIONS_SHOW_LOW_STORAGE_KEY, showLowPriority ? "1" : "0");
    } catch {
      // ignore
    }
  }, [showLowPriority]);

  useEffect(() => {
    if (activeNotification) return;
    setIsNotificationDetailsOpen(false);
    setNotificationImageIndex(0);
  }, [activeNotification]);

  useEffect(() => {
    setNotificationImageIndex(0);
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

  const lowPriorityHiddenCount = useMemo(() => {
    if (showLowPriority) return 0;
    return notifications.filter((n) => notificationPriority(n) === "low" && (!activeNotificationId || n.id !== activeNotificationId)).length;
  }, [activeNotificationId, notifications, showLowPriority]);

  const visibleNotifications = useMemo(() => {
    if (showLowPriority) return notifications;
    return notifications.filter((n) => {
      const prio = notificationPriority(n);
      if (prio !== "low") return true;
      return Boolean(activeNotificationId && n.id === activeNotificationId);
    });
  }, [activeNotificationId, notifications, showLowPriority]);

  return (
    <div className="screenRoot">
      {renderMode === "3d" ? (
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
        <MainViewport2D
          elements={elements}
          elementTypesById={elementTypesById}
          compositionId={activeCompositionId}
          onElementActivated={handleElementActivated}
        />
      )}

      <div className="overlayTopRight">
        <button className="chipButton" type="button" onClick={() => setIsRenderModalOpen(true)}>
          {t("core.ui.rendering")}: {renderMode === "3d" ? "3D" : "2D"}
        </button>
        <button className="chipButton" type="button" onClick={() => setIsCompositionModalOpen(true)}>
          {t("core.ui.composition")}: {compositionName}
        </button>
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
        <button
          className="iconButton iconButtonPrimary"
          type="button"
          aria-label={t("core.actions.edit")}
          title={t("core.actions.edit")}
          onClick={onEditComposition}
        >
          <Icon name="pen-to-square" />
        </button>
      </div>

      <div className={["overlayLeft", notificationsOpen ? "isOpen" : "isCollapsed"].filter(Boolean).join(" ")}>
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

                <button
                  className={["iconButton", showLowPriority ? "iconButtonPrimary" : ""].filter(Boolean).join(" ")}
                  type="button"
                  aria-label={
                    showLowPriority
                      ? t("core.ui.notifications.hide_low", {}, "Hide low priority")
                      : t("core.ui.notifications.show_low", {}, "Show low priority")
                  }
                  title={
                    showLowPriority
                      ? t("core.ui.notifications.hide_low", {}, "Hide low priority")
                      : t("core.ui.notifications.show_low", {}, "Show low priority")
                  }
                  onClick={() => setShowLowPriority((prev) => !prev)}
                >
                  <Icon name={showLowPriority ? "eye" : "eye-slash"} />
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

            <div className="railScroll" ref={notificationScrollRef}>
              {visibleNotifications.length === 0 ? (
                <div className="card">
                  <div className="cardBody">
                    {lowPriorityHiddenCount
                      ? t("core.ui.notifications.low_hidden", { count: lowPriorityHiddenCount }, "{{count}} low priority notifications hidden.")
                      : t("core.ui.notifications_empty")}
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
              {visibleNotifications.length > 0 ? (
                <span className="notificationsToggleBadge">{visibleNotifications.length}</span>
              ) : null}
            </button>
          </div>
        )}
      </div>

      {elements.length === 0 ? (
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

                  <img className="notificationGalleryImage" src={activeNotificationImage.url} alt="" />

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
