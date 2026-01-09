import React, { useEffect, useMemo, useRef, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostApi,
  Notification,
  NotificationRenderer,
  ViewSettings,
  WallHeightPreset,
} from "@toposync/plugin-api";

import type { Composition, CompositionSummary } from "../../util/api";
import { i18n } from "../../util/i18n";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { Icon } from "../Icon";
import { Viewport3D } from "../Viewport3D";

type Props = {
  compositionName: string;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  viewSettings: ViewSettings;
  onSetWallHeightPreset: (preset: WallHeightPreset) => void;
  onSetGhostWalls: (enabled: boolean) => void;
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

function formatTime(iso: string | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function MainScreen({
  compositionName,
  compositions,
  activeCompositionId,
  elements,
  elementTypesById,
  viewSettings,
  onSetWallHeightPreset,
  onSetGhostWalls,
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
  const { t } = i18n.useI18n();
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [isViewSettingsOpen, setIsViewSettingsOpen] = useState(false);
  const [activeElementId, setActiveElementId] = useState<string | null>(null);
  const [imageModal, setImageModal] = useState<{ url: string; title: string; subtitle?: string } | null>(null);
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

  return (
    <div className="screenRoot">
      <Viewport3D
        elements={elements}
        elementTypesById={elementTypesById}
        onElementActivated={(elementId, intent) => {
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
        }}
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

      <div className="overlayTopRight">
        <button className="chipButton" type="button" onClick={() => setIsRenderModalOpen(true)}>
          {t("core.ui.rendering")}: 3D
        </button>
        <button className="chipButton" type="button" onClick={() => setIsCompositionModalOpen(true)}>
          {t("core.ui.composition")}: {compositionName}
        </button>
        <button
          className="iconButton"
          type="button"
          aria-label={t("core.ui.view_settings.aria")}
          onClick={() => setIsViewSettingsOpen(true)}
        >
          <Icon name="sliders" />
        </button>
        <button
          className="iconButton"
          type="button"
          aria-label={t("core.ui.settings.aria")}
          onClick={onOpenSettings}
        >
          <Icon name="gear" />
        </button>
        <button className="primaryButton" type="button" onClick={onEditComposition}>
          {t("core.actions.edit")}
        </button>
      </div>

      <div className="overlayLeft">
        <div className="rail railNotifications">
          <div className="railTitle">{t("core.ui.notifications")}</div>
          <div className="railScroll" ref={notificationScrollRef}>
            {notifications.length === 0 ? (
              <div className="card">
                <div className="cardBody">{t("core.ui.notifications_empty")}</div>
              </div>
            ) : null}
            {notifications.map((n) => {
              const renderer = notificationRenderers.find((r) => r.type === n.type);
              const time = formatTime(n.createdAt);
              const isActive = Boolean(activeNotificationId && n.id === activeNotificationId);
              return (
                <button
                  className={["card", "cardButton", isActive ? "isActive" : ""].filter(Boolean).join(" ")}
                  type="button"
                  key={n.id}
                  onClick={() => onSelectNotification(n.id)}
                >
                  <div className="cardHeaderRow">
                    <div className="cardTitle">{n.title}</div>
                    {time ? <div className="cardMeta">{time}</div> : null}
                  </div>
                  <div className="cardBody">
                    {renderer ? (
                      renderer.render(n)
                    ) : (
                      <>
                        {n.description ? <div className="notificationText">{n.description}</div> : null}
                        {n.imageUrl ? (
                          <img className="notificationThumb" src={n.imageUrl} alt="" loading="lazy" />
                        ) : null}
                        {!n.description && !n.imageUrl ? <div className="notificationText">{JSON.stringify(n.payload)}</div> : null}
                      </>
                    )}
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
            className="choiceItem"
            role="button"
            tabIndex={0}
            onClick={() => setIsRenderModalOpen(false)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") setIsRenderModalOpen(false);
            }}
          >
            <div className="choiceTitle">{t("core.ui.render_modal.option_3d.title")}</div>
            <div className="choiceDesc">{t("core.ui.render_modal.option_3d.desc")}</div>
          </div>
        </div>
      </Modal>

      <Modal
        open={isViewSettingsOpen}
        title={t("core.ui.view_settings.title")}
        onClose={() => setIsViewSettingsOpen(false)}
      >
        <div className="modalSectionTitle">{t("core.ui.view_settings.wall_height")}</div>
        <div className="choiceList">
          {(
            [
              { id: "low", title: t("core.ui.wall_height.low"), desc: t("core.ui.wall_height.low_desc") },
              { id: "medium", title: t("core.ui.wall_height.medium"), desc: t("core.ui.wall_height.medium_desc") },
              { id: "high", title: t("core.ui.wall_height.high"), desc: t("core.ui.wall_height.high_desc") },
            ] as const
          ).map((opt) => {
            const selected = viewSettings.wallHeightPreset === opt.id;
            return (
              <div
                key={opt.id}
                className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                role="button"
                tabIndex={0}
                onClick={() => {
                  onSetWallHeightPreset(opt.id);
                  setIsViewSettingsOpen(false);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    onSetWallHeightPreset(opt.id);
                    setIsViewSettingsOpen(false);
                  }
                }}
              >
                <div className="choiceTitle">{opt.title}</div>
                <div className="choiceDesc">{opt.desc}</div>
              </div>
            );
          })}
        </div>

        <div className="modalSectionTitle">{t("core.ui.view_settings.interactivity")}</div>
        <div className="choiceList">
          {(() => {
            const selected = Boolean(viewSettings.ghostWalls);
            return (
              <div
                className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                role="button"
                tabIndex={0}
                onClick={() => onSetGhostWalls(!selected)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") onSetGhostWalls(!selected);
                }}
              >
                <div className="choiceTitle">{t("core.ui.view_settings.ghost_walls")}</div>
                <div className="choiceDesc">{t("core.ui.view_settings.ghost_walls_desc")}</div>
              </div>
            );
          })()}
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
