import Editor from "@monaco-editor/react";
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { i18n } from "../../util/i18n";
import type { Pipeline, PipelineOperatorDefinition, ProcessingServer } from "../../util/api";
import {
  compilePipeline,
  createPipeline,
  deletePipeline,
  deleteProcessingServer,
  getPipelinesFeatureFlag,
  listPipelineOperators,
  listPipelines,
  listProcessingServers,
  putPipeline,
  putProcessingServer,
  setPipelinesFeatureFlag,
} from "../../util/api";

type Props = {
  onClose: () => void;
};

type EditorMode = "interactive" | "json" | "python";
type DragInsertPosition = "before" | "after";

type InteractiveStep = {
  uid: string;
  nodeId: string;
  operatorId: string;
  configText: string;
  collapsed: boolean;
};

type InteractiveBuildResult = {
  graph: Record<string, unknown> | null;
  error: string | null;
};

type InteractiveFromGraphResult = {
  steps: InteractiveStep[];
  warning: string | null;
};

const PIPELINE_PRESET_OPERATOR_IDS = [
  "camera.source",
  "camera.motion_gate",
  "core.fps_reducer",
  "vision.object_tracking_yolo",
  "vision.object_detection_yolo",
  "camera.object_segmentation",
  "camera.camera_mapping",
  "camera.area_restriction",
  "camera.velocity_estimation",
  "camera.best_frame_selector",
  "core.throttle",
  "core.debounce",
  "core.store_images",
  "core.notify",
];

const NODE_ID_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

let interactiveStepCounter = 0;

function nextInteractiveStepUid(): string {
  interactiveStepCounter += 1;
  return `step_${interactiveStepCounter.toString(36)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeJsonParse(value: string): { ok: true; data: unknown } | { ok: false; error: string } {
  try {
    return { ok: true, data: JSON.parse(value) };
  } catch (err: any) {
    return { ok: false, error: String(err?.message ?? err) };
  }
}

function jsonPretty(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

function emptyGraph(): Record<string, unknown> {
  return { schema_version: 1, nodes: [], edges: [] };
}

function defaultPipeline(name: string, type: "reuse" | "final"): Pipeline {
  return {
    name,
    type,
    enabled: true,
    processing_server_id: "local",
    editor_mode: "interactive",
    python_source: "",
    graph: emptyGraph(),
  };
}

function slugifyOperatorId(operatorId: string): string {
  const raw = String(operatorId || "").trim();
  if (!raw) return "step";
  const last = raw.split(".").pop() || raw;
  const slug = last.replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
  return slug || "step";
}

function nextUniqueNodeId(base: string, used: Set<string>): string {
  const normalizedBase = (base || "step").replace(/[^A-Za-z0-9_]+/g, "_").replace(/^\d/, "_").replace(/^_+|_+$/g, "");
  const root = normalizedBase || "step";
  if (!used.has(root)) {
    used.add(root);
    return root;
  }
  let index = 2;
  while (used.has(`${root}_${index}`)) {
    index += 1;
  }
  const value = `${root}_${index}`;
  used.add(value);
  return value;
}

function createInteractiveStep(
  operatorId: string,
  defaults: Record<string, unknown>,
  usedNodeIds: Set<string>,
): InteractiveStep {
  const nodeId = nextUniqueNodeId(slugifyOperatorId(operatorId), usedNodeIds);
  return {
    uid: nextInteractiveStepUid(),
    nodeId,
    operatorId,
    configText: jsonPretty(defaults),
    collapsed: false,
  };
}

function edgePolicyFor(source: PipelineOperatorDefinition | null, target: PipelineOperatorDefinition | null): { maxsize: number; drop_policy: string } {
  const sourceCaps = new Set((source?.capabilities ?? []).map((value) => String(value).trim().toLowerCase()));
  const targetCaps = new Set((target?.capabilities ?? []).map((value) => String(value).trim().toLowerCase()));

  if (sourceCaps.has("source") || sourceCaps.has("camera") || sourceCaps.has("realtime")) {
    return { maxsize: 1, drop_policy: "latest_only" };
  }
  if (targetCaps.has("sink") || targetCaps.has("origin_only")) {
    return { maxsize: 128, drop_policy: "drop_oldest" };
  }
  return { maxsize: 32, drop_policy: "drop_oldest" };
}

function buildGraphFromInteractiveSteps(
  steps: InteractiveStep[],
  operatorsById: Record<string, PipelineOperatorDefinition>,
): InteractiveBuildResult {
  const usedNodeIds = new Set<string>();
  const nodes: Array<Record<string, unknown>> = [];

  for (let index = 0; index < steps.length; index += 1) {
    const step = steps[index];
    const operatorId = String(step.operatorId || "").trim();
    if (!operatorId) {
      return { graph: null, error: `Step ${index + 1} has no operator selected.` };
    }
    if (!operatorsById[operatorId]) {
      return { graph: null, error: `Step ${index + 1} uses unknown operator '${operatorId}'.` };
    }

    const nodeIdRaw = String(step.nodeId || "").trim();
    if (!nodeIdRaw) {
      return { graph: null, error: `Step ${index + 1} must have a node id.` };
    }
    if (!NODE_ID_RE.test(nodeIdRaw)) {
      return { graph: null, error: `Step ${index + 1} node id '${nodeIdRaw}' is invalid.` };
    }
    if (usedNodeIds.has(nodeIdRaw)) {
      return { graph: null, error: `Duplicate node id '${nodeIdRaw}'.` };
    }
    usedNodeIds.add(nodeIdRaw);

    const parsed = safeJsonParse(step.configText || "{}");
    if (!parsed.ok) {
      return { graph: null, error: `Step ${index + 1} has invalid config JSON: ${parsed.error}` };
    }
    if (!isRecord(parsed.data)) {
      return { graph: null, error: `Step ${index + 1} config must be a JSON object.` };
    }

    nodes.push({
      id: nodeIdRaw,
      operator: operatorId,
      config: parsed.data,
    });
  }

  const edges: Array<Record<string, unknown>> = [];
  for (let index = 0; index < nodes.length - 1; index += 1) {
    const sourceNode = nodes[index];
    const targetNode = nodes[index + 1];
    const sourceOperatorId = String(sourceNode.operator || "");
    const targetOperatorId = String(targetNode.operator || "");
    const policy = edgePolicyFor(operatorsById[sourceOperatorId] ?? null, operatorsById[targetOperatorId] ?? null);

    edges.push({
      from: { node: sourceNode.id, port: "out" },
      to: { node: targetNode.id, port: "in" },
      maxsize: policy.maxsize,
      drop_policy: policy.drop_policy,
    });
  }

  return {
    graph: {
      schema_version: 1,
      nodes,
      edges,
    },
    error: null,
  };
}

function readLinearNodeOrder(graphValue: unknown): { order: string[]; warning: string | null } {
  if (!isRecord(graphValue)) {
    return { order: [], warning: null };
  }
  const rawNodes = Array.isArray(graphValue.nodes) ? graphValue.nodes : [];
  const rawEdges = Array.isArray(graphValue.edges) ? graphValue.edges : [];
  const nodeIds = rawNodes
    .map((value) => (isRecord(value) ? String(value.id || "").trim() : ""))
    .filter((value) => value.length > 0);

  if (nodeIds.length <= 1) {
    return { order: nodeIds, warning: null };
  }

  const inDegree = new Map<string, number>();
  const outEdges = new Map<string, string[]>();
  for (const id of nodeIds) {
    inDegree.set(id, 0);
    outEdges.set(id, []);
  }

  for (const edge of rawEdges) {
    if (!isRecord(edge)) continue;
    const from = isRecord(edge.from) ? String(edge.from.node || "").trim() : "";
    const to = isRecord(edge.to) ? String(edge.to.node || "").trim() : "";
    if (!from || !to) continue;
    if (!inDegree.has(from) || !inDegree.has(to)) continue;
    outEdges.set(from, [...(outEdges.get(from) ?? []), to]);
    inDegree.set(to, (inDegree.get(to) ?? 0) + 1);
  }

  if ([...outEdges.values()].some((targets) => targets.length > 1) || [...inDegree.values()].some((count) => count > 1)) {
    return {
      order: nodeIds,
      warning: "Graph is not a simple chain. Interactive mode loaded node list order and will rewrite edges sequentially.",
    };
  }

  const starts = nodeIds.filter((id) => (inDegree.get(id) ?? 0) === 0);
  if (starts.length !== 1) {
    return {
      order: nodeIds,
      warning: "Graph has multiple starts. Interactive mode loaded node list order and will rewrite edges sequentially.",
    };
  }

  const order: string[] = [];
  const visited = new Set<string>();
  let current: string | undefined = starts[0];
  while (current) {
    if (visited.has(current)) {
      return {
        order: nodeIds,
        warning: "Graph contains a cycle. Interactive mode loaded node list order and will rewrite edges sequentially.",
      };
    }
    visited.add(current);
    order.push(current);
    const next = outEdges.get(current) ?? [];
    current = next.length > 0 ? next[0] : undefined;
  }

  if (order.length !== nodeIds.length) {
    return {
      order: nodeIds,
      warning: "Graph has disconnected segments. Interactive mode loaded node list order and will rewrite edges sequentially.",
    };
  }

  return { order, warning: null };
}

function buildInteractiveStepsFromGraph(
  graphValue: unknown,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): InteractiveFromGraphResult {
  if (!isRecord(graphValue)) {
    return { steps: [], warning: null };
  }
  const rawNodes = Array.isArray(graphValue.nodes) ? graphValue.nodes : [];
  const nodeById = new Map<string, Record<string, unknown>>();
  for (const node of rawNodes) {
    if (!isRecord(node)) continue;
    const nodeId = String(node.id || "").trim();
    if (!nodeId) continue;
    nodeById.set(nodeId, node);
  }

  const { order, warning } = readLinearNodeOrder(graphValue);
  const usedNodeIds = new Set<string>();
  const steps: InteractiveStep[] = [];

  for (const nodeId of order) {
    const node = nodeById.get(nodeId);
    if (!node) continue;
    const operatorId = String(node.operator || "").trim();
    const operator = operatorsById[operatorId];
    const config = isRecord(node.config) ? node.config : (operator?.defaults ?? {});

    usedNodeIds.add(nodeId);
    steps.push({
      uid: nextInteractiveStepUid(),
      nodeId,
      operatorId,
      configText: jsonPretty(config),
      collapsed: false,
    });
  }

  return { steps, warning };
}

function pickDefaultOperatorId(operators: PipelineOperatorDefinition[]): string {
  for (const operatorId of PIPELINE_PRESET_OPERATOR_IDS) {
    if (operators.some((operator) => operator.id === operatorId)) {
      return operatorId;
    }
  }
  return operators[0]?.id ?? "";
}

function prettyOperatorLabel(operator: PipelineOperatorDefinition): string {
  const tail = operator.id.split(".").pop() || operator.id;
  return `${tail} (${operator.id})`;
}

function moveStep(
  items: InteractiveStep[],
  draggedUid: string,
  targetUid: string,
  position: DragInsertPosition,
): InteractiveStep[] {
  if (draggedUid === targetUid) return items;
  const byUid = new Map(items.map((item) => [item.uid, item]));
  if (!byUid.has(draggedUid) || !byUid.has(targetUid)) return items;

  const withoutDragged = items.filter((item) => item.uid !== draggedUid);
  const targetIndex = withoutDragged.findIndex((item) => item.uid === targetUid);
  if (targetIndex < 0) return items;

  const insertIndex = position === "before" ? targetIndex : targetIndex + 1;
  const next = withoutDragged.slice();
  next.splice(insertIndex, 0, byUid.get(draggedUid)!);
  return next;
}

export function PipelinesScreen({ onClose }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [servers, setServers] = useState<ProcessingServer[]>([]);
  const [operators, setOperators] = useState<PipelineOperatorDefinition[]>([]);
  const [featureFlag, setFeatureFlag] = useState<boolean>(false);
  const [selectedName, setSelectedName] = useState<string | null>(null);

  const [createName, setCreateName] = useState("");
  const [createType, setCreateType] = useState<"reuse" | "final">("final");

  const [draft, setDraft] = useState<Pipeline | null>(null);
  const [graphText, setGraphText] = useState<string>("");
  const [pythonText, setPythonText] = useState<string>("");
  const [mode, setMode] = useState<EditorMode>("interactive");
  const [compileOutput, setCompileOutput] = useState<any>(null);

  const [interactiveSteps, setInteractiveSteps] = useState<InteractiveStep[]>([]);
  const [interactiveWarning, setInteractiveWarning] = useState<string | null>(null);
  const [interactiveAddOperatorId, setInteractiveAddOperatorId] = useState<string>("");
  const [draggingStepUid, setDraggingStepUid] = useState<string | null>(null);
  const [dragOverStep, setDragOverStep] = useState<{ uid: string; position: DragInsertPosition } | null>(null);

  const operatorsById = useMemo(() => {
    const entries = operators.map((operator) => [operator.id, operator] as const);
    return Object.fromEntries(entries);
  }, [operators]);

  const presetOperators = useMemo(
    () => PIPELINE_PRESET_OPERATOR_IDS.map((id) => operatorsById[id]).filter(Boolean),
    [operatorsById],
  );

  const selected = useMemo(() => {
    if (!selectedName) return null;
    return pipelines.find((pipeline) => pipeline.name === selectedName) ?? null;
  }, [pipelines, selectedName]);

  const interactiveGraph = useMemo(
    () => buildGraphFromInteractiveSteps(interactiveSteps, operatorsById),
    [interactiveSteps, operatorsById],
  );

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [flag, pipelineList, serverList, operatorList] = await Promise.all([
        getPipelinesFeatureFlag(),
        listPipelines(),
        listProcessingServers(),
        listPipelineOperators(),
      ]);
      setFeatureFlag(Boolean(flag?.enabled));
      setPipelines(pipelineList);
      setServers(serverList);
      setOperators(operatorList);
      if (!selectedName && pipelineList.length > 0) setSelectedName(pipelineList[0].name);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setLoading(false);
    }
  }, [selectedName]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!selected) {
      setDraft(null);
      setGraphText("");
      setPythonText("");
      setMode("interactive");
      setCompileOutput(null);
      setInteractiveSteps([]);
      setInteractiveWarning(null);
      return;
    }

    setDraft(selected);
    setGraphText(jsonPretty(selected.graph ?? emptyGraph()));
    setPythonText(String(selected.python_source ?? ""));
    setMode((selected.editor_mode as EditorMode) ?? "interactive");
    setCompileOutput(null);

    const loaded = buildInteractiveStepsFromGraph(selected.graph, operatorsById);
    setInteractiveSteps(loaded.steps);
    setInteractiveWarning(loaded.warning);
  }, [selected, operatorsById]);

  useEffect(() => {
    if (interactiveAddOperatorId && operatorsById[interactiveAddOperatorId]) return;
    setInteractiveAddOperatorId(pickDefaultOperatorId(operators));
  }, [interactiveAddOperatorId, operatorsById, operators]);

  useEffect(() => {
    if (mode !== "interactive") return;
    if (!interactiveGraph.graph) return;
    setGraphText(jsonPretty(interactiveGraph.graph));
  }, [mode, interactiveGraph.graph]);

  const isPythonLocked = Boolean(draft && draft.editor_mode === "python");

  const switchMode = (nextMode: EditorMode) => {
    if (!draft) return;
    if (isPythonLocked && nextMode !== "python") return;

    if (nextMode === "interactive" && mode === "json") {
      const parsed = safeJsonParse(graphText);
      if (!parsed.ok) {
        setError(`Invalid graph JSON: ${parsed.error}`);
        return;
      }
      const loaded = buildInteractiveStepsFromGraph(parsed.data, operatorsById);
      setInteractiveSteps(loaded.steps);
      setInteractiveWarning(loaded.warning);
    }

    if (nextMode === "json" && mode === "interactive") {
      if (!interactiveGraph.graph) {
        setError(interactiveGraph.error || "Interactive graph is invalid.");
        return;
      }
      setGraphText(jsonPretty(interactiveGraph.graph));
    }

    setError(null);
    setMode(nextMode);
  };

  const resolveGraphFromActiveMode = (): { ok: true; graph: Record<string, unknown> } | { ok: false; message: string } => {
    if (!draft) return { ok: false, message: "No pipeline selected." };

    if (mode === "interactive") {
      if (!interactiveGraph.graph) {
        return { ok: false, message: interactiveGraph.error || "Interactive graph is invalid." };
      }
      return { ok: true, graph: interactiveGraph.graph };
    }

    if (mode === "json") {
      const parsed = safeJsonParse(graphText);
      if (!parsed.ok) return { ok: false, message: `Invalid graph JSON: ${parsed.error}` };
      if (!isRecord(parsed.data)) return { ok: false, message: "Graph JSON must be an object." };
      return { ok: true, graph: parsed.data };
    }

    const graph = isRecord(draft.graph) ? draft.graph : emptyGraph();
    return { ok: true, graph };
  };

  const handleCreate = async () => {
    const name = createName.trim();
    if (!name) return;
    setError(null);
    try {
      const created = await createPipeline(defaultPipeline(name, createType));
      setPipelines((prev) => [...prev, created].sort((a, b) => a.name.localeCompare(b.name)));
      setSelectedName(created.name);
      setCreateName("");
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleSave = async () => {
    if (!draft) return;
    setError(null);
    setCompileOutput(null);

    const resolved = resolveGraphFromActiveMode();
    if (!resolved.ok) {
      setError(resolved.message);
      return;
    }

    const updated: Pipeline = {
      ...draft,
      graph: resolved.graph,
      editor_mode: mode,
      python_source: mode === "python" ? pythonText : draft.python_source ?? "",
    };

    try {
      const saved = await putPipeline(draft.name, updated);
      setPipelines((prev) => prev.map((pipeline) => (pipeline.name === saved.name ? saved : pipeline)).sort((a, b) => a.name.localeCompare(b.name)));
      setDraft(saved);
      setGraphText(jsonPretty(saved.graph ?? emptyGraph()));
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleCompile = async () => {
    if (!draft) return;
    setError(null);
    setCompileOutput(null);

    const resolved = resolveGraphFromActiveMode();
    if (!resolved.ok) {
      setError(resolved.message);
      return;
    }

    try {
      const output = await compilePipeline({ ...draft, graph: resolved.graph });
      setCompileOutput(output);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleDelete = async () => {
    if (!draft) return;
    if (!confirm(`Delete pipeline '${draft.name}'?`)) return;
    setError(null);
    try {
      await deletePipeline(draft.name);
      setPipelines((prev) => prev.filter((pipeline) => pipeline.name !== draft.name));
      setSelectedName(null);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleToggleFlag = async () => {
    setError(null);
    try {
      const next = await setPipelinesFeatureFlag(!featureFlag);
      setFeatureFlag(Boolean(next?.enabled));
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleSaveServer = async (server: ProcessingServer) => {
    setError(null);
    try {
      await putProcessingServer(server);
      await reload();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleDeleteServer = async (serverId: string) => {
    if (!confirm(`Delete processing server '${serverId}'?`)) return;
    setError(null);
    try {
      await deleteProcessingServer(serverId);
      await reload();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const addInteractiveStep = (operatorId: string) => {
    const op = operatorsById[operatorId];
    if (!op) return;
    setInteractiveSteps((prev) => {
      const used = new Set(prev.map((item) => item.nodeId));
      const next = createInteractiveStep(operatorId, op.defaults ?? {}, used);
      return [...prev, next];
    });
    setInteractiveWarning(null);
  };

  const updateInteractiveStep = (uid: string, patch: Partial<InteractiveStep>) => {
    setInteractiveSteps((prev) => prev.map((step) => (step.uid === uid ? { ...step, ...patch } : step)));
  };

  const removeInteractiveStep = (uid: string) => {
    setInteractiveSteps((prev) => prev.filter((step) => step.uid !== uid));
  };

  const updateInteractiveStepScalar = (uid: string, key: string, value: string | number | boolean) => {
    setInteractiveSteps((prev) =>
      prev.map((step) => {
        if (step.uid !== uid) return step;
        const parsed = safeJsonParse(step.configText || "{}");
        const nextConfig = isRecord(parsed.ok ? parsed.data : null) ? { ...(parsed.data as Record<string, unknown>) } : {};
        nextConfig[key] = value;
        return { ...step, configText: jsonPretty(nextConfig) };
      }),
    );
  };

  const beginStepDrag = useCallback((event: React.DragEvent, uid: string) => {
    setDraggingStepUid(uid);
    setDragOverStep(null);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", uid);
  }, []);

  const endStepDrag = useCallback(() => {
    setDraggingStepUid(null);
    setDragOverStep(null);
  }, []);

  const updateStepDragOver = useCallback(
    (event: React.DragEvent<HTMLElement>, targetUid: string) => {
      const draggedUid = draggingStepUid;
      if (!draggedUid || draggedUid === targetUid) return;
      event.preventDefault();
      const rect = event.currentTarget.getBoundingClientRect();
      const position: DragInsertPosition = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
      setDragOverStep({ uid: targetUid, position });
    },
    [draggingStepUid],
  );

  const dropStep = useCallback(
    (event: React.DragEvent<HTMLElement>, targetUid: string) => {
      const draggedUid = draggingStepUid || event.dataTransfer.getData("text/plain");
      if (!draggedUid || draggedUid === targetUid) return;
      event.preventDefault();
      const rect = event.currentTarget.getBoundingClientRect();
      const position: DragInsertPosition = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
      setInteractiveSteps((prev) => moveStep(prev, draggedUid, targetUid, position));
      setDraggingStepUid(null);
      setDragOverStep(null);
    },
    [draggingStepUid],
  );

  return (
    <div className="pipelinesRoot screenRoot">
      <div className="pipelinesTopbar">
        <button className="iconButton" type="button" onClick={onClose} aria-label={t("core.actions.back", {}, "Back")}>
          <i className="fa-solid fa-arrow-left" aria-hidden="true" />
        </button>
        <div className="pipelinesTitle">Pipelines</div>
        <div className="pipelinesTopbarRight">
          <label className="pipelinesFlag">
            <input type="checkbox" checked={featureFlag} onChange={() => void handleToggleFlag()} />
            <span>Enable pipelines</span>
          </label>
        </div>
      </div>

      <div className="pipelinesBody">
        <div className="pipelinesSidebar">
          <div className="pipelinesSidebarHeader">
            <div className="pipelinesSidebarTitle">Pipelines</div>
          </div>

          <div className="pipelinesCreate">
            <input
              className="pipelinesInput"
              placeholder="pipeline_name"
              value={createName}
              onChange={(event) => setCreateName(event.target.value)}
            />
            <select className="pipelinesSelect" value={createType} onChange={(event) => setCreateType(event.target.value as any)}>
              <option value="final">final</option>
              <option value="reuse">reuse</option>
            </select>
            <button className="pillButton" type="button" onClick={() => void handleCreate()}>
              Create
            </button>
          </div>

          <div className="pipelinesList">
            {pipelines.map((pipeline) => (
              <button
                key={pipeline.name}
                type="button"
                className={["pipelinesListItem", selectedName === pipeline.name ? "isActive" : ""].filter(Boolean).join(" ")}
                onClick={() => setSelectedName(pipeline.name)}
              >
                <div className="pipelinesListItemName">{pipeline.name}</div>
                <div className="pipelinesListItemMeta">{pipeline.type}</div>
              </button>
            ))}
          </div>

          <div className="pipelinesSidebarFooter">
            <div className="pipelinesSidebarTitle">Processing</div>
            <div className="pipelinesServers">
              {servers.map((server) => (
                <div key={server.id} className="pipelinesServerRow">
                  <div className="pipelinesServerMain">
                    <div className="pipelinesServerId">{server.id}</div>
                    <div className="pipelinesServerMeta">
                      {server.kind}
                      {server.url ? ` • ${server.url}` : ""}
                    </div>
                  </div>
                  {server.id !== "local" ? (
                    <button className="iconButton" type="button" onClick={() => void handleDeleteServer(server.id)} title="Delete server">
                      <i className="fa-solid fa-trash" aria-hidden="true" />
                    </button>
                  ) : null}
                </div>
              ))}
            </div>

            <button
              className="pillButton"
              type="button"
              onClick={() =>
                void handleSaveServer({
                  id: `srv_${Math.random().toString(16).slice(2, 8)}`,
                  name: "",
                  kind: "http",
                  url: "http://127.0.0.1:9001",
                })
              }
            >
              Add HTTP server (stub)
            </button>
          </div>
        </div>

        <div className="pipelinesEditor">
          {loading ? (
            <div className="card">
              <div className="cardBody">Loading…</div>
            </div>
          ) : null}

          {error ? (
            <div className="card cardDanger">
              <div className="cardBody">{error}</div>
            </div>
          ) : null}

          {!draft ? (
            <div className="card">
              <div className="cardBody">Select or create a pipeline.</div>
            </div>
          ) : (
            <div className="pipelinesEditorInner">
              <div className="pipelinesEditorHeader">
                <div className="pipelinesEditorTitle">{draft.name}</div>
                <div className="pipelinesEditorActions">
                  <button className="pillButton" type="button" onClick={() => void handleCompile()}>
                    Compile
                  </button>
                  <button className="pillButton pillButtonPrimary" type="button" onClick={() => void handleSave()}>
                    Save
                  </button>
                  <button className="pillButton pillButtonDanger" type="button" onClick={() => void handleDelete()}>
                    Delete
                  </button>
                </div>
              </div>

              <div className="pipelinesEditorGrid">
                <div className="pipelinesForm">
                  <label className="pipelinesLabel">
                    <span>Type</span>
                    <select
                      className="pipelinesSelect"
                      value={draft.type}
                      onChange={(event) => setDraft((prev) => (prev ? { ...prev, type: event.target.value as any } : prev))}
                      disabled={isPythonLocked}
                    >
                      <option value="final">final</option>
                      <option value="reuse">reuse</option>
                    </select>
                  </label>

                  {draft.type === "final" ? (
                    <>
                      <label className="pipelinesLabel">
                        <span>Enabled</span>
                        <input
                          type="checkbox"
                          checked={draft.enabled !== false}
                          onChange={(event) => setDraft((prev) => (prev ? { ...prev, enabled: event.target.checked } : prev))}
                        />
                      </label>

                      <label className="pipelinesLabel">
                        <span>Processing server</span>
                        <select
                          className="pipelinesSelect"
                          value={draft.processing_server_id ?? "local"}
                          onChange={(event) =>
                            setDraft((prev) => (prev ? { ...prev, processing_server_id: event.target.value } : prev))
                          }
                        >
                          {servers.map((server) => (
                            <option key={server.id} value={server.id}>
                              {server.id}
                            </option>
                          ))}
                        </select>
                      </label>
                    </>
                  ) : null}

                  <div className="pipelinesModes">
                    <button
                      className={["pillButton", mode === "interactive" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      disabled={isPythonLocked}
                      onClick={() => switchMode("interactive")}
                    >
                      Interactive
                    </button>
                    <button
                      className={["pillButton", mode === "json" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      disabled={isPythonLocked}
                      onClick={() => switchMode("json")}
                    >
                      JSON
                    </button>
                    <button
                      className={["pillButton", mode === "python" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      onClick={() => switchMode("python")}
                    >
                      Python (one-way)
                    </button>
                  </div>

                  <div className="pipelinesHint">Operators available: {operators.length}</div>
                </div>

                <div className="pipelinesEditorPanel">
                  {mode === "python" ? (
                    <div className="pipelinesMonacoWrap">
                      <Editor
                        height="520px"
                        language="python"
                        value={pythonText}
                        onChange={(value) => setPythonText(String(value ?? ""))}
                        options={{
                          automaticLayout: true,
                          fontSize: 13,
                          minimap: { enabled: false },
                          scrollBeyondLastLine: false,
                          wordWrap: "on",
                        }}
                      />
                    </div>
                  ) : mode === "interactive" ? (
                    <div className="pipelinesInteractiveRoot">
                      <div className="pipelinesInteractiveToolbar">
                        <div className="pipelinesInteractiveLabel">Add step</div>
                        <div className="pipelinesPresetButtons">
                          {presetOperators.map((operator) => (
                            <button
                              key={operator.id}
                              className="pillButton"
                              type="button"
                              onClick={() => addInteractiveStep(operator.id)}
                              title={operator.description || operator.id}
                            >
                              + {operator.id.split(".").pop()}
                            </button>
                          ))}
                        </div>
                        <div className="pipelinesInlineAddRow">
                          <select
                            className="pipelinesSelect"
                            value={interactiveAddOperatorId}
                            onChange={(event) => setInteractiveAddOperatorId(event.target.value)}
                          >
                            {operators.map((operator) => (
                              <option key={operator.id} value={operator.id}>
                                {prettyOperatorLabel(operator)}
                              </option>
                            ))}
                          </select>
                          <button className="pillButton pillButtonPrimary" type="button" onClick={() => addInteractiveStep(interactiveAddOperatorId)}>
                            Add
                          </button>
                        </div>
                      </div>

                      {interactiveWarning ? (
                        <div className="card">
                          <div className="cardBody">{interactiveWarning}</div>
                        </div>
                      ) : null}

                      {interactiveGraph.error ? (
                        <div className="card cardDanger">
                          <div className="cardBody">{interactiveGraph.error}</div>
                        </div>
                      ) : null}

                      <div className="pipelinesStepsList">
                        {interactiveSteps.map((step, index) => {
                          const operator = operatorsById[step.operatorId];
                          const configParsed = safeJsonParse(step.configText || "{}");
                          const scalarEntries = isRecord(configParsed.ok ? configParsed.data : null)
                            ? Object.entries(configParsed.data as Record<string, unknown>).filter(([, value]) => {
                                const valueType = typeof value;
                                return valueType === "string" || valueType === "number" || valueType === "boolean";
                              })
                            : [];

                          const rowClass = ["pipelinesStepCard"];
                          if (draggingStepUid === step.uid) rowClass.push("isDragSource");
                          if (dragOverStep?.uid === step.uid) {
                            rowClass.push(dragOverStep.position === "before" ? "isDropBefore" : "isDropAfter");
                          }

                          return (
                            <div
                              key={step.uid}
                              className={rowClass.join(" ")}
                              onDragOver={(event) => updateStepDragOver(event, step.uid)}
                              onDrop={(event) => dropStep(event, step.uid)}
                            >
                              <div className="pipelinesStepHeader">
                                <button
                                  className="layerDragHandle"
                                  type="button"
                                  draggable
                                  onDragStart={(event) => beginStepDrag(event, step.uid)}
                                  onDragEnd={endStepDrag}
                                  aria-label="Reorder step"
                                  title="Drag to reorder"
                                >
                                  <i className="fa-solid fa-grip-vertical" aria-hidden="true" />
                                </button>

                                <span className="pipelinesStepIndex">{index + 1}</span>

                                <input
                                  className="pipelinesInput pipelinesStepNodeId"
                                  value={step.nodeId}
                                  onChange={(event) => updateInteractiveStep(step.uid, { nodeId: event.target.value })}
                                  placeholder="node_id"
                                />

                                <select
                                  className="pipelinesSelect pipelinesStepOperator"
                                  value={step.operatorId}
                                  onChange={(event) => {
                                    const nextOperatorId = event.target.value;
                                    const nextOperator = operatorsById[nextOperatorId];
                                    updateInteractiveStep(step.uid, {
                                      operatorId: nextOperatorId,
                                      configText: jsonPretty(nextOperator?.defaults ?? {}),
                                    });
                                  }}
                                >
                                  {operators.map((operatorOption) => (
                                    <option key={operatorOption.id} value={operatorOption.id}>
                                      {prettyOperatorLabel(operatorOption)}
                                    </option>
                                  ))}
                                </select>

                                <button
                                  className="iconButton"
                                  type="button"
                                  aria-label={step.collapsed ? "Expand step" : "Collapse step"}
                                  onClick={() => updateInteractiveStep(step.uid, { collapsed: !step.collapsed })}
                                >
                                  <i className={`fa-solid ${step.collapsed ? "fa-chevron-down" : "fa-chevron-up"}`} aria-hidden="true" />
                                </button>

                                <button
                                  className="iconButton"
                                  type="button"
                                  aria-label="Remove step"
                                  onClick={() => removeInteractiveStep(step.uid)}
                                >
                                  <i className="fa-solid fa-trash" aria-hidden="true" />
                                </button>
                              </div>

                              {!step.collapsed ? (
                                <div className="pipelinesStepBody">
                                  {operator ? <div className="pipelinesStepDescription">{operator.description || operator.id}</div> : null}
                                  {operator && operator.capabilities.length > 0 ? (
                                    <div className="pipelinesStepCapabilities">caps: {operator.capabilities.join(", ")}</div>
                                  ) : null}

                                  {scalarEntries.length > 0 ? (
                                    <div className="pipelinesScalarGrid">
                                      {scalarEntries.map(([key, value]) => (
                                        <label key={`${step.uid}:${key}`} className="pipelinesLabel pipelinesScalarLabel">
                                          <span>{key}</span>
                                          {typeof value === "boolean" ? (
                                            <input
                                              type="checkbox"
                                              checked={value}
                                              onChange={(event) => updateInteractiveStepScalar(step.uid, key, event.target.checked)}
                                            />
                                          ) : typeof value === "number" ? (
                                            <input
                                              className="pipelinesInput"
                                              type="number"
                                              value={Number.isFinite(value) ? String(value) : "0"}
                                              onChange={(event) => updateInteractiveStepScalar(step.uid, key, Number(event.target.value || 0))}
                                            />
                                          ) : (
                                            <input
                                              className="pipelinesInput"
                                              type="text"
                                              value={String(value)}
                                              onChange={(event) => updateInteractiveStepScalar(step.uid, key, event.target.value)}
                                            />
                                          )}
                                        </label>
                                      ))}
                                    </div>
                                  ) : null}

                                  <label className="pipelinesLabel">
                                    <span>Config JSON</span>
                                    <textarea
                                      className="pipelinesStepConfigTextarea"
                                      value={step.configText}
                                      onChange={(event) => updateInteractiveStep(step.uid, { configText: event.target.value })}
                                      spellCheck={false}
                                    />
                                  </label>

                                  {!configParsed.ok ? (
                                    <div className="pipelinesInlineError">Invalid config JSON: {configParsed.error}</div>
                                  ) : null}
                                </div>
                              ) : null}
                            </div>
                          );
                        })}

                        {interactiveSteps.length === 0 ? (
                          <div className="card">
                            <div className="cardBody">No steps yet. Add operators to build the pipeline chain.</div>
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ) : (
                    <div className="pipelinesMonacoWrap">
                      <Editor
                        height="520px"
                        language="json"
                        value={graphText}
                        onChange={(value) => setGraphText(String(value ?? ""))}
                        options={{
                          automaticLayout: true,
                          fontSize: 13,
                          minimap: { enabled: false },
                          scrollBeyondLastLine: false,
                          wordWrap: "on",
                        }}
                      />
                    </div>
                  )}
                </div>
              </div>

              {compileOutput ? (
                <div className="card">
                  <div className="cardTitle">Compile output</div>
                  <div className="cardBody">
                    <pre className="pipelinesPre">{JSON.stringify(compileOutput, null, 2)}</pre>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
