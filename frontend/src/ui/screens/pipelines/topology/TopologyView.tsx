import type React from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PipelineOperatorPanel } from "@toposync/plugin-api";
import { applyNodeChanges, Background, Controls, MiniMap, ReactFlow, useUpdateNodeInternals } from "@xyflow/react";
import type {
  Connection,
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
import { prettyOperatorName } from "../utils";
import type { SelectOption, TelemetryFieldInspectorRequest } from "../types";
import { useCameraContexts } from "../editor/useCameraContexts";
import {
  addTopologyGraphNode,
  connectTopologyGraphEdge,
  deleteTopologyGraphEdge,
  deleteTopologyGraphNode,
  updateTopologyGraphEdgePolicy,
  updateTopologyGraphNodeConfig,
  updateTopologyGraphNodePosition,
  type TopologyEdgePolicyPatch,
  type TopologyGraphEditResult,
} from "./topologyGraph";
import { buildTopologyModel } from "./topologyModel";
import { TopologyEdgeComponent } from "./TopologyEdge";
import { TopologyInspector } from "./TopologyInspector";
import { TopologyNodeComponent } from "./TopologyNode";
import type { TopologyEdge, TopologyNode, TopologyRuntimeStatus, TopologySelection } from "./topologyTypes";

const NODE_TYPES: NodeTypes = { topologyNode: TopologyNodeComponent };
const EDGE_TYPES: EdgeTypes = { topologyEdge: TopologyEdgeComponent };

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

function modelPositionKey(nodes: TopologyNode[]): string {
  return nodes.map((node) => `${node.id}:${Math.round(node.position.x)}:${Math.round(node.position.y)}`).join("|");
}

function mergeNodePositions(modelNodes: TopologyNode[], canvasNodes: TopologyNode[]): TopologyNode[] {
  if (canvasNodes.length === 0) return modelNodes;
  const positions = new Map(canvasNodes.map((node) => [node.id, node.position]));
  return modelNodes.map((node) => {
    const position = positions.get(node.id);
    return position ? { ...node, position } : node;
  });
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
        return `${operator.id} ${prettyOperatorName(operator.id)} ${operator.description}`.toLowerCase().includes(query);
      })
      .slice(0, 80);
  }, [operatorOptions, operatorQuery]);
  const graphPositionKey = built.ok
    ? `${pipelineName}:${built.model.summary.graphUid}:${modelPositionKey(built.model.nodes)}`
    : `${pipelineName}:unsupported`;
  const sourceNodes = built.ok && canEdit ? mergeNodePositions(built.model.nodes, canvasNodes) : built.ok ? built.model.nodes : [];

  useEffect(() => {
    setSelection({ kind: "summary" });
    undoGraphStackRef.current = [];
    setUndoGraphStack([]);
  }, [pipelineName, built.ok ? built.model.summary.graphUid : "unsupported"]);

  useEffect(() => {
    if (dirty) return;
    undoGraphStackRef.current = [];
    setUndoGraphStack([]);
  }, [dirty]);

  useEffect(() => {
    latestGraphRef.current = graph;
  }, [graph]);

  useEffect(() => {
    setCanvasNodes(built.ok ? built.model.nodes : []);
  }, [graphPositionKey]);

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
      }
      if (result.edgeId) setSelection({ kind: "edge", id: result.edgeId });
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
    (operator: PipelineOperatorDefinition, position?: XYPosition) => {
      if (!canEdit) return;
      commitGraphResult(addTopologyGraphNode(latestGraphRef.current, operator.id, operator, position));
      setPaletteOpen(false);
      setOperatorQuery("");
    },
    [canEdit, commitGraphResult],
  );

  const handleNodesChange = useCallback(
    (changes: NodeChange<TopologyNode>[]) => {
      if (!canEdit) return;
      setCanvasNodes((previous) => applyNodeChanges(changes, previous) as TopologyNode[]);
    },
    [canEdit],
  );

  const handleNodeDragStop = useCallback<OnNodeDrag<TopologyNode>>(
    (_, node) => {
      if (!canEdit) return;
      commitGraphResult(updateTopologyGraphNodePosition(latestGraphRef.current, node.id, node.position));
    },
    [canEdit, commitGraphResult],
  );

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!canEdit) return;
      commitGraphResult(connectTopologyGraphEdge(latestGraphRef.current, connection, operatorsById));
    },
    [canEdit, commitGraphResult, operatorsById],
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

  const deleteNodeIds = useCallback(
    (nodeIds: string[]) => {
      if (!canEdit || nodeIds.length === 0) return;
      let currentGraph = latestGraphRef.current;
      for (const nodeId of nodeIds) {
        const result = deleteTopologyGraphNode(currentGraph, nodeId);
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

  const deleteEdgeIds = useCallback(
    (edgeIds: string[]) => {
      if (!canEdit || edgeIds.length === 0) return;
      let currentGraph = latestGraphRef.current;
      for (const edgeId of edgeIds) {
        const result = deleteTopologyGraphEdge(currentGraph, edgeId);
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

  const handleOperatorDragStart = useCallback((event: React.DragEvent<HTMLButtonElement>, operator: PipelineOperatorDefinition) => {
    event.dataTransfer.setData("application/toposync-operator", operator.id);
    event.dataTransfer.effectAllowed = "copy";
  }, []);

  const handleDragOver = useCallback(
    (event: React.DragEvent) => {
      if (!canEdit) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    },
    [canEdit],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      if (!canEdit) return;
      event.preventDefault();
      const operatorId = event.dataTransfer.getData("application/toposync-operator");
      const operator = operatorsById[operatorId];
      if (!operator) return;
      const position = flowInstance?.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      addOperator(operator, position);
    },
    [addOperator, canEdit, flowInstance, operatorsById],
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
    selected: selection.kind === "edge" && selection.id === edge.id,
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
          {validationState ? (
            <span className="pipelineTopologyStatusChip" data-mode={validationState.tone}>
              <i className={`fa-solid ${validationState.icon} ${validationState.tone === "checking" ? "fa-spin" : ""}`} aria-hidden="true" />
              {validationState.label}
            </span>
          ) : null}
          <button className="pillButton" type="button" disabled={!canEdit} onClick={() => setPaletteOpen((prev) => !prev)}>
            <i className="fa-solid fa-plus" aria-hidden="true" />
            {t("core.ui.pipelines.topology.add_node", {}, "Add node")}
          </button>
          <button className="pillButton" type="button" disabled={!canEdit || undoGraphStack.length === 0} onClick={undoLastEdit}>
            <i className="fa-solid fa-rotate-left" aria-hidden="true" />
            {t("core.actions.undo", {}, "Undo")}
          </button>
          <button className="pillButton" type="button" onClick={() => void toggleFullscreen()}>
            <i className={`fa-solid ${fullscreenActive ? "fa-compress" : "fa-expand"}`} aria-hidden="true" />
            {fullscreenActive
              ? t("core.ui.pipelines.topology.exit_fullscreen", {}, "Exit fullscreen")
              : t("core.ui.pipelines.topology.fullscreen", {}, "Fullscreen")}
          </button>
          <button className="pillButton" type="button" disabled={!canEdit || validationLoading} onClick={onValidate}>
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
                  const description = String(operator.description || operator.id).trim();
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
            onNodesDelete={(deletedNodes) => deleteNodeIds(deletedNodes.map((node) => node.id))}
            onEdgesDelete={(deletedEdges) => {
              if (selection.kind === "node") return;
              deleteEdgeIds(deletedEdges.map((edge) => edge.id));
            }}
            onNodeDragStop={handleNodeDragStop}
            onConnect={handleConnect}
            onDragOver={handleDragOver}
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
        onUpdateEdgePolicy={handleUpdateEdgePolicy}
        onDeleteNode={(nodeId) => deleteNodeIds([nodeId])}
        onDeleteEdge={(edgeId) => deleteEdgeIds([edgeId])}
        collapsed={inspectorCollapsed}
        onToggleCollapsed={() => setInspectorCollapsed((prev) => !prev)}
      />
    </div>
  );
}
