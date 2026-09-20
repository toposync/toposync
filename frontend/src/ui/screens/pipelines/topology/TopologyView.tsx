import type React from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PipelineOperatorPanel } from "@toposync/plugin-api";
import { applyNodeChanges, Background, Controls, MiniMap, ReactFlow, useUpdateNodeInternals } from "@xyflow/react";
import type {
  Connection,
  EdgeChange,
  EdgeTypes,
  NodeChange,
  NodeTypes,
  OnNodeDrag,
  ReactFlowInstance,
  XYPosition,
} from "@xyflow/react";

import "@xyflow/react/dist/style.css";
import "./topology.css";

import type { CamerasIndexResponse, GraphRuntimeInfo, PipelineAlert, PipelineOperatorDefinition } from "../../../../util/api";
import { i18n } from "../../../../util/i18n";
import { prettyOperatorDescription, prettyOperatorName } from "../utils";
import type { SelectOption, TelemetryFieldInspectorRequest } from "../types";
import { useCameraContexts } from "../editor/useCameraContexts";
import {
  addTopologyGraphNodeContextual,
  connectTopologyGraphEdge,
  deleteTopologyGraphEdge,
  deleteTopologyGraphNode,
  insertTopologyGraphNodeOnEdge,
  updateTopologyGraphEdgePolicy,
  updateTopologyGraphNodeConfig,
  updateTopologyGraphNodePosition,
  updateTopologyGraphNodePositions,
  type TopologyAddNodeContext,
  type TopologyEdgePolicyPatch,
  type TopologyGraphEditResult,
} from "./topologyGraph";
import { layoutTopologyNodes } from "./topologyLayout";
import { buildTopologyModel } from "./topologyModel";
import { TopologyEdgeComponent } from "./TopologyEdge";
import { TopologyInspector } from "./TopologyInspector";
import { TopologyNodeComponent } from "./TopologyNode";
import type { TopologyEdge, TopologyNode, TopologyRuntimeStatus, TopologySelection } from "./topologyTypes";

const NODE_TYPES: NodeTypes = { topologyNode: TopologyNodeComponent };
const EDGE_TYPES: EdgeTypes = { topologyEdge: TopologyEdgeComponent };
const NODE_WIDTH = 232;
const NODE_HEIGHT = 112;
const EDGE_INSERT_THRESHOLD = 72;

type Props = {
  pipelineName: string;
  graph: unknown;
  graphText: string;
  operatorsById: Record<string, PipelineOperatorDefinition>;
  camerasIndex: CamerasIndexResponse;
  processingServerId: string;
  onOpenProcessingServers?: () => void;
  operatorPanels?: Record<string, PipelineOperatorPanel>;
  onOpenTelemetryField?: (request: TelemetryFieldInspectorRequest) => void;
  alerts: PipelineAlert[];
  runtimeInfo: GraphRuntimeInfo | null;
  runtimeStatus: TopologyRuntimeStatus;
  editable?: boolean;
  dirty?: boolean;
  validationLoading?: boolean;
  validationError?: string | null;
  validationSuccessAt?: number | null;
  onChangeGraph?: (graph: Record<string, unknown>) => void;
  onValidate?: () => void;
  onDiscard?: () => void;
};

type TopologyDeleteGraphItem = (graph: unknown, id: string) => TopologyGraphEditResult;

function minimapStrokeColor(node: TopologyNode): string {
  if (node.data.pressureState === "error") return "#EF4444";
  if (node.data.pressureState === "critical") return "#F97316";
  if (node.data.pressureState === "warning") return "#F59E0B";
  return "rgba(226, 232, 240, 0.72)";
}

function operatorSortKey(operator: PipelineOperatorDefinition): string {
  const group = String(operator.ui?.pipeline_group ?? "");
  const order = Number(operator.ui?.pipeline_order ?? 9999);
  return `${group}:${order.toString().padStart(4, "0")}:${operator.id}`;
}

function mergeNodePositions(modelNodes: TopologyNode[], canvasNodes: TopologyNode[]): TopologyNode[] {
  if (canvasNodes.length === 0) return modelNodes;
  const positions = new Map(canvasNodes.map((node) => [node.id, node.position]));
  return modelNodes.map((node) => {
    const position = positions.get(node.id);
    return position ? { ...node, position } : node;
  });
}

function distanceToSegment(point: XYPosition, start: XYPosition, end: XYPosition): number {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  if (dx === 0 && dy === 0) return Math.hypot(point.x - start.x, point.y - start.y);
  const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(point.x - (start.x + t * dx), point.y - (start.y + t * dy));
}

function editResultMessage(result: Extract<TopologyGraphEditResult, { ok: false }>, t: ReturnType<typeof i18n.useI18n>["t"]): string {
  return result.messageKey ? t(result.messageKey, result.messageParams ?? {}, result.message) : result.message;
}

function TopologyInternalsUpdater({ nodes }: { nodes: TopologyNode[] }): null {
  const updateNodeInternals = useUpdateNodeInternals();
  const nodePortsKey = useMemo(
    () => nodes.map((node) => `${node.id}:${node.data.inputPorts.join(",")}:${node.data.outputPorts.join(",")}`).join("|"),
    [nodes],
  );

  useLayoutEffect(() => {
    if (!nodes.length) return;
    const nodeIds = nodes.map((node) => node.id);
    updateNodeInternals(nodeIds);
    const frame = window.requestAnimationFrame(() => updateNodeInternals(nodeIds));
    return () => window.cancelAnimationFrame(frame);
  }, [nodePortsKey, updateNodeInternals]);

  return null;
}

export function TopologyView({
  pipelineName,
  graph,
  graphText,
  operatorsById,
  camerasIndex,
  processingServerId,
  onOpenProcessingServers,
  operatorPanels = {},
  onOpenTelemetryField,
  alerts,
  runtimeInfo,
  runtimeStatus,
  editable = false,
  dirty = false,
  validationLoading = false,
  validationError = null,
  validationSuccessAt = null,
  onChangeGraph,
  onValidate,
  onDiscard,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [selection, setSelection] = useState<TopologySelection>({ kind: "summary" });
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [operatorQuery, setOperatorQuery] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance<TopologyNode, TopologyEdge> | null>(null);
  const [canvasNodes, setCanvasNodes] = useState<TopologyNode[]>([]);
  const [pendingFocusNodeId, setPendingFocusNodeId] = useState<string | null>(null);
  const [undoGraphStack, setUndoGraphStack] = useState<Record<string, unknown>[]>([]);
  const [fullscreenActive, setFullscreenActive] = useState(false);
  const [insertEdgeId, setInsertEdgeId] = useState<string | null>(null);
  const graphPaneRef = useRef<HTMLDivElement | null>(null);
  const latestGraphRef = useRef<unknown>(graph);
  const undoGraphStackRef = useRef<Record<string, unknown>[]>([]);
  const built = useMemo(
    () => buildTopologyModel({ graph, operatorsById, alerts, runtimeInfo }),
    [graph, operatorsById, alerts, runtimeInfo],
  );
  const interactiveCameraId = useMemo(() => {
    if (!built.ok) return "";
    const sourceNode = built.model.nodes.find((node) => node.data.operatorId === "camera.source");
    return String(sourceNode?.data.config?.camera_id ?? "").trim();
  }, [built]);
  const cameraSelectOptions = useMemo<SelectOption[]>(() => {
    const cameras = Array.isArray(camerasIndex.cameras) ? camerasIndex.cameras : [];
    return cameras
      .map((camera) => {
        const name = String(camera.name || "").trim();
        const id = String(camera.id || "").trim();
        return { value: id, label: name ? `${name} (${id})` : id };
      })
      .filter((option) => option.value.length > 0)
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [camerasIndex]);
  const cameraSelectOptionById = useMemo(() => {
    const map = new Map<string, SelectOption>();
    for (const option of cameraSelectOptions) map.set(option.value, option);
    return map;
  }, [cameraSelectOptions]);
  const { activeCameraContexts, activeCameraContextsError, cameraAreaOptions } = useCameraContexts(interactiveCameraId);
  const canEdit = Boolean(editable && built.ok && onChangeGraph);
  const operatorOptions = useMemo(
    () => Object.values(operatorsById).sort((left, right) => operatorSortKey(left).localeCompare(operatorSortKey(right))),
    [operatorsById],
  );
  const filteredOperatorOptions = useMemo(() => {
    const query = operatorQuery.trim().toLowerCase();
    return operatorOptions
      .filter((operator) => {
        if (!query) return true;
        return `${operator.id} ${prettyOperatorName(operator.id)} ${prettyOperatorDescription(operator)}`.toLowerCase().includes(query);
      })
      .slice(0, 80);
  }, [operatorOptions, operatorQuery]);
  const graphIdentityKey = built.ok ? `${pipelineName}:${built.model.summary.graphUid}` : `${pipelineName}:unsupported`;
  const sourceNodes = built.ok && canEdit ? mergeNodePositions(built.model.nodes, canvasNodes) : built.ok ? built.model.nodes : [];
  const nodesById = useMemo(() => new Map(sourceNodes.map((node) => [node.id, node])), [sourceNodes]);

  const edgeInsertPosition = useCallback(
    (edgeId: string): XYPosition | undefined => {
      if (!built.ok) return undefined;
      const edge = built.model.edges.find((item) => item.id === edgeId);
      const source = edge ? nodesById.get(edge.source) : null;
      const target = edge ? nodesById.get(edge.target) : null;
      if (!source || !target) return undefined;
      return {
        x: Math.round((source.position.x + NODE_WIDTH + target.position.x) / 2 - NODE_WIDTH / 2),
        y: Math.round((source.position.y + target.position.y) / 2),
      };
    },
    [built, nodesById],
  );

  const findInsertEdge = useCallback(
    (point: XYPosition, ignoreNodeId?: string): TopologyEdge | null => {
      if (!built.ok) return null;
      let selected: { edge: TopologyEdge; distance: number } | null = null;
      for (const edge of built.model.edges) {
        if (edge.source === ignoreNodeId || edge.target === ignoreNodeId) continue;
        const source = nodesById.get(edge.source);
        const target = nodesById.get(edge.target);
        if (!source || !target) continue;
        const distance = distanceToSegment(
          point,
          { x: source.position.x + NODE_WIDTH, y: source.position.y + NODE_HEIGHT / 2 },
          { x: target.position.x, y: target.position.y + NODE_HEIGHT / 2 },
        );
        if (distance <= EDGE_INSERT_THRESHOLD && (!selected || distance < selected.distance)) {
          selected = { edge, distance };
        }
      }
      return selected?.edge ?? null;
    },
    [built, nodesById],
  );

  useEffect(() => {
    setSelection({ kind: "summary" });
    undoGraphStackRef.current = [];
    setUndoGraphStack([]);
    setCanvasNodes(built.ok ? built.model.nodes : []);
    setInsertEdgeId(null);
  }, [graphIdentityKey]);

  useEffect(() => {
    if (dirty) return;
    undoGraphStackRef.current = [];
    setUndoGraphStack([]);
    setCanvasNodes(built.ok ? built.model.nodes : []);
  }, [dirty]);

  useEffect(() => {
    latestGraphRef.current = graph;
  }, [graph]);

  const commitGraphResult = useCallback(
    (result: TopologyGraphEditResult) => {
      if (!result.ok) {
        setActionError(editResultMessage(result, t));
        return;
      }
      setActionError(null);
      if (latestGraphRef.current && latestGraphRef.current !== result.graph) {
        const nextUndoStack = [...undoGraphStackRef.current.slice(-9), latestGraphRef.current as Record<string, unknown>];
        undoGraphStackRef.current = nextUndoStack;
        setUndoGraphStack(nextUndoStack);
      }
      latestGraphRef.current = result.graph;
      onChangeGraph?.(result.graph);
      if (result.nodeId) {
        setSelection({ kind: "node", id: result.nodeId });
        setPendingFocusNodeId(result.nodeId);
      } else if (result.edgeId) {
        setSelection({ kind: "edge", id: result.edgeId });
      }
    },
    [onChangeGraph, t],
  );

  const undoLastEdit = useCallback(() => {
    if (!canEdit) return;
    const previousGraph = undoGraphStackRef.current.at(-1);
    if (!previousGraph) return;
    const nextUndoStack = undoGraphStackRef.current.slice(0, -1);
    undoGraphStackRef.current = nextUndoStack;
    latestGraphRef.current = previousGraph;
    onChangeGraph?.(previousGraph);
    setSelection({ kind: "summary" });
    setActionError(null);
    setUndoGraphStack(nextUndoStack);
  }, [canEdit, onChangeGraph]);

  const fitTopologyView = useCallback(() => {
    flowInstance?.fitView({ padding: 0.24, duration: 180 });
  }, [flowInstance]);

  const arrangeTopology = useCallback(() => {
    if (!canEdit || !built.ok) return;
    const arrangedNodes = layoutTopologyNodes(built.model.nodes, built.model.edges);
    const positions = Object.fromEntries(arrangedNodes.map((node) => [node.id, node.position]));
    const currentPositions = new Map(sourceNodes.map((node) => [node.id, node.position]));
    const changed = arrangedNodes.some((node) => {
      const current = currentPositions.get(node.id);
      return !current || Math.round(current.x) !== Math.round(node.position.x) || Math.round(current.y) !== Math.round(node.position.y);
    });
    setCanvasNodes(arrangedNodes);
    if (!changed) {
      window.requestAnimationFrame(fitTopologyView);
      return;
    }
    commitGraphResult(updateTopologyGraphNodePositions(latestGraphRef.current, positions));
    window.requestAnimationFrame(fitTopologyView);
  }, [built, canEdit, commitGraphResult, fitTopologyView, sourceNodes]);

  const toggleFullscreen = useCallback(async () => {
    const element = graphPaneRef.current;
    if (!element) return;
    try {
      if (document.fullscreenElement === element) {
        await document.exitFullscreen();
        return;
      }
      if (document.fullscreenElement) await document.exitFullscreen();
      if (document.fullscreenEnabled && typeof element.requestFullscreen === "function") {
        await element.requestFullscreen();
        return;
      }
    } catch {
      // Fall back to a useful canvas action when the browser blocks fullscreen.
    }
    fitTopologyView();
  }, [fitTopologyView]);

  const addOperator = useCallback(
    (operator: PipelineOperatorDefinition, position?: XYPosition, edgeId?: string | null) => {
      if (!canEdit) return;
      let context: TopologyAddNodeContext = { kind: "none" };
      if (edgeId) context = { kind: "edge", edgeId };
      else if (edgeId !== null && selection.kind === "edge") context = { kind: "edge", edgeId: selection.id };
      else if (edgeId !== null && selection.kind === "node") context = { kind: "node", nodeId: selection.id };
      let insertPosition = position;
      if (!insertPosition && context.kind === "edge") insertPosition = edgeInsertPosition(context.edgeId);
      if (!insertPosition && context.kind === "node") {
        const node = nodesById.get(context.nodeId);
        insertPosition = node ? { x: node.position.x + NODE_WIDTH + 140, y: node.position.y } : undefined;
      }
      commitGraphResult(
        addTopologyGraphNodeContextual(
          latestGraphRef.current,
          operator.id,
          operator,
          operatorsById,
          context,
          insertPosition,
        ),
      );
      setPaletteOpen(false);
      setOperatorQuery("");
      setInsertEdgeId(null);
    },
    [canEdit, commitGraphResult, edgeInsertPosition, nodesById, operatorsById, selection],
  );

  const handleNodesChange = useCallback(
    (changes: NodeChange<TopologyNode>[]) => {
      for (const change of changes) {
        if (change.type !== "select") continue;
        setSelection((previous) => {
          if (change.selected) return { kind: "node", id: change.id };
          return previous.kind === "node" && previous.id === change.id ? { kind: "summary" } : previous;
        });
      }
      if (!canEdit) return;
      setCanvasNodes((previous) => applyNodeChanges(changes, previous) as TopologyNode[]);
    },
    [canEdit],
  );

  const handleEdgesChange = useCallback((changes: EdgeChange<TopologyEdge>[]) => {
    for (const change of changes) {
      if (change.type !== "select") continue;
      setSelection((previous) => {
        if (change.selected) return { kind: "edge", id: change.id };
        return previous.kind === "edge" && previous.id === change.id ? { kind: "summary" } : previous;
      });
    }
  }, []);

  const handleNodeDragStop = useCallback<OnNodeDrag<TopologyNode>>(
    (_, node) => {
      if (!canEdit) return;
      const edge = findInsertEdge({ x: node.position.x + NODE_WIDTH / 2, y: node.position.y + NODE_HEIGHT / 2 }, node.id);
      setInsertEdgeId(null);
      if (edge) {
        commitGraphResult(insertTopologyGraphNodeOnEdge(latestGraphRef.current, node.id, edge.id, operatorsById, node.position));
        return;
      }
      commitGraphResult(updateTopologyGraphNodePosition(latestGraphRef.current, node.id, node.position));
    },
    [canEdit, commitGraphResult, findInsertEdge, operatorsById],
  );

  const handleNodeDrag = useCallback<OnNodeDrag<TopologyNode>>(
    (_, node) => {
      if (!canEdit) return;
      const edge = findInsertEdge({ x: node.position.x + NODE_WIDTH / 2, y: node.position.y + NODE_HEIGHT / 2 }, node.id);
      setInsertEdgeId(edge?.id ?? null);
    },
    [canEdit, findInsertEdge],
  );

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!canEdit) return;
      commitGraphResult(connectTopologyGraphEdge(latestGraphRef.current, connection, operatorsById));
    },
    [canEdit, commitGraphResult, operatorsById],
  );

  const handleInspectorConnect = useCallback(
    (connection: Connection): string | null => {
      if (!canEdit) return t("core.ui.pipelines.topology.read_only", {}, "Read-only graph view");
      const result = connectTopologyGraphEdge(latestGraphRef.current, connection, operatorsById);
      if (!result.ok) return editResultMessage(result, t);
      // Keep the node inspector mounted so keyboard focus stays on Connect.
      commitGraphResult({ ok: true, graph: result.graph });
      return null;
    },
    [canEdit, commitGraphResult, operatorsById, t],
  );

  const handleUpdateNodeConfig = useCallback(
    (nodeId: string, config: Record<string, unknown>) => {
      if (!canEdit) return;
      commitGraphResult(updateTopologyGraphNodeConfig(latestGraphRef.current, nodeId, config));
    },
    [canEdit, commitGraphResult],
  );

  const handleUpdateEdgePolicy = useCallback(
    (edgeId: string, patch: TopologyEdgePolicyPatch) => {
      if (!canEdit) return;
      commitGraphResult(updateTopologyGraphEdgePolicy(latestGraphRef.current, edgeId, patch));
    },
    [canEdit, commitGraphResult],
  );

  const deleteGraphIds = useCallback(
    (ids: string[], deleteOne: TopologyDeleteGraphItem) => {
      if (!canEdit || ids.length === 0) return;
      let currentGraph = latestGraphRef.current;
      for (const id of ids) {
        const result = deleteOne(currentGraph, id);
        if (!result.ok) {
          setActionError(editResultMessage(result, t));
          return;
        }
        currentGraph = result.graph;
      }
      commitGraphResult({ ok: true, graph: currentGraph as Record<string, unknown> });
      setSelection({ kind: "summary" });
    },
    [canEdit, commitGraphResult, t],
  );

  const deleteNodeIds = useCallback((nodeIds: string[]) => deleteGraphIds(nodeIds, deleteTopologyGraphNode), [deleteGraphIds]);
  const deleteEdgeIds = useCallback((edgeIds: string[]) => deleteGraphIds(edgeIds, deleteTopologyGraphEdge), [deleteGraphIds]);

  const handleOperatorDragStart = useCallback((event: React.DragEvent<HTMLButtonElement>, operator: PipelineOperatorDefinition) => {
    event.dataTransfer.setData("application/toposync-operator", operator.id);
    event.dataTransfer.effectAllowed = "copy";
  }, []);

  const handleDragOver = useCallback(
    (event: React.DragEvent) => {
      if (!canEdit) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      const position = flowInstance?.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      setInsertEdgeId(position ? findInsertEdge(position)?.id ?? null : null);
    },
    [canEdit, findInsertEdge, flowInstance],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      if (!canEdit) return;
      event.preventDefault();
      const operatorId = event.dataTransfer.getData("application/toposync-operator");
      const operator = operatorsById[operatorId];
      if (!operator) return;
      const position = flowInstance?.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      const edge = position ? findInsertEdge(position) : null;
      addOperator(operator, position, edge ? edge.id : null);
    },
    [addOperator, canEdit, findInsertEdge, flowInstance, operatorsById],
  );

  useEffect(() => {
    if (!pendingFocusNodeId || !flowInstance) return;
    const node = sourceNodes.find((item) => item.id === pendingFocusNodeId);
    if (!node) return;
    void flowInstance.setCenter(node.position.x + 116, node.position.y + 56, { duration: 180, zoom: 0.9 });
    setPendingFocusNodeId(null);
  }, [flowInstance, pendingFocusNodeId, sourceNodes]);

  useEffect(() => {
    if (!paletteOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setPaletteOpen(false);
      setOperatorQuery("");
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [paletteOpen]);

  useEffect(() => {
    const updateFullscreenState = () => {
      const active = document.fullscreenElement === graphPaneRef.current;
      setFullscreenActive(active);
      window.setTimeout(() => fitTopologyView(), 80);
    };
    document.addEventListener("fullscreenchange", updateFullscreenState);
    updateFullscreenState();
    return () => document.removeEventListener("fullscreenchange", updateFullscreenState);
  }, [fitTopologyView]);

  if (!built.ok) {
    return (
      <div className="pipelineTopologyFallback">
        <div className="pipelineTopologyFallbackHeader">
          <div>
            <div className="pipelineTopologyFallbackTitle">{built.titleKey ? t(built.titleKey, {}, built.title) : built.title}</div>
            <div className="pipelineTopologyFallbackDetail">{built.detailKey ? t(built.detailKey, {}, built.detail) : built.detail}</div>
          </div>
        </div>
        <pre className="pipelineTopologyJsonFallback">{graphText}</pre>
      </div>
    );
  }

  const model = built.model;
  const nodes = sourceNodes.map((node) => ({
    ...node,
    selected: selection.kind === "node" && selection.id === node.id,
  }));
  const edges = model.edges.map((edge) => ({
    ...edge,
    selected: (selection.kind === "edge" && selection.id === edge.id) || insertEdgeId === edge.id,
  }));
  const flowKey = `${model.summary.graphUid}:${model.edges.map((edge) => edge.id).join("|")}`;
  const validationState = validationLoading
    ? { tone: "checking", icon: "fa-spinner", label: t("core.ui.pipelines.topology.validating", {}, "Validating") }
    : validationError
      ? { tone: "error", icon: "fa-circle-exclamation", label: t("core.ui.pipelines.topology.validation_error", {}, "Validation error") }
      : validationSuccessAt
        ? { tone: "ok", icon: "fa-circle-check", label: t("core.ui.pipelines.topology.valid", {}, "Valid") }
        : dirty
          ? { tone: "pending", icon: "fa-circle-dot", label: t("core.ui.pipelines.topology.not_validated", {}, "Not validated") }
          : null;

  return (
    <div className="pipelineTopologyRoot">
      <div className="pipelineTopologyGraphPane" data-fullscreen={fullscreenActive ? "true" : undefined} ref={graphPaneRef}>
        <div className="pipelineTopologyToolbar">
          {!canEdit ? (
            <span className="pipelineTopologyStatusChip" data-mode="read">
              <i className="fa-solid fa-lock" aria-hidden="true" />
              {t("core.ui.pipelines.topology.read_only_short", {}, "Read-only")}
            </span>
          ) : null}
          {dirty ? <span className="pipelineTopologyStatusChip">{t("core.ui.pipelines.topology.unsaved", {}, "Unsaved")}</span> : null}
          <span
            aria-hidden={validationState ? undefined : "true"}
            className="pipelineTopologyStatusChip pipelineTopologyValidationStatus"
            data-empty={validationState ? undefined : "true"}
            data-mode={validationState?.tone}
          >
            {validationState ? (
              <>
                <i
                  className={`fa-solid ${validationState.icon} ${validationState.tone === "checking" ? "fa-spin" : ""}`}
                  aria-hidden="true"
                />
                {validationState.label}
              </>
            ) : (
              t("core.ui.pipelines.topology.not_validated", {}, "Not validated")
            )}
          </span>
          <button className="pillButton" type="button" disabled={!canEdit} onClick={() => setPaletteOpen((prev) => !prev)}>
            <i className="fa-solid fa-plus" aria-hidden="true" />
            {t("core.ui.pipelines.topology.add_node", {}, "Add node")}
          </button>
          <button className="pillButton" type="button" disabled={!canEdit || undoGraphStack.length === 0} onClick={undoLastEdit}>
            <i className="fa-solid fa-rotate-left" aria-hidden="true" />
            {t("core.actions.undo", {}, "Undo")}
          </button>
          <button className="pillButton" type="button" disabled={!canEdit || model.summary.nodeCount < 2} onClick={arrangeTopology}>
            <i className="fa-solid fa-sitemap" aria-hidden="true" />
            {t("core.ui.pipelines.topology.arrange", {}, "Arrange")}
          </button>
          <button className="pillButton" type="button" onClick={() => void toggleFullscreen()}>
            <i className={`fa-solid ${fullscreenActive ? "fa-compress" : "fa-expand"}`} aria-hidden="true" />
            {fullscreenActive
              ? t("core.ui.pipelines.topology.exit_fullscreen", {}, "Exit fullscreen")
              : t("core.ui.pipelines.topology.fullscreen", {}, "Fullscreen")}
          </button>
          <button
            className="pillButton pipelineTopologyValidateButton"
            type="button"
            disabled={!canEdit || validationLoading}
            onClick={onValidate}
          >
            <i className="fa-solid fa-shield-halved" aria-hidden="true" />
            {validationLoading
              ? t("core.ui.pipelines.topology.validating", {}, "Validating")
              : t("core.ui.pipelines.topology.validate", {}, "Validate")}
          </button>
          <button className="pillButton" type="button" disabled={!canEdit || !dirty} onClick={onDiscard}>
            <i className="fa-solid fa-rotate-left" aria-hidden="true" />
            {t("core.ui.pipelines.topology.discard", {}, "Discard")}
          </button>
        </div>
        <div className="pipelineTopologyCanvas" aria-label={t("core.ui.pipelines.topology.canvas", {}, "Pipeline topology")}>
          {paletteOpen && canEdit ? (
            <div className="pipelineTopologyPalette">
              <div className="pipelineTopologyPaletteHeader">
                <strong>{t("core.ui.pipelines.topology.add_node", {}, "Add node")}</strong>
                <button
                  aria-label={t("core.actions.close", {}, "Close")}
                  className="pipelineTopologyPaletteClose"
                  type="button"
                  onClick={() => {
                    setPaletteOpen(false);
                    setOperatorQuery("");
                  }}
                >
                  <i className="fa-solid fa-xmark" aria-hidden="true" />
                </button>
              </div>
              <input
                autoFocus
                id="pipeline-topology-operator-search"
                name="operator_search"
                value={operatorQuery}
                placeholder={t("core.ui.pipelines.topology.search_operator", {}, "Search operator")}
                onChange={(event) => setOperatorQuery(event.target.value)}
              />
              <div className="pipelineTopologyPaletteList">
                {filteredOperatorOptions.map((operator) => {
                  const label = prettyOperatorName(operator.id) || operator.id;
                  const description = prettyOperatorDescription(operator) || operator.id;
                  const ariaLabel = description && description !== operator.id ? `${label}. ${description}` : label;
                  return (
                    <button
                      aria-label={ariaLabel}
                      draggable
                      key={operator.id}
                      type="button"
                      onClick={() => addOperator(operator)}
                      onDragStart={(event) => handleOperatorDragStart(event, operator)}
                    >
                      <strong>{label}</strong>
                      <span title={description || operator.id}>{description || operator.id}</span>
                    </button>
                  );
                })}
                {filteredOperatorOptions.length === 0 ? (
                  <div className="pipelineTopologyPaletteEmpty">
                    {t("core.ui.pipelines.topology.no_operators", {}, "No operators found.")}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
          {actionError || validationError ? (
            <div className="pipelineTopologyActionError">{actionError || validationError}</div>
          ) : null}
          <ReactFlow<TopologyNode, TopologyEdge>
            key={flowKey}
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
            fitView
            fitViewOptions={{ padding: 0.24 }}
            nodesDraggable={canEdit}
            nodesConnectable={canEdit}
            edgesReconnectable={false}
            connectOnClick={false}
            elementsSelectable
            nodesFocusable
            edgesFocusable
            minZoom={0.2}
            maxZoom={1.5}
            deleteKeyCode={canEdit ? ["Backspace", "Delete"] : null}
            onInit={setFlowInstance}
            onNodesChange={handleNodesChange}
            onEdgesChange={handleEdgesChange}
            onNodesDelete={(deletedNodes) => deleteNodeIds(deletedNodes.map((node) => node.id))}
            onEdgesDelete={(deletedEdges) => {
              if (selection.kind === "node") return;
              deleteEdgeIds(deletedEdges.map((edge) => edge.id));
            }}
            onNodeDrag={handleNodeDrag}
            onNodeDragStop={handleNodeDragStop}
            onConnect={handleConnect}
            onDragOver={handleDragOver}
            onDragLeave={() => setInsertEdgeId(null)}
            onDrop={handleDrop}
            onNodeClick={(_, node) => setSelection({ kind: "node", id: node.id })}
            onEdgeClick={(_, edge) => setSelection({ kind: "edge", id: edge.id })}
            onPaneClick={() => setSelection({ kind: "summary" })}
          >
            <TopologyInternalsUpdater nodes={nodes} />
            <Background gap={24} size={1} />
            <MiniMap
              className="pipelineTopologyMiniMap"
              pannable
              zoomable
              nodeBorderRadius={6}
              nodeColor={(node) => (node as TopologyNode).data.color}
              nodeStrokeColor={(node) => minimapStrokeColor(node as TopologyNode)}
            />
            <Controls showFitView={false} showInteractive={false} />
          </ReactFlow>
          {model.summary.nodeCount === 0 ? (
            <div className="pipelineTopologyEmpty">
              {t("core.ui.pipelines.topology.empty", {}, "This graph has no nodes yet.")}
            </div>
          ) : null}
        </div>
      </div>
      <TopologyInspector
        model={model}
        selection={selection}
        runtimeStatus={runtimeStatus}
        runtimeGeneratedAt={runtimeInfo?.generated_at ?? null}
        editable={canEdit}
        pipelineName={pipelineName}
        processingServerId={processingServerId}
        onOpenProcessingServers={onOpenProcessingServers}
        operatorsById={operatorsById}
        interactiveCameraId={interactiveCameraId}
        camerasIndex={camerasIndex}
        cameraSelectOptions={cameraSelectOptions}
        cameraSelectOptionById={cameraSelectOptionById}
        activeCameraContexts={activeCameraContexts}
        activeCameraContextsError={activeCameraContextsError}
        cameraAreaOptions={cameraAreaOptions}
        operatorPanels={operatorPanels}
        onOpenTelemetryField={onOpenTelemetryField}
        onUpdateNodeConfig={handleUpdateNodeConfig}
        onConnect={handleInspectorConnect}
        onUpdateEdgePolicy={handleUpdateEdgePolicy}
        onDeleteNode={(nodeId) => deleteNodeIds([nodeId])}
        onDeleteEdge={(edgeId) => deleteEdgeIds([edgeId])}
        collapsed={inspectorCollapsed}
        onToggleCollapsed={() => setInspectorCollapsed((prev) => !prev)}
      />
    </div>
  );
}
