import React, { useMemo, useState } from "react";

import type { CompositionElement, CompositionElementPatch, ElementType } from "@toposync/plugin-api";

import type { Composition, CompositionSummary } from "../../util/api";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { Viewport2D } from "../Viewport2D";

type Props = {
  compositionName: string;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  addElement: (typeId: string) => string | null;
  updateElement: (elementId: string, patch: CompositionElementPatch) => void;
  removeElement: (elementId: string) => void;
  onExit: () => void;
  onActivateComposition: (compositionId: string) => Promise<Composition>;
  onCreateComposition: (name: string) => Promise<Composition>;
  onRenameComposition: (compositionId: string, name: string) => Promise<Composition>;
  onDeleteComposition: (compositionId: string) => Promise<void>;
};

function degrees(rad: number): number {
  return (rad * 180) / Math.PI;
}

function radians(deg: number): number {
  return (deg * Math.PI) / 180;
}

export function CompositionEditorScreen({
  compositionName,
  compositions,
  activeCompositionId,
  elements,
  elementTypesById,
  addElement,
  updateElement,
  removeElement,
  onExit,
  onActivateComposition,
  onCreateComposition,
  onRenameComposition,
  onDeleteComposition,
}: Props): React.ReactElement {
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [editingElementId, setEditingElementId] = useState<string | null>(null);

  const elementTypes = useMemo(
    () => Object.values(elementTypesById).sort((a, b) => a.name.localeCompare(b.name)),
    [elementTypesById],
  );

  const editingElement = useMemo(
    () => (editingElementId ? elements.find((e) => e.id === editingElementId) ?? null : null),
    [editingElementId, elements],
  );
  const editingType = editingElement ? elementTypesById[editingElement.type] ?? null : null;

  return (
    <div className="screenRoot">
      <Viewport2D elements={elements} />

      <div className="overlayTopRight">
        <button className="chipButton" type="button" onClick={() => setIsRenderModalOpen(true)}>
          Renderização: 2D
        </button>
        <button className="chipButton" type="button" onClick={() => setIsCompositionModalOpen(true)}>
          Composição: {compositionName}
        </button>
        <button className="primaryButton" type="button" onClick={onExit}>
          Voltar
        </button>
      </div>

      <div className="overlayLeft">
        <div className="rail">
          <div className="railTitle">Adicionar</div>
          {elementTypes.length === 0 ? (
            <div className="card">
              <div className="cardBody">Nenhuma extensão registrou elementos ainda.</div>
            </div>
          ) : (
            <div className="elementButtonGrid">
              {elementTypes.map((t) => (
                <button
                  className="elementTypeButton"
                  key={t.type}
                  type="button"
                  onClick={() => {
                    const id = addElement(t.type);
                    if (id) setEditingElementId(id);
                  }}
                >
                  <span>{t.name}</span>
                  <span className="elementTypeButtonHint">+</span>
                </button>
              ))}
            </div>
          )}

          <div className="sectionDivider" />

          <div className="railTitle">Camadas</div>
          <div className="railScroll">
            {elements.length === 0 ? (
              <div className="card">
                <div className="cardBody">Nenhum elemento adicionado ainda.</div>
              </div>
            ) : null}
            {elements.map((el) => {
              const type = elementTypesById[el.type];
              const title = el.name || type?.name || el.type;
              return (
                <div className="layerRow" key={el.id}>
                  <button className="layerMainButton" type="button" onClick={() => setEditingElementId(el.id)}>
                    <div className="layerMainTitle">{title}</div>
                    <div className="layerMainMeta">{type ? type.name : el.type}</div>
                  </button>
                  <button className="layerDeleteButton" type="button" onClick={() => removeElement(el.id)}>
                    Excluir
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <Modal
        open={isRenderModalOpen}
        title="Renderização"
        onClose={() => {
          setIsRenderModalOpen(false);
        }}
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
            <div className="choiceTitle">2D (Canvas)</div>
            <div className="choiceDesc">Modo atual de edição. Em breve: 3D e ferramentas de desenho.</div>
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
        open={Boolean(editingElement)}
        title={editingElement ? `Editar: ${editingElement.name}` : "Editar elemento"}
        onClose={() => setEditingElementId(null)}
      >
        {editingElement ? (
          editingType?.renderEditorModal ? (
            editingType.renderEditorModal({
              element: editingElement,
              update: (patch) => updateElement(editingElement.id, patch),
              remove: () => {
                removeElement(editingElement.id);
                setEditingElementId(null);
              },
              close: () => setEditingElementId(null),
            })
          ) : (
            <>
              <div className="field">
                <div className="label">Nome</div>
                <input
                  className="input"
                  value={editingElement.name}
                  onChange={(e) => updateElement(editingElement.id, { name: e.target.value })}
                />
              </div>

              <div className="sectionDivider" />

              <div className="rowWrap">
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">Posição X</div>
                  <input
                    className="input"
                    type="number"
                    value={editingElement.position.x}
                    step={0.1}
                    onChange={(e) => updateElement(editingElement.id, { position: { x: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">Posição Y</div>
                  <input
                    className="input"
                    type="number"
                    value={editingElement.position.y}
                    step={0.1}
                    onChange={(e) => updateElement(editingElement.id, { position: { y: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">Posição Z</div>
                  <input
                    className="input"
                    type="number"
                    value={editingElement.position.z}
                    step={0.1}
                    onChange={(e) => updateElement(editingElement.id, { position: { z: Number(e.target.value) } })}
                  />
                </div>
              </div>

              <div className="rowWrap">
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">Rotação X (graus)</div>
                  <input
                    className="input"
                    type="number"
                    value={Math.round(degrees(editingElement.rotation.x) * 10) / 10}
                    step={5}
                    onChange={(e) => updateElement(editingElement.id, { rotation: { x: radians(Number(e.target.value)) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">Rotação Y (graus)</div>
                  <input
                    className="input"
                    type="number"
                    value={Math.round(degrees(editingElement.rotation.y) * 10) / 10}
                    step={5}
                    onChange={(e) => updateElement(editingElement.id, { rotation: { y: radians(Number(e.target.value)) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">Rotação Z (graus)</div>
                  <input
                    className="input"
                    type="number"
                    value={Math.round(degrees(editingElement.rotation.z) * 10) / 10}
                    step={5}
                    onChange={(e) => updateElement(editingElement.id, { rotation: { z: radians(Number(e.target.value)) } })}
                  />
                </div>
              </div>

              <div className="sectionDivider" />
              <button
                className="dangerButton"
                type="button"
                onClick={() => {
                  removeElement(editingElement.id);
                  setEditingElementId(null);
                }}
              >
                Excluir elemento
              </button>
            </>
          )
        ) : null}
      </Modal>
    </div>
  );
}
