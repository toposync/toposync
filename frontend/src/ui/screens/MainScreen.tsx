import React, { useEffect, useMemo, useRef, useState } from "react";

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
  onOpenSettings: () => void;
  onActivateComposition: (compositionId: string) => Promise<Composition>;
  onCreateComposition: (name: string) => Promise<Composition>;
  onRenameComposition: (compositionId: string, name: string) => Promise<Composition>;
  onDeleteComposition: (compositionId: string) => Promise<void>;
};

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  return {};
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

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

function formatCameraTrackingTitle(
  t: (key: string, params?: Record<string, unknown>, fallback?: string) => string,
  notification: Notification,
): string | null {
  if (notification.type !== "cameras.tracking") return null;
  const payload = asRecord(notification.payload);
  const rawLabel = asString(payload.label).trim();
  if (!rawLabel) return null;
  const key = `ext.cameras.yolo.${rawLabel.toLowerCase().replace(/\\s+/g, "_")}`;
  const label = t(key, {}, rawLabel);
  return t("ext.cameras.notification.object_detected", { label }, `Object detected: ${label}`);
}

const NOTIFICATIONS_OPEN_STORAGE_KEY = "toposync.notifications_open.v1";

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
  const [notificationsOpen, setNotificationsOpen] = useState(() => loadNotificationsOpen());
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

            <div className="railScroll" ref={notificationScrollRef}>
              {notifications.length === 0 ? (
                <div className="card">
                  <div className="cardBody">{t("core.ui.notifications_empty")}</div>
                </div>
              ) : null}
              {notifications.map((n) => {
                const renderer = notificationRenderers.find((r) => r.type === n.type);
                const time = formatDateTimeShort(locale, n.updatedAt ?? n.createdAt);
                const title = formatCameraTrackingTitle(t, n) ?? n.title;
                const isActive = Boolean(activeNotificationId && n.id === activeNotificationId);
                return (
                  <button
                    className={["card", "cardButton", "notificationCard", isActive ? "isActive" : ""].filter(Boolean).join(" ")}
                    type="button"
                    key={n.id}
                    onClick={() => {
                      onSelectNotification(n.id);
                      if (shouldAutoCloseNotificationsAfterSelect()) setNotificationsOpen(false);
                    }}
                  >
                    <div className="notificationCardGrid">
                      <div className="notificationCardMain">
                        <div className="notificationCardHeader">
                          <div className="notificationCardTitle">{title}</div>
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

                      {n.imageUrl ? (
                        <img className="notificationCardThumb" src={n.imageUrl} alt="" loading="lazy" draggable={false} />
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
              {notifications.length > 0 ? <span className="notificationsToggleBadge">{notifications.length}</span> : null}
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
              borderRadius: 14,
              border: "1px solid rgba(255,255,255,0.12)",
              background: "rgba(0,0,0,0.18)",
              marginTop: imageModal.subtitle ? 12 : 0,
              display: "block",
            }}
          />
        ) : null}
      </Modal>
    </div>
  );
}
