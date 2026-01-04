import React, { useMemo, useState } from "react";

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
  notificationRenderers: NotificationRenderer[];
  notifications: Notification[];
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
  notificationRenderers,
  notifications,
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

  const activeElement = useMemo(
    () => (activeElementId ? elements.find((e) => e.id === activeElementId) ?? null : null),
    [activeElementId, elements],
  );
  const activeType = activeElement ? elementTypesById[activeElement.type] ?? null : null;

  const actionTitle = activeElement ? activeElement.name : t("core.ui.action");
  const isActionModalOpen = Boolean(activeElement);

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
        <div className="rail">
          <div className="railTitle">{t("core.ui.notifications")}</div>
          <div className="railScroll">
            {notifications.length === 0 ? (
              <div className="card">
                <div className="cardBody">{t("core.ui.notifications_empty")}</div>
              </div>
            ) : null}
            {notifications.map((n) => {
              const renderer = notificationRenderers.find((r) => r.type === n.type);
              const time = formatTime(n.createdAt);
              return (
                <div className="card" key={n.id}>
                  <div className="cardHeaderRow">
                    <div className="cardTitle">{n.title}</div>
                    {time ? <div className="cardMeta">{time}</div> : null}
                  </div>
                  <div className="cardBody">{renderer ? renderer.render(n) : JSON.stringify(n.payload)}</div>
                </div>
              );
            })}
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
    </div>
  );
}
