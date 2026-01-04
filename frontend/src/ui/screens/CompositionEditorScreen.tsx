import React, { useMemo, useState } from "react";

import type { CompositionElement, CompositionElementPatch, ElementType } from "@toposync/plugin-api";

import type { Composition, CompositionSummary } from "../../util/api";
import { i18n, resolveLocalizedString } from "../../util/i18n";

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
  const { locale, t } = i18n.useI18n();
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [editingElementId, setEditingElementId] = useState<string | null>(null);

  const elementTypes = useMemo(
    () =>
      Object.values(elementTypesById).sort((a, b) =>
        resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)),
      ),
    [elementTypesById, locale],
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
          {t("core.ui.rendering")}: 2D
        </button>
        <button className="chipButton" type="button" onClick={() => setIsCompositionModalOpen(true)}>
          {t("core.ui.composition")}: {compositionName}
        </button>
        <button className="primaryButton" type="button" onClick={onExit}>
          {t("core.actions.back")}
        </button>
      </div>

      <div className="overlayLeft">
        <div className="rail">
          <div className="railTitle">{t("core.ui.add")}</div>
          {elementTypes.length === 0 ? (
            <div className="card">
              <div className="cardBody">{t("core.ui.element_types_empty")}</div>
            </div>
          ) : (
            <div className="elementButtonGrid">
              {elementTypes.map((elementType) => (
                <button
                  className="elementTypeButton"
                  key={elementType.type}
                  type="button"
                  onClick={() => {
                    const id = addElement(elementType.type);
                    if (id) setEditingElementId(id);
                  }}
                >
                  <span>{resolveLocalizedString(elementType.name)}</span>
                  <span className="elementTypeButtonHint">+</span>
                </button>
              ))}
            </div>
          )}

          <div className="sectionDivider" />

          <div className="railTitle">{t("core.ui.layers")}</div>
          <div className="railScroll">
            {elements.length === 0 ? (
              <div className="card">
                <div className="cardBody">{t("core.ui.layers_empty")}</div>
              </div>
            ) : null}
            {elements.map((el) => {
              const type = elementTypesById[el.type];
              const typeName = type ? resolveLocalizedString(type.name) : el.type;
              const title = el.name || typeName || el.type;
              return (
                <div className="layerRow" key={el.id}>
                  <button className="layerMainButton" type="button" onClick={() => setEditingElementId(el.id)}>
                    <div className="layerMainTitle">{title}</div>
                    <div className="layerMainMeta">{typeName}</div>
                  </button>
                  <button className="layerDeleteButton" type="button" onClick={() => removeElement(el.id)}>
                    {t("core.actions.delete")}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <Modal
        open={isRenderModalOpen}
        title={t("core.ui.render_modal.title")}
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
        open={Boolean(editingElement)}
        title={editingElement ? `${t("core.actions.edit")}: ${editingElement.name}` : t("core.element_editor.title")}
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
                <div className="label">{t("core.element_editor.name")}</div>
                <input
                  className="input"
                  value={editingElement.name}
                  onChange={(e) => updateElement(editingElement.id, { name: e.target.value })}
                />
              </div>

              <div className="sectionDivider" />

              <div className="rowWrap">
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">{t("core.element_editor.pos_x")}</div>
                  <input
                    className="input"
                    type="number"
                    value={editingElement.position.x}
                    step={0.1}
                    onChange={(e) => updateElement(editingElement.id, { position: { x: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">{t("core.element_editor.pos_y")}</div>
                  <input
                    className="input"
                    type="number"
                    value={editingElement.position.y}
                    step={0.1}
                    onChange={(e) => updateElement(editingElement.id, { position: { y: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">{t("core.element_editor.pos_z")}</div>
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
                  <div className="label">{t("core.element_editor.rot_x")}</div>
                  <input
                    className="input"
                    type="number"
                    value={Math.round(degrees(editingElement.rotation.x) * 10) / 10}
                    step={5}
                    onChange={(e) => updateElement(editingElement.id, { rotation: { x: radians(Number(e.target.value)) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">{t("core.element_editor.rot_y")}</div>
                  <input
                    className="input"
                    type="number"
                    value={Math.round(degrees(editingElement.rotation.y) * 10) / 10}
                    step={5}
                    onChange={(e) => updateElement(editingElement.id, { rotation: { y: radians(Number(e.target.value)) } })}
                  />
                </div>
                <div className="field" style={{ flex: 1, minWidth: 140 }}>
                  <div className="label">{t("core.element_editor.rot_z")}</div>
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
                {t("core.element_editor.delete")}
              </button>
            </>
          )
        ) : null}
      </Modal>
    </div>
  );
}
