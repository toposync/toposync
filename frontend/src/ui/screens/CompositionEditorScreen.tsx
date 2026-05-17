import React, { useCallback, useEffect, useMemo, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorFileDropEvent,
  EditorTool,
  EditorToolSession,
  ElementType,
  FileDropHandler,
  HostApi,
  PlanePoint,
} from "@toposync/plugin-api";

import type { Composition, CompositionSummary } from "../../util/api";
import { i18n, resolveLocalizedString } from "../../util/i18n";

import { Modal } from "../Modal";
import { CompositionSelectorModal } from "../CompositionSelectorModal";
import { Icon } from "../Icon";
import { Viewport2D } from "../Viewport2D";
import { drawMeasurementLine2D, MEASUREMENT_LINE_ELEMENT_TYPE_ID } from "../editor/measurementLineElementType";

type Props = {
  compositionName: string;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  api: HostApi;
  fileDropHandlers: FileDropHandler[];
  createElement: (typeId: string, init?: Partial<Omit<CompositionElement, "id" | "type">>) => string | null;
  editorTools: EditorTool[];
  updateElement: (elementId: string, patch: CompositionElementPatch) => void;
  reorderElements: (nextElements: CompositionElement[]) => void;
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

type LayerControl = {
  hidden?: boolean;
  locked?: boolean;
};

type LayerControlsState = {
  compositionId: string;
  byElementId: Record<string, LayerControl>;
};

type LayerGroupId = "background" | "ungrouped" | "walls" | "areas" | "measurements";
type DragInsertPosition = "before" | "after";

function degrees(rad: number): number {
  return (rad * 180) / Math.PI;
}

function radians(deg: number): number {
  return (deg * Math.PI) / 180;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return Boolean(v) && typeof v === "object" && !Array.isArray(v);
}

const LAYER_CONTROLS_STORAGE_KEY_PREFIX = "toposync.editor.layerControls.v1:";

function loadLayerControls(compositionId: string): Record<string, LayerControl> {
  try {
    const raw = localStorage.getItem(`${LAYER_CONTROLS_STORAGE_KEY_PREFIX}${compositionId}`);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) return {};

    const out: Record<string, LayerControl> = {};
    for (const [id, value] of Object.entries(parsed)) {
      if (!isRecord(value)) continue;
      const hidden = value.hidden === true;
      const locked = value.locked === true;
      if (hidden || locked) out[id] = { hidden, locked };
    }
    return out;
  } catch {
    return {};
  }
}

function saveLayerControls(compositionId: string, byElementId: Record<string, LayerControl>): void {
  try {
    const key = `${LAYER_CONTROLS_STORAGE_KEY_PREFIX}${compositionId}`;
    const cleaned: Record<string, LayerControl> = {};
    for (const [id, value] of Object.entries(byElementId)) {
      const hidden = value.hidden === true;
      const locked = value.locked === true;
      if (hidden || locked) cleaned[id] = { hidden, locked };
    }

    if (Object.keys(cleaned).length === 0) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, JSON.stringify(cleaned));
  } catch {
    // ignore
  }
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
const CORE_TOOL_MEASURE_LINE_ID = "core.measure_line";

type ToolGroupMeta = NonNullable<EditorTool["group"]>;
type ToolGroupView = {
  id: string;
  name: ToolGroupMeta["name"];
  order: number;
  tools: EditorTool[];
};

const TOOL_GROUP_BASIC: ToolGroupMeta = {
  id: "basic",
  name: { key: "core.ui.tools.group.basic", fallback: "Basic" },
  order: 0,
};

const TOOL_GROUP_REFERENCES: ToolGroupMeta = {
  id: "references",
  name: { key: "core.ui.tools.group.references", fallback: "References" },
  order: 10,
};

const TOOL_GROUP_STRUCTURE: ToolGroupMeta = {
  id: "structure",
  name: { key: "core.ui.tools.group.structure", fallback: "Structure" },
  order: 20,
};

const TOOL_GROUP_AREAS: ToolGroupMeta = {
  id: "areas",
  name: { key: "core.ui.tools.group.areas", fallback: "Areas" },
  order: 30,
};

const TOOL_GROUP_DEVICES: ToolGroupMeta = {
  id: "devices",
  name: { key: "core.ui.tools.group.devices", fallback: "Devices" },
  order: 40,
};

const TOOL_GROUP_OTHER: ToolGroupMeta = {
  id: "other",
  name: { key: "core.ui.tools.group.other", fallback: "Other" },
  order: 900,
};

const KNOWN_TOOL_GROUPS: Record<string, ToolGroupMeta> = {
  [TOOL_GROUP_BASIC.id]: TOOL_GROUP_BASIC,
  [TOOL_GROUP_REFERENCES.id]: TOOL_GROUP_REFERENCES,
  [TOOL_GROUP_STRUCTURE.id]: TOOL_GROUP_STRUCTURE,
  [TOOL_GROUP_AREAS.id]: TOOL_GROUP_AREAS,
  [TOOL_GROUP_DEVICES.id]: TOOL_GROUP_DEVICES,
  [TOOL_GROUP_OTHER.id]: TOOL_GROUP_OTHER,
};

function finiteOrder(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function CompositionEditorScreen({
  compositionName,
  compositions,
  activeCompositionId,
  elements,
  elementTypesById,
  api,
  fileDropHandlers,
  createElement,
  editorTools,
  updateElement,
  reorderElements,
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
	  const [isBackgroundOpen, setIsBackgroundOpen] = useState(true);
	  const [isWallsOpen, setIsWallsOpen] = useState(true);
	  const [isAreasOpen, setIsAreasOpen] = useState(true);
	  const [isMeasurementsOpen, setIsMeasurementsOpen] = useState(true);
	  const [draggingLayer, setDraggingLayer] = useState<{ elementId: string; groupId: LayerGroupId } | null>(null);
  const [dragOverLayer, setDragOverLayer] = useState<{
    elementId: string;
    groupId: LayerGroupId;
    position: DragInsertPosition;
  } | null>(null);

  const [layerControlsState, setLayerControlsState] = useState<LayerControlsState>(() => ({
    compositionId: activeCompositionId,
    byElementId: loadLayerControls(activeCompositionId),
  }));

  useEffect(() => {
    setLayerControlsState({
      compositionId: activeCompositionId,
      byElementId: loadLayerControls(activeCompositionId),
    });
  }, [activeCompositionId]);

  useEffect(() => {
    saveLayerControls(layerControlsState.compositionId, layerControlsState.byElementId);
  }, [layerControlsState]);

  useEffect(() => {
    setLayerControlsState((prev) => {
      if (prev.compositionId !== activeCompositionId) return prev;
      const existing = new Set(elements.map((e) => e.id));
      let changed = false;
      const next: Record<string, LayerControl> = {};
      for (const [id, value] of Object.entries(prev.byElementId)) {
        if (!existing.has(id)) {
          changed = true;
          continue;
        }
        if (value.hidden || value.locked) next[id] = value;
      }
      return changed ? { ...prev, byElementId: next } : prev;
    });
  }, [activeCompositionId, elements]);

  const hiddenElementIds = useMemo(
    () => Object.entries(layerControlsState.byElementId).filter(([, v]) => v.hidden).map(([id]) => id),
    [layerControlsState.byElementId],
  );

  const lockedElementIds = useMemo(
    () => Object.entries(layerControlsState.byElementId).filter(([, v]) => v.locked).map(([id]) => id),
    [layerControlsState.byElementId],
  );

  const toggleLayerHidden = useCallback(
    (elementId: string) => {
      setLayerControlsState((prev) => {
        if (prev.compositionId !== activeCompositionId) return prev;
        const current = prev.byElementId[elementId] ?? {};
        const nextHidden = current.hidden !== true;
        const nextEntry: LayerControl = { ...current, hidden: nextHidden };
        if (!nextEntry.hidden && !nextEntry.locked) {
          const { [elementId]: _, ...rest } = prev.byElementId;
          return { ...prev, byElementId: rest };
        }
        return { ...prev, byElementId: { ...prev.byElementId, [elementId]: nextEntry } };
      });
    },
    [activeCompositionId],
  );

  const toggleLayerLocked = useCallback(
    (elementId: string) => {
      setLayerControlsState((prev) => {
        if (prev.compositionId !== activeCompositionId) return prev;
        const current = prev.byElementId[elementId] ?? {};
        const nextLocked = current.locked !== true;
        const nextEntry: LayerControl = { ...current, locked: nextLocked };
        if (!nextEntry.hidden && !nextEntry.locked) {
          const { [elementId]: _, ...rest } = prev.byElementId;
          return { ...prev, byElementId: rest };
        }
        return { ...prev, byElementId: { ...prev.byElementId, [elementId]: nextEntry } };
      });
    },
    [activeCompositionId],
  );

  const onDropFiles = useCallback(
    (event: EditorFileDropEvent) => {
      if (fileDropHandlers.length === 0) return;

      const ctx = {
        i18n,
        api,
        compositionId: activeCompositionId,
        elements,
        createElement,
        openEditor: (elementId: string) => setEditingElementId(elementId),
      };

      for (const handler of fileDropHandlers) {
        try {
          if (handler.canHandle && !handler.canHandle(event)) continue;
          const out = handler.handle(ctx, event);
          if (out instanceof Promise) {
            out.catch((err) => console.error(`[fileDropHandler:${handler.id}]`, err));
            break;
          }
          if (out !== false) break;
        } catch (err) {
          console.error(`[fileDropHandler:${handler.id}]`, err);
        }
      }
    },
    [activeCompositionId, api, createElement, elements, fileDropHandlers],
  );

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
	    if (group === "measurements") {
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
        name: { key: "core.tools.navigate", fallback: "Move" },
        description: { key: "core.tools.navigate_desc", fallback: "Pan around the canvas." },
        icon: "hand",
        group: TOOL_GROUP_BASIC,
        order: 10,
        createSession: () => ({}),
      },
      {
        id: CORE_TOOL_SELECT_ID,
        name: { key: "core.tools.select", fallback: "Select" },
        description: { key: "core.tools.select_desc", fallback: "Select and move elements." },
        icon: "arrow-pointer",
        group: TOOL_GROUP_BASIC,
        order: 20,
        createSession: () => ({}),
      },
      {
        id: CORE_TOOL_MEASURE_LINE_ID,
        name: { key: "core.tools.measure_line", fallback: "Measure" },
        description: { key: "core.tools.measure_line_desc", fallback: "Measure a straight line." },
        icon: "ruler",
        group: TOOL_GROUP_BASIC,
        order: 30,
        createSession: (toolContext) => {
          let startPoint: PlanePoint | null = null;
          let currentPoint: PlanePoint | null = null;

          function reset() {
            startPoint = null;
            currentPoint = null;
          }

          function commit(endPoint: PlanePoint) {
            if (!startPoint) return;
            if (distance(startPoint, endPoint) < 0.05) {
              reset();
              return;
            }
            const center = { x: (startPoint.x + endPoint.x) / 2, z: (startPoint.z + endPoint.z) / 2 };
            toolContext.createElement(MEASUREMENT_LINE_ELEMENT_TYPE_ID, {
              name: "",
              position: { x: center.x, y: 0, z: center.z },
              props: { a: startPoint, b: endPoint },
            });
            reset();
          }

          return {
            onPointerEvent: (event) => {
              if (event.kind === "cancel") {
                reset();
                return;
              }
              if (event.kind === "move") {
                if (startPoint) currentPoint = event.world;
                return;
              }
              if (event.kind === "down") {
                if (event.button !== 0) return;
                startPoint = event.world;
                currentPoint = event.world;
                return;
              }
              if (event.kind === "up") {
                if (event.button !== 0) return;
                if (!startPoint) return;
                commit(event.world);
              }
            },
            onKeyDown: (event) => {
              if (event.key === "Escape") reset();
            },
            renderOverlay2D: ({ ctx: canvasContext, viewport }) => {
              if (!startPoint || !currentPoint) return;
              drawMeasurementLine2D({
                ctx: canvasContext,
                viewport,
                aWorld: startPoint,
                bWorld: currentPoint,
                dashed: true,
                showLabel: true,
              });
            },
            getCursor: () => "crosshair",
          };
        },
      },
    ];

    const extTools = [...editorTools];

    const placementTools: EditorTool[] = elementTypes
      .filter((elType) => elType.layerGroup !== "walls" && elType.layerGroup !== "areas")
      .filter((elType) => elType.placeable !== false)
      .map((elType) => ({
        id: `core.place:${elType.type}`,
        name: elType.name,
        description: elType.description,
        icon: "plus",
        group: TOOL_GROUP_OTHER,
        order: 0,
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

  const groupedTools = useMemo<ToolGroupView[]>(() => {
    const groupsById = new Map<string, ToolGroupView>();

    for (const tool of tools) {
      const rawGroup = tool.group ?? TOOL_GROUP_OTHER;
      const groupId = rawGroup.id?.trim() || TOOL_GROUP_OTHER.id;
      const fallbackGroup = KNOWN_TOOL_GROUPS[groupId];
      const groupOrder = finiteOrder(rawGroup.order, finiteOrder(fallbackGroup?.order, TOOL_GROUP_OTHER.order ?? 900));
      const groupName = rawGroup.name ?? fallbackGroup?.name ?? groupId;
      const existing = groupsById.get(groupId);

      if (existing) {
        existing.order = Math.min(existing.order, groupOrder);
        existing.tools.push(tool);
        continue;
      }

      groupsById.set(groupId, { id: groupId, name: groupName, order: groupOrder, tools: [tool] });
    }

    const compareTools = (a: EditorTool, b: EditorTool) => {
      const orderDelta = finiteOrder(a.order, 0) - finiteOrder(b.order, 0);
      if (orderDelta !== 0) return orderDelta;
      return resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name), locale);
    };

    const groups = [...groupsById.values()];
    for (const group of groups) group.tools.sort(compareTools);
    groups.sort((a, b) => {
      const orderDelta = a.order - b.order;
      if (orderDelta !== 0) return orderDelta;
      return resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name), locale);
    });

    return groups;
  }, [locale, tools]);

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
      getElements: () => elements,
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
	      if (group === "background") setIsBackgroundOpen(true);
	      if (group === "walls") setIsWallsOpen(true);
	      if (group === "areas") setIsAreasOpen(true);
	      if (group === "measurements") setIsMeasurementsOpen(true);
	    }
	  }, [elements, elementTypesById, selectedElementIds]);

  const layerGroupForElement = useCallback(
    (el: CompositionElement): LayerGroupId => {
	      const group = elementTypesById[el.type]?.layerGroup ?? "";
	      if (group === "background") return "background";
	      if (group === "walls") return "walls";
	      if (group === "areas") return "areas";
	      if (group === "measurements") return "measurements";
	      return "ungrouped";
	    },
	    [elementTypesById],
	  );

  const reorderLayersInGroup = useCallback(
    (args: { groupId: LayerGroupId; draggedId: string; targetId: string; position: DragInsertPosition }) => {
      const groupIndices: number[] = [];
      const byId = new Map<string, CompositionElement>();
      for (let idx = 0; idx < elements.length; idx += 1) {
        const el = elements[idx];
        if (layerGroupForElement(el) !== args.groupId) continue;
        groupIndices.push(idx);
        byId.set(el.id, el);
      }

      if (groupIndices.length < 2) return;
      if (!byId.has(args.draggedId) || !byId.has(args.targetId) || args.draggedId === args.targetId) return;

      const displayOrderIds = [...groupIndices].sort((a, b) => b - a).map((idx) => elements[idx].id);
      const nextDisplayOrderIds = displayOrderIds.filter((id) => id !== args.draggedId);
      const targetIndex = nextDisplayOrderIds.indexOf(args.targetId);
      if (targetIndex < 0) return;

      const insertIndex = args.position === "before" ? targetIndex : targetIndex + 1;
      nextDisplayOrderIds.splice(insertIndex, 0, args.draggedId);

      const arrayOrderIds = [...nextDisplayOrderIds].reverse();
      const orderedIndices = [...groupIndices].sort((a, b) => a - b);

      const nextElements = elements.slice();
      for (let i = 0; i < orderedIndices.length; i += 1) {
        const el = byId.get(arrayOrderIds[i]);
        if (!el) return;
        nextElements[orderedIndices[i]] = el;
      }

      reorderElements(nextElements);
    },
    [elements, layerGroupForElement, reorderElements],
  );

  const beginLayerDrag = useCallback((event: React.DragEvent, elementId: string, groupId: LayerGroupId) => {
    setDraggingLayer({ elementId, groupId });
    setDragOverLayer(null);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", elementId);
  }, []);

  const endLayerDrag = useCallback(() => {
    setDraggingLayer(null);
    setDragOverLayer(null);
  }, []);

  const updateDragOverLayer = useCallback(
    (event: React.DragEvent<HTMLElement>, targetId: string, groupId: LayerGroupId) => {
      const dragging = draggingLayer;
      if (!dragging) return;
      if (dragging.groupId !== groupId) return;
      if (dragging.elementId === targetId) return;

      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      const rect = event.currentTarget.getBoundingClientRect();
      const position: DragInsertPosition = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
      setDragOverLayer({ elementId: targetId, groupId, position });
    },
    [draggingLayer],
  );

  const handleLayerDrop = useCallback(
    (event: React.DragEvent<HTMLElement>, targetId: string, groupId: LayerGroupId) => {
      const dragging = draggingLayer;
      if (!dragging) return;
      if (dragging.groupId !== groupId) return;
      if (dragging.elementId === targetId) return;

      event.preventDefault();
      const rect = event.currentTarget.getBoundingClientRect();
      const position: DragInsertPosition = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
      reorderLayersInGroup({ groupId, draggedId: dragging.elementId, targetId, position });
      setDraggingLayer(null);
      setDragOverLayer(null);
    },
    [draggingLayer, reorderLayersInGroup],
  );

  const layerRowClassName = useCallback(
    (base: string[], elementId: string, groupId: LayerGroupId): string => {
      const cls = [...base];
      if (draggingLayer?.elementId === elementId && draggingLayer.groupId === groupId) cls.push("isDragSource");
      if (dragOverLayer?.elementId === elementId && dragOverLayer.groupId === groupId) {
        cls.push(dragOverLayer.position === "before" ? "isDropBefore" : "isDropAfter");
      }
      return cls.join(" ");
    },
    [dragOverLayer, draggingLayer],
  );

	  const layerGroups = useMemo(() => {
	    const ungrouped: Array<{ el: CompositionElement; idx: number }> = [];
	    const background: Array<{ el: CompositionElement; idx: number }> = [];
	    const walls: Array<{ el: CompositionElement; idx: number }> = [];
	    const areas: Array<{ el: CompositionElement; idx: number }> = [];
	    const measurements: Array<{ el: CompositionElement; idx: number }> = [];

	    elements.forEach((el, idx) => {
	      const group = elementTypesById[el.type]?.layerGroup ?? "";
	      if (group === "background") background.push({ el, idx });
	      else if (group === "walls") walls.push({ el, idx });
	      else if (group === "areas") areas.push({ el, idx });
	      else if (group === "measurements") measurements.push({ el, idx });
	      else ungrouped.push({ el, idx });
	    });

	    ungrouped.sort((a, b) => b.idx - a.idx);
	    background.sort((a, b) => b.idx - a.idx);
	    walls.sort((a, b) => b.idx - a.idx);
	    areas.sort((a, b) => b.idx - a.idx);
	    measurements.sort((a, b) => b.idx - a.idx);

	    return { ungrouped, background, walls, areas, measurements };
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
        onDropFiles={onDropFiles}
        hiddenElementIds={hiddenElementIds}
        lockedElementIds={lockedElementIds}
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
            <div className="railScroll railScrollTools">
              <div className="toolGroups">
                {groupedTools.map((group) => (
                  <div className="toolGroup" key={group.id}>
                    <div className="toolGroupTitle">{resolveLocalizedString(group.name)}</div>
                    <div className="elementButtonGrid">
                      {group.tools.map((tool) => {
                        const isSelected = selectedToolId === tool.id;
                        const toolName = resolveLocalizedString(tool.name);
                        const toolDescription = resolveLocalizedString(tool.description);
                        const toolTitle = toolDescription ? `${toolName}\n${toolDescription}` : toolName;
                        return (
                          <button
                            className={["elementTypeButton", isSelected ? "isSelected" : ""].join(" ")}
                            key={tool.id}
                            type="button"
                            title={toolTitle}
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
                              <span>{toolName}</span>
                            </span>
                            <span className="elementTypeButtonHint">{isSelected ? <Icon name="check" /> : null}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
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

            {layerGroups.background.length > 0 ? (
              <div className="layerGroup">
                <button className="layerGroupHeader" type="button" onClick={() => setIsBackgroundOpen((v) => !v)}>
                  <span className="layerGroupTitle">
                    <Icon
                      name={isBackgroundOpen ? "chevron-down" : "chevron-right"}
                      className="layerGroupChevron"
                    />
                    <span>{t("core.ui.layers_group_background")}</span>
                  </span>
                  <span className="layerGroupCount">{layerGroups.background.length}</span>
                </button>
                {isBackgroundOpen ? (
                  <div className="layerGroupItems">
                    {layerGroups.background.map(({ el }) => {
                      const type = elementTypesById[el.type];
                      const typeName = type ? resolveLocalizedString(type.name) : el.type;
                      const title = el.name || typeName || el.type;
                      const selected = selectedElementIds.includes(el.id);
                      const control = layerControlsState.byElementId[el.id] ?? {};
                      const hidden = control.hidden === true;
                      const locked = control.locked === true;
                      const measurement = measurementFor(el);
                      return (
                        <div
                          className={layerRowClassName(["layerRow", "layerRowGrouped", hidden ? "isHidden" : ""], el.id, "background")}
                          key={el.id}
                          onDragOver={(e) => updateDragOverLayer(e, el.id, "background")}
                          onDrop={(e) => handleLayerDrop(e, el.id, "background")}
                        >
                          <button
                            className="layerDragHandle"
                            type="button"
                            title={t("core.ui.layers.reorder")}
                            aria-label={t("core.ui.layers.reorder")}
                            draggable
                            onDragStart={(e) => beginLayerDrag(e, el.id, "background")}
                            onDragEnd={endLayerDrag}
                          >
                            <Icon name="grip-vertical" />
                          </button>
                          <button
                            className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
                            type="button"
                            onClick={(e) => {
                              if (e.metaKey || e.ctrlKey) {
                                setSelectedElementIds((prev) =>
                                  prev.includes(el.id) ? prev.filter((id) => id !== el.id) : [...prev, el.id],
                                );
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
                          <button
                            className={["layerToggleButton", locked ? "isActive" : ""].join(" ")}
                            type="button"
                            title={locked ? t("core.ui.layers.unlock") : t("core.ui.layers.lock")}
                            onClick={() => toggleLayerLocked(el.id)}
                          >
                            <Icon name={locked ? "lock" : "lock-open"} />
                          </button>
                          <button
                            className={["layerToggleButton", hidden ? "isActive" : ""].join(" ")}
                            type="button"
                            title={hidden ? t("core.ui.layers.show") : t("core.ui.layers.hide")}
                            onClick={() => toggleLayerHidden(el.id)}
                          >
                            <Icon name={hidden ? "eye-slash" : "eye"} />
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

            {layerGroups.ungrouped.map(({ el }) => {
              const type = elementTypesById[el.type];
              const typeName = type ? resolveLocalizedString(type.name) : el.type;
              const title = el.name || typeName || el.type;
              const selected = selectedElementIds.includes(el.id);
              const control = layerControlsState.byElementId[el.id] ?? {};
              const hidden = control.hidden === true;
              const locked = control.locked === true;
              const measurement = measurementFor(el);
              return (
                <div
                  className={layerRowClassName(["layerRow", hidden ? "isHidden" : ""], el.id, "ungrouped")}
                  key={el.id}
                  onDragOver={(e) => updateDragOverLayer(e, el.id, "ungrouped")}
                  onDrop={(e) => handleLayerDrop(e, el.id, "ungrouped")}
                >
                  <button
                    className="layerDragHandle"
                    type="button"
                    title={t("core.ui.layers.reorder")}
                    aria-label={t("core.ui.layers.reorder")}
                    draggable
                    onDragStart={(e) => beginLayerDrag(e, el.id, "ungrouped")}
                    onDragEnd={endLayerDrag}
                  >
                    <Icon name="grip-vertical" />
                  </button>
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
                  <button
                    className={["layerToggleButton", locked ? "isActive" : ""].join(" ")}
                    type="button"
                    title={locked ? t("core.ui.layers.unlock") : t("core.ui.layers.lock")}
                    onClick={() => toggleLayerLocked(el.id)}
                  >
                    <Icon name={locked ? "lock" : "lock-open"} />
                  </button>
                  <button
                    className={["layerToggleButton", hidden ? "isActive" : ""].join(" ")}
                    type="button"
                    title={hidden ? t("core.ui.layers.show") : t("core.ui.layers.hide")}
                    onClick={() => toggleLayerHidden(el.id)}
                  >
                    <Icon name={hidden ? "eye-slash" : "eye"} />
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
                      const control = layerControlsState.byElementId[el.id] ?? {};
	                      const hidden = control.hidden === true;
	                      const locked = control.locked === true;
	                      const measurement = measurementFor(el);
	                      return (
	                        <div
	                          className={layerRowClassName(["layerRow", "layerRowGrouped", hidden ? "isHidden" : ""], el.id, "walls")}
	                          key={el.id}
	                          onDragOver={(e) => updateDragOverLayer(e, el.id, "walls")}
	                          onDrop={(e) => handleLayerDrop(e, el.id, "walls")}
	                        >
	                          <button
	                            className="layerDragHandle"
	                            type="button"
	                            title={t("core.ui.layers.reorder")}
	                            aria-label={t("core.ui.layers.reorder")}
	                            draggable
	                            onDragStart={(e) => beginLayerDrag(e, el.id, "walls")}
	                            onDragEnd={endLayerDrag}
	                          >
	                            <Icon name="grip-vertical" />
	                          </button>
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
                          <button
                            className={["layerToggleButton", locked ? "isActive" : ""].join(" ")}
                            type="button"
                            title={locked ? t("core.ui.layers.unlock") : t("core.ui.layers.lock")}
                            onClick={() => toggleLayerLocked(el.id)}
                          >
                            <Icon name={locked ? "lock" : "lock-open"} />
                          </button>
                          <button
                            className={["layerToggleButton", hidden ? "isActive" : ""].join(" ")}
                            type="button"
                            title={hidden ? t("core.ui.layers.show") : t("core.ui.layers.hide")}
                            onClick={() => toggleLayerHidden(el.id)}
                          >
                            <Icon name={hidden ? "eye-slash" : "eye"} />
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

	            {layerGroups.measurements.length > 0 ? (
	              <div className="layerGroup">
	                <button
	                  className="layerGroupHeader"
	                  type="button"
	                  onClick={() => setIsMeasurementsOpen((v) => !v)}
	                >
	                  <span className="layerGroupTitle">
	                    <Icon
	                      name={isMeasurementsOpen ? "chevron-down" : "chevron-right"}
	                      className="layerGroupChevron"
	                    />
	                    <span>{t("core.ui.layers_group_measurements")}</span>
	                  </span>
	                  <span className="layerGroupCount">{layerGroups.measurements.length}</span>
	                </button>
	                {isMeasurementsOpen ? (
	                  <div className="layerGroupItems">
	                    {layerGroups.measurements.map(({ el }) => {
	                      const type = elementTypesById[el.type];
	                      const typeName = type ? resolveLocalizedString(type.name) : el.type;
	                      const title = el.name || typeName || el.type;
	                      const selected = selectedElementIds.includes(el.id);
	                      const control = layerControlsState.byElementId[el.id] ?? {};
	                      const hidden = control.hidden === true;
	                      const locked = control.locked === true;
	                      const measurement = measurementFor(el);
	                      return (
	                        <div
	                          className={layerRowClassName(
	                            ["layerRow", "layerRowGrouped", hidden ? "isHidden" : ""],
	                            el.id,
	                            "measurements",
	                          )}
	                          key={el.id}
	                          onDragOver={(e) => updateDragOverLayer(e, el.id, "measurements")}
	                          onDrop={(e) => handleLayerDrop(e, el.id, "measurements")}
	                        >
	                          <button
	                            className="layerDragHandle"
	                            type="button"
	                            title={t("core.ui.layers.reorder")}
	                            aria-label={t("core.ui.layers.reorder")}
	                            draggable
	                            onDragStart={(e) => beginLayerDrag(e, el.id, "measurements")}
	                            onDragEnd={endLayerDrag}
	                          >
	                            <Icon name="grip-vertical" />
	                          </button>
	                          <button
	                            className={["layerMainButton", selected ? "isSelected" : ""].join(" ")}
	                            type="button"
	                            onClick={(e) => {
	                              if (e.metaKey || e.ctrlKey) {
	                                setSelectedElementIds((prev) =>
	                                  prev.includes(el.id) ? prev.filter((id) => id !== el.id) : [...prev, el.id],
	                                );
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
	                          <button
	                            className={["layerToggleButton", locked ? "isActive" : ""].join(" ")}
	                            type="button"
	                            title={locked ? t("core.ui.layers.unlock") : t("core.ui.layers.lock")}
	                            onClick={() => toggleLayerLocked(el.id)}
	                          >
	                            <Icon name={locked ? "lock" : "lock-open"} />
	                          </button>
	                          <button
	                            className={["layerToggleButton", hidden ? "isActive" : ""].join(" ")}
	                            type="button"
	                            title={hidden ? t("core.ui.layers.show") : t("core.ui.layers.hide")}
	                            onClick={() => toggleLayerHidden(el.id)}
	                          >
	                            <Icon name={hidden ? "eye-slash" : "eye"} />
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
                      const control = layerControlsState.byElementId[el.id] ?? {};
	                      const hidden = control.hidden === true;
	                      const locked = control.locked === true;
	                      const measurement = measurementFor(el);
	                      return (
	                        <div
	                          className={layerRowClassName(["layerRow", "layerRowGrouped", hidden ? "isHidden" : ""], el.id, "areas")}
	                          key={el.id}
	                          onDragOver={(e) => updateDragOverLayer(e, el.id, "areas")}
	                          onDrop={(e) => handleLayerDrop(e, el.id, "areas")}
	                        >
	                          <button
	                            className="layerDragHandle"
	                            type="button"
	                            title={t("core.ui.layers.reorder")}
	                            aria-label={t("core.ui.layers.reorder")}
	                            draggable
	                            onDragStart={(e) => beginLayerDrag(e, el.id, "areas")}
	                            onDragEnd={endLayerDrag}
	                          >
	                            <Icon name="grip-vertical" />
	                          </button>
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
                          <button
                            className={["layerToggleButton", locked ? "isActive" : ""].join(" ")}
                            type="button"
                            title={locked ? t("core.ui.layers.unlock") : t("core.ui.layers.lock")}
                            onClick={() => toggleLayerLocked(el.id)}
                          >
                            <Icon name={locked ? "lock" : "lock-open"} />
                          </button>
                          <button
                            className={["layerToggleButton", hidden ? "isActive" : ""].join(" ")}
                            type="button"
                            title={hidden ? t("core.ui.layers.show") : t("core.ui.layers.hide")}
                            onClick={() => toggleLayerHidden(el.id)}
                          >
                            <Icon name={hidden ? "eye-slash" : "eye"} />
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
