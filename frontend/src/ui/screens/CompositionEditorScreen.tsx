import React, { useCallback, useEffect, useMemo, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  EditorToolSession,
  ElementType,
  PlanePoint,
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
  onBeginUndoGroup: () => void;
  onEndUndoGroup: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onExit: () => void;
  onOpenSettings: () => void;
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

function isRecord(v: unknown): v is Record<string, unknown> {
  return Boolean(v) && typeof v === "object" && !Array.isArray(v);
}

function readPlanePoint(v: unknown): PlanePoint | null {
  if (!isRecord(v)) return null;
  const x = v.x;
  const z = v.z;
  if (typeof x !== "number" || typeof z !== "number") return null;
  if (!Number.isFinite(x) || !Number.isFinite(z)) return null;
  return { x, z };
}

function readVertices(v: unknown): PlanePoint[] {
  if (!Array.isArray(v)) return [];
  const out: PlanePoint[] = [];
  for (const item of v) {
    const p = readPlanePoint(item);
    if (p) out.push(p);
  }
  return out;
}

function distance(a: PlanePoint, b: PlanePoint): number {
  return Math.hypot(a.x - b.x, a.z - b.z);
}

function polygonArea(vertices: PlanePoint[]): number {
  if (vertices.length < 3) return 0;
  let sum = 0;
  for (let i = 0; i < vertices.length; i++) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    sum += a.x * b.z - b.x * a.z;
  }
  return Math.abs(sum) / 2;
}

const CORE_TOOL_NAVIGATE_ID = "core.navigate";
const CORE_TOOL_SELECT_ID = "core.select";

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
  onBeginUndoGroup,
  onEndUndoGroup,
  onUndo,
  onRedo,
  onExit,
  onOpenSettings,
  onActivateComposition,
  onCreateComposition,
  onRenameComposition,
  onDeleteComposition,
}: Props): React.ReactElement {
  const { locale, t } = i18n.useI18n();
  const [isRenderModalOpen, setIsRenderModalOpen] = useState(false);
  const [isCompositionModalOpen, setIsCompositionModalOpen] = useState(false);
  const [editingElementId, setEditingElementId] = useState<string | null>(null);
  const [selectedElementIds, setSelectedElementIds] = useState<string[]>([]);
  const [selectedToolId, setSelectedToolId] = useState<string>(CORE_TOOL_NAVIGATE_ID);
  const [activeToolSession, setActiveToolSession] = useState<EditorToolSession | null>(null);
  const [isWallsOpen, setIsWallsOpen] = useState(true);
  const [isAreasOpen, setIsAreasOpen] = useState(true);

  const numberFmt = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  function measurementFor(el: CompositionElement): string | null {
    const group = elementTypesById[el.type]?.layerGroup ?? "";
    if (group === "walls") {
      const a = readPlanePoint(el.props.a);
      const b = readPlanePoint(el.props.b);
      if (!a || !b) return null;
      return `${numberFmt.format(distance(a, b))} m`;
    }
    if (group === "areas") {
      const vertices = readVertices(el.props.vertices);
      if (vertices.length < 3) return null;
      return `${numberFmt.format(polygonArea(vertices))} m²`;
    }
    return null;
  }

  const elementTypes = useMemo(
    () =>
      Object.values(elementTypesById).sort((a, b) =>
        resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)),
      ),
    [elementTypesById, locale],
  );

  const tools = useMemo(() => {
    const coreTools: EditorTool[] = [
      {
        id: CORE_TOOL_NAVIGATE_ID,
        name: { key: "core.tools.navigate", fallback: "Navigate" },
        description: { key: "core.tools.navigate_desc", fallback: "Pan around the canvas." },
        icon: "hand",
        createSession: () => ({}),
      },
      {
        id: CORE_TOOL_SELECT_ID,
        name: { key: "core.tools.select", fallback: "Select" },
        description: { key: "core.tools.select_desc", fallback: "Select and move elements." },
        icon: "arrow-pointer",
        createSession: () => ({}),
      },
    ];

    const extTools = [...editorTools].sort((a, b) =>
      resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)),
    );

    const placementTools: EditorTool[] = elementTypes
      .filter((elType) => elType.layerGroup !== "walls" && elType.layerGroup !== "areas")
      .filter((elType) => elType.placeable !== false)
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

    return [...coreTools, ...extTools, ...placementTools];
  }, [editorTools, elementTypes, locale]);

  const toolsById = useMemo(() => {
    const out: Record<string, EditorTool> = {};
    for (const tool of tools) out[tool.id] = tool;
    return out;
  }, [tools]);

  useEffect(() => {
    if (selectedToolId && !toolsById[selectedToolId]) {
      setSelectedToolId(CORE_TOOL_NAVIGATE_ID);
      setActiveToolSession(null);
      return;
    }

    if (selectedToolId === CORE_TOOL_NAVIGATE_ID || selectedToolId === CORE_TOOL_SELECT_ID) {
      setActiveToolSession(null);
      return;
    }

    const tool = toolsById[selectedToolId] ?? null;
    if (!tool) {
      setSelectedToolId(CORE_TOOL_NAVIGATE_ID);
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
    setSelectedElementIds((prev) => {
      if (prev.length === 0) return prev;
      const existing = new Set(elements.map((e) => e.id));
      const next = prev.filter((id) => existing.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [elements]);

  useEffect(() => {
    if (!editingElementId) return;
    setSelectedElementIds([editingElementId]);
  }, [editingElementId]);

  useEffect(() => {
    if (selectedElementIds.length === 0) return;
    const byId = new Map(elements.map((e) => [e.id, e]));
    for (const id of selectedElementIds) {
      const el = byId.get(id);
      if (!el) continue;
      const group = elementTypesById[el.type]?.layerGroup ?? "";
      if (group === "walls") setIsWallsOpen(true);
      if (group === "areas") setIsAreasOpen(true);
    }
  }, [elements, elementTypesById, selectedElementIds]);

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

  const duplicateElements = useCallback(
    (source: CompositionElement[]): string[] => {
      const ids: string[] = [];
      for (const el of source) {
        const id = createElement(el.type, {
          name: el.name,
          position: { ...el.position },
          rotation: { ...el.rotation },
          props: { ...el.props },
        });
        if (!id) return [];
        ids.push(id);
      }
      return ids;
    },
    [createElement],
  );

  return (
    <div className="screenRoot">
      <Viewport2D
        elements={elements}
        elementTypesById={elementTypesById}
        activeToolSession={activeToolSession}
        interactionMode={selectedToolId === CORE_TOOL_NAVIGATE_ID ? "navigate" : "select"}
        selectedElementIds={selectedElementIds}
        onSelectElements={setSelectedElementIds}
        onOpenEditor={(id) => {
          setSelectedElementIds([id]);
          setEditingElementId(id);
        }}
        updateElement={updateElement}
        removeElement={removeElement}
        duplicateElements={duplicateElements}
        onBeginUndoGroup={onBeginUndoGroup}
        onEndUndoGroup={onEndUndoGroup}
        onUndo={onUndo}
        onRedo={onRedo}
      />

      <div className="overlayTopRight">
        <button className="chipButton" type="button" onClick={() => setIsRenderModalOpen(true)}>
          {t("core.ui.rendering")}: 2D
        </button>
        <button className="chipButton" type="button" onClick={() => setIsCompositionModalOpen(true)}>
          {t("core.ui.composition")}: {compositionName}
        </button>
        <button className="iconButton" type="button" aria-label={t("core.ui.settings.aria")} onClick={onOpenSettings}>
          <Icon name="gear" />
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
                      setSelectedToolId((prev) => {
                        if (tool.id === CORE_TOOL_NAVIGATE_ID) return CORE_TOOL_NAVIGATE_ID;
                        if (tool.id === CORE_TOOL_SELECT_ID) return CORE_TOOL_SELECT_ID;
                        return prev === tool.id ? CORE_TOOL_NAVIGATE_ID : tool.id;
                      });
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
              const selected = selectedElementIds.includes(el.id);
              const measurement = measurementFor(el);
              return (
                <div className="layerRow" key={el.id}>
                  <button
                    className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
                    type="button"
                    onClick={(e) => {
                      if (e.metaKey || e.ctrlKey) {
                        setSelectedElementIds((prev) => (prev.includes(el.id) ? prev.filter((id) => id !== el.id) : [...prev, el.id]));
                        return;
                      }
                      setSelectedElementIds([el.id]);
                    }}
                    onDoubleClick={() => {
                      setSelectedElementIds([el.id]);
                      setEditingElementId(el.id);
                    }}
                  >
                    <div className="layerMainTitle">{title}</div>
                    <div className="layerMainMeta">
                      {typeName}
                      {measurement ? ` • ${measurement}` : ""}
                    </div>
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
                      const selected = selectedElementIds.includes(el.id);
                      const measurement = measurementFor(el);
                      return (
                        <div className="layerRow layerRowGrouped" key={el.id}>
                          <button
                            className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
                            type="button"
                            onClick={(e) => {
                              if (e.metaKey || e.ctrlKey) {
                                setSelectedElementIds((prev) => (prev.includes(el.id) ? prev.filter((id) => id !== el.id) : [...prev, el.id]));
                                return;
                              }
                              setSelectedElementIds([el.id]);
                            }}
                            onDoubleClick={() => {
                              setSelectedElementIds([el.id]);
                              setEditingElementId(el.id);
                            }}
                          >
                            <div className="layerMainTitle">{title}</div>
                            <div className="layerMainMeta">
                              {typeName}
                              {measurement ? ` • ${measurement}` : ""}
                            </div>
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
                      const selected = selectedElementIds.includes(el.id);
                      const measurement = measurementFor(el);
                      return (
                        <div className="layerRow layerRowGrouped" key={el.id}>
                          <button
                            className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
                            type="button"
                            onClick={(e) => {
                              if (e.metaKey || e.ctrlKey) {
                                setSelectedElementIds((prev) => (prev.includes(el.id) ? prev.filter((id) => id !== el.id) : [...prev, el.id]));
                                return;
                              }
                              setSelectedElementIds([el.id]);
                            }}
                            onDoubleClick={() => {
                              setSelectedElementIds([el.id]);
                              setEditingElementId(el.id);
                            }}
                          >
                            <div className="layerMainTitle">{title}</div>
                            <div className="layerMainMeta">
                              {typeName}
                              {measurement ? ` • ${measurement}` : ""}
                            </div>
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
