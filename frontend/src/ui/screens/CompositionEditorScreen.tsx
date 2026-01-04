import React, { useEffect, useMemo, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  EditorToolSession,
  ElementType,
} from "@toposync/plugin-api";

import type { Composition, CompositionSummary } from "../../util/api";
import { i18n, resolveLocalizedString } from "../../util/i18n";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { Icon } from "../Icon";
import { Viewport2D } from "../Viewport2D";

type Props = {
  compositionName: string;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  createElement: (typeId: string, init?: Partial<Omit<CompositionElement, "id" | "type">>) => string | null;
  editorTools: EditorTool[];
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
  createElement,
  editorTools,
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
  const [selectedElementId, setSelectedElementId] = useState<string | null>(null);
  const [selectedToolId, setSelectedToolId] = useState<string | null>(null);
  const [activeToolSession, setActiveToolSession] = useState<EditorToolSession | null>(null);
  const [isWallsOpen, setIsWallsOpen] = useState(true);
  const [isAreasOpen, setIsAreasOpen] = useState(true);

  const elementTypes = useMemo(
    () =>
      Object.values(elementTypesById).sort((a, b) =>
        resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)),
      ),
    [elementTypesById, locale],
  );

  const tools = useMemo(() => {
    const extTools = [...editorTools].sort((a, b) =>
      resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)),
    );

    const placementTools: EditorTool[] = elementTypes
      .filter((elType) => elType.layerGroup !== "walls" && elType.layerGroup !== "areas")
      .map((elType) => ({
        id: `core.place:${elType.type}`,
        name: elType.name,
        description: elType.description,
        icon: "plus",
        createSession: ({ createElement: create, openEditor }) => ({
          onPointerEvent: (evt) => {
            if (evt.kind !== "down") return;
            if (evt.button !== 0) return;
            const id = create(elType.type, {
              position: { x: evt.world.x, y: 0, z: evt.world.z },
            });
            if (id) openEditor(id);
          },
        }),
      }));

    return [...extTools, ...placementTools];
  }, [editorTools, elementTypes, locale]);

  const toolsById = useMemo(() => {
    const out: Record<string, EditorTool> = {};
    for (const tool of tools) out[tool.id] = tool;
    return out;
  }, [tools]);

  useEffect(() => {
    if (selectedToolId && !toolsById[selectedToolId]) {
      setSelectedToolId(null);
      setActiveToolSession(null);
      return;
    }

    const tool = selectedToolId ? toolsById[selectedToolId] : null;
    if (!tool) {
      setActiveToolSession(null);
      return;
    }

    const session = tool.createSession({
      i18n,
      createElement,
      updateElement,
      removeElement,
      openEditor: (elementId) => setEditingElementId(elementId),
      closeEditor: () => setEditingElementId(null),
    });
    setActiveToolSession(session);

    return () => session.dispose?.();
  }, [createElement, removeElement, selectedToolId, toolsById, updateElement]);

  const editingElement = useMemo(
    () => (editingElementId ? elements.find((e) => e.id === editingElementId) ?? null : null),
    [editingElementId, elements],
  );
  const editingType = editingElement ? elementTypesById[editingElement.type] ?? null : null;

  useEffect(() => {
    if (!selectedElementId) return;
    const el = elements.find((e) => e.id === selectedElementId);
    if (!el) {
      setSelectedElementId(null);
      return;
    }
    const group = elementTypesById[el.type]?.layerGroup ?? "";
    if (group === "walls") setIsWallsOpen(true);
    if (group === "areas") setIsAreasOpen(true);
  }, [elements, elementTypesById, selectedElementId]);

  useEffect(() => {
    if (!editingElementId) return;
    setSelectedElementId(editingElementId);
  }, [editingElementId]);

  const layerGroups = useMemo(() => {
    const ungrouped: Array<{ el: CompositionElement; idx: number }> = [];
    const walls: Array<{ el: CompositionElement; idx: number }> = [];
    const areas: Array<{ el: CompositionElement; idx: number }> = [];

    elements.forEach((el, idx) => {
      const group = elementTypesById[el.type]?.layerGroup ?? "";
      if (group === "walls") walls.push({ el, idx });
      else if (group === "areas") areas.push({ el, idx });
      else ungrouped.push({ el, idx });
    });

    walls.sort((a, b) => b.idx - a.idx);
    areas.sort((a, b) => b.idx - a.idx);

    return { ungrouped, walls, areas };
  }, [elements, elementTypesById]);

  const editingTitle = useMemo(() => {
    if (!editingElement) return t("core.element_editor.title");
    const typeName = editingType ? resolveLocalizedString(editingType.name) : editingElement.type;
    const title = editingElement.name || typeName || editingElement.type;
    return `${t("core.actions.edit")}: ${title}`;
  }, [editingElement, editingType, t]);

  return (
    <div className="screenRoot">
      <Viewport2D
        elements={elements}
        elementTypesById={elementTypesById}
        activeToolSession={activeToolSession}
        selectedElementId={selectedElementId}
        onSelectElement={setSelectedElementId}
        onOpenEditor={(id) => {
          setSelectedElementId(id);
          setEditingElementId(id);
        }}
        updateElement={updateElement}
      />

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
          <div className="railTitle">{t("core.ui.tools")}</div>
          {tools.length === 0 ? (
            <div className="card">
              <div className="cardBody">{t("core.ui.element_types_empty")}</div>
            </div>
          ) : (
            <div className="elementButtonGrid">
              {tools.map((tool) => {
                const isSelected = selectedToolId === tool.id;
                return (
                <button
                  className={["elementTypeButton", isSelected ? "isSelected" : ""].join(" ")}
                  key={tool.id}
                  type="button"
                  onClick={() => {
                    setSelectedToolId((prev) => (prev === tool.id ? null : tool.id));
                  }}
                >
                  <span className="toolLabel">
                    {tool.icon ? <Icon name={tool.icon} className="toolIcon" /> : null}
                    <span>{resolveLocalizedString(tool.name)}</span>
                  </span>
                  <span className="elementTypeButtonHint">{isSelected ? <Icon name="check" /> : null}</span>
                </button>
                );
              })}
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

            {layerGroups.ungrouped.map(({ el }) => {
              const type = elementTypesById[el.type];
              const typeName = type ? resolveLocalizedString(type.name) : el.type;
              const title = el.name || typeName || el.type;
              const selected = selectedElementId === el.id;
              return (
                <div className="layerRow" key={el.id}>
                  <button
                    className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
                    type="button"
                    onClick={() => setSelectedElementId(el.id)}
                    onDoubleClick={() => setEditingElementId(el.id)}
                  >
                    <div className="layerMainTitle">{title}</div>
                    <div className="layerMainMeta">{typeName}</div>
                  </button>
                  <button className="layerDeleteButton" type="button" onClick={() => removeElement(el.id)}>
                    {t("core.actions.delete")}
                  </button>
                </div>
              );
            })}

            {layerGroups.walls.length > 0 ? (
              <div className="layerGroup">
                <button className="layerGroupHeader" type="button" onClick={() => setIsWallsOpen((v) => !v)}>
                  <span className="layerGroupTitle">
                    <Icon name={isWallsOpen ? "chevron-down" : "chevron-right"} className="layerGroupChevron" />
                    <span>{t("core.ui.layers_group_walls")}</span>
                  </span>
                  <span className="layerGroupCount">{layerGroups.walls.length}</span>
                </button>
                {isWallsOpen ? (
                  <div className="layerGroupItems">
                    {layerGroups.walls.map(({ el }) => {
                      const type = elementTypesById[el.type];
                      const typeName = type ? resolveLocalizedString(type.name) : el.type;
                      const title = el.name || typeName || el.type;
                      const selected = selectedElementId === el.id;
                      return (
                        <div className="layerRow layerRowGrouped" key={el.id}>
                          <button
                            className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
                            type="button"
                            onClick={() => setSelectedElementId(el.id)}
                            onDoubleClick={() => setEditingElementId(el.id)}
                          >
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
                ) : null}
              </div>
            ) : null}

            {layerGroups.areas.length > 0 ? (
              <div className="layerGroup">
                <button className="layerGroupHeader" type="button" onClick={() => setIsAreasOpen((v) => !v)}>
                  <span className="layerGroupTitle">
                    <Icon name={isAreasOpen ? "chevron-down" : "chevron-right"} className="layerGroupChevron" />
                    <span>{t("core.ui.layers_group_areas")}</span>
                  </span>
                  <span className="layerGroupCount">{layerGroups.areas.length}</span>
                </button>
                {isAreasOpen ? (
                  <div className="layerGroupItems">
                    {layerGroups.areas.map(({ el }) => {
                      const type = elementTypesById[el.type];
                      const typeName = type ? resolveLocalizedString(type.name) : el.type;
                      const title = el.name || typeName || el.type;
                      const selected = selectedElementId === el.id;
                      return (
                        <div className="layerRow layerRowGrouped" key={el.id}>
                          <button
                            className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
                            type="button"
                            onClick={() => setSelectedElementId(el.id)}
                            onDoubleClick={() => setEditingElementId(el.id)}
                          >
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
                ) : null}
              </div>
            ) : null}
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
        title={editingTitle}
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
