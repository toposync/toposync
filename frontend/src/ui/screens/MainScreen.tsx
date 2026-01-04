import React, { useMemo, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostApi,
  Notification,
  NotificationRenderer,
} from "@toposync/plugin-api";

import type { Composition, CompositionSummary } from "../../util/api";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { Viewport3D } from "../Viewport3D";

type Props = {
  compositionName: string;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  notificationRenderers: NotificationRenderer[];
  notifications: Notification[];
  api: HostApi;
  updateElement: (elementId: string, patch: CompositionElementPatch) => void;
  onEditComposition: () => void;
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
  notificationRenderers,
  notifications,
  api,
  updateElement,
  onEditComposition,
  onActivateComposition,
  onCreateComposition,
  onRenameComposition,
  onDeleteComposition,
}: Props): React.ReactElement {
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [activeElementId, setActiveElementId] = useState<string | null>(null);

  const activeElement = useMemo(
    () => (activeElementId ? elements.find((e) => e.id === activeElementId) ?? null : null),
    [activeElementId, elements],
  );
  const activeType = activeElement ? elementTypesById[activeElement.type] ?? null : null;

  const actionTitle = activeElement ? activeElement.name : "Ação";
  const isActionModalOpen = Boolean(activeElement);

  return (
    <div className="screenRoot">
      <Viewport3D elements={elements} elementTypesById={elementTypesById} onElementActivated={setActiveElementId} />

      <div className="overlayTopRight">
        <button className="chipButton" type="button" onClick={() => setIsRenderModalOpen(true)}>
          Renderização: 3D
        </button>
        <button className="chipButton" type="button" onClick={() => setIsCompositionModalOpen(true)}>
          Composição: {compositionName}
        </button>
        <button className="primaryButton" type="button" onClick={onEditComposition}>
          Editar
        </button>
      </div>

      <div className="overlayLeft">
        <div className="rail">
          <div className="railTitle">Notificações</div>
          <div className="railScroll">
            {notifications.length === 0 ? (
              <div className="card">
                <div className="cardBody">Nenhuma notificação por enquanto.</div>
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
            <div className="cardTitle">Nada configurado ainda</div>
            <div className="cardBody">Clique em “Editar” para adicionar elementos na composição.</div>
          </div>
        </div>
      ) : null}

      <Modal open={isRenderModalOpen} title="Renderização" onClose={() => setIsRenderModalOpen(false)}>
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
            <div className="choiceTitle">3D (ThreeJS)</div>
            <div className="choiceDesc">Modo atual. Em breve: 2D e outros modos.</div>
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
            <div className="cardBody">Sem ações disponíveis para este elemento.</div>
          )
        ) : null}
      </Modal>
    </div>
  );
}
