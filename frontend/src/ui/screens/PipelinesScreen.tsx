import Editor from "@monaco-editor/react";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import Select, { type MultiValue, type SingleValue, type StylesConfig } from "react-select";
import CreatableSelect from "react-select/creatable";

import { i18n } from "../../util/i18n";
import type {
  CameraContextsResponse,
  CamerasIndexResponse,
  Pipeline,
  PipelineOperatorDefinition,
  ProcessingServer,
} from "../../util/api";
import {
  compilePipeline,
  createPipeline,
  deletePipeline,
  deleteProcessingServer,
  getCameraContexts,
  getPipelinesFeatureFlag,
  listCamerasIndex,
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
  showAdvanced: boolean;
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

type SelectOption = { value: string; label: string };

const OPERATOR_FRIENDLY_NAMES: Record<string, string> = {
  "camera.source": "Camera source",
  "camera.motion_gate": "Motion detection gate",
  "core.fps_reducer": "FPS reducer",
  "vision.object_tracking_yolo": "YOLO tracking",
  "vision.object_detection_yolo": "YOLO detection",
  "camera.object_segmentation": "Object segmentation",
  "camera.camera_mapping": "Camera mapping",
  "camera.area_restriction": "Area restriction",
  "camera.velocity_estimation": "Velocity estimation",
  "camera.best_frame_selector": "Best frame selector",
  "core.throttle": "Throttle",
  "core.debounce": "Debounce",
  "core.store_images": "Store images",
  "core.notify": "Notification",
};

const pipelinesReactSelectStyles: StylesConfig<SelectOption, boolean> = {
  container: (base) => ({ ...base }),
  control: (base, state) => ({
    ...base,
    minHeight: 34,
    borderRadius: 10,
    border: `1px solid ${state.isFocused ? "rgba(251,191,36,0.38)" : "rgba(255,255,255,0.12)"}`,
    backgroundColor: "rgba(15, 23, 48, 0.85)",
    boxShadow: state.isFocused ? "0 0 0 1px rgba(251,191,36,0.22)" : "none",
    cursor: "text",
  }),
  menu: (base) => ({
    ...base,
    backgroundColor: "rgba(12, 18, 37, 0.98)",
    border: "1px solid rgba(255,255,255,0.12)",
    borderRadius: 12,
    overflow: "hidden",
    boxShadow: "0 22px 70px rgba(0, 0, 0, 0.60)",
    zIndex: 50,
  }),
  option: (base, state) => ({
    ...base,
    backgroundColor: state.isSelected
      ? "rgba(251, 191, 36, 0.16)"
      : state.isFocused
        ? "rgba(255, 255, 255, 0.06)"
        : "transparent",
    color: "rgba(230,232,242,0.96)",
    cursor: "pointer",
  }),
  multiValue: (base) => ({
    ...base,
    backgroundColor: "rgba(251, 191, 36, 0.14)",
    border: "1px solid rgba(251, 191, 36, 0.22)",
    borderRadius: 999,
  }),
  multiValueLabel: (base) => ({ ...base, color: "rgba(255, 244, 210, 0.95)", fontWeight: 650 }),
  multiValueRemove: (base) => ({ ...base, color: "rgba(255, 244, 210, 0.85)" }),
  input: (base) => ({ ...base, color: "rgba(230,232,242,0.96)" }),
  placeholder: (base) => ({ ...base, color: "rgba(160,167,189,0.85)" }),
  singleValue: (base) => ({ ...base, color: "rgba(230,232,242,0.96)" }),
  indicatorSeparator: (base) => ({ ...base, backgroundColor: "rgba(255,255,255,0.10)" }),
  dropdownIndicator: (base) => ({ ...base, color: "rgba(160,167,189,0.9)" }),
  clearIndicator: (base) => ({ ...base, color: "rgba(160,167,189,0.9)" }),
};

const YOLO_CATEGORY_VALUES = [
  "person",
  "bicycle",
  "car",
  "motorcycle",
  "airplane",
  "bus",
  "train",
  "truck",
  "boat",
  "traffic light",
  "fire hydrant",
  "stop sign",
  "parking meter",
  "bench",
  "bird",
  "cat",
  "dog",
  "horse",
  "sheep",
  "cow",
  "elephant",
  "bear",
  "zebra",
  "giraffe",
  "backpack",
  "umbrella",
  "handbag",
  "tie",
  "suitcase",
  "frisbee",
  "skis",
  "snowboard",
  "sports ball",
  "kite",
  "baseball bat",
  "baseball glove",
  "skateboard",
  "surfboard",
  "tennis racket",
  "bottle",
  "wine glass",
  "cup",
  "fork",
  "knife",
  "spoon",
  "bowl",
  "banana",
  "apple",
  "sandwich",
  "orange",
  "broccoli",
  "carrot",
  "hot dog",
  "pizza",
  "donut",
  "cake",
  "chair",
  "couch",
  "potted plant",
  "bed",
  "dining table",
  "toilet",
  "tv",
  "laptop",
  "mouse",
  "remote",
  "keyboard",
  "cell phone",
  "microwave",
  "oven",
  "toaster",
  "sink",
  "refrigerator",
  "book",
  "clock",
  "vase",
  "scissors",
  "teddy bear",
  "hair drier",
  "toothbrush",
];

const YOLO_CATEGORY_OPTIONS: SelectOption[] = YOLO_CATEGORY_VALUES.map((value) => ({ value, label: value }));

const ARTIFACT_SUGGESTIONS: SelectOption[] = [
  { value: "frame_original", label: "Full frame" },
  { value: "best_frame", label: "Best frame" },
  { value: "segmented", label: "Segmented" },
  { value: "face", label: "Face" },
  { value: "pose", label: "Pose" },
];

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

const HUMANIZE_ACRONYMS: Record<string, string> = {
  id: "ID",
  fps: "FPS",
  rtsp: "RTSP",
  url: "URL",
  jpeg: "JPEG",
  png: "PNG",
  yolo: "YOLO",
  api: "API",
  ui: "UI",
  sse: "SSE",
  ts: "TS",
};

function humanizeIdentifier(raw: string): string {
  const normalized = String(raw || "").trim();
  if (!normalized) return "";

  const tokens = normalized
    .replace(/[.-]+/g, "_")
    .split("_")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);

  return tokens
    .map((token) => {
      const lower = token.toLowerCase();
      const acronym = HUMANIZE_ACRONYMS[lower];
      if (acronym) return acronym;
      if (!lower) return token;
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    })
    .join(" ");
}

function prettyConfigKeyLabel(key: string): string {
  const raw = String(key || "").trim();
  const lower = raw.toLowerCase();

  if (lower.endsWith("_seconds")) return `${humanizeIdentifier(raw.slice(0, -8))} (seconds)`;
  if (lower.endsWith("_ms")) return `${humanizeIdentifier(raw.slice(0, -3))} (ms)`;
  if (lower.endsWith("_kmh")) return `${humanizeIdentifier(raw.slice(0, -4))} (km/h)`;
  if (lower.endsWith("_mps")) return `${humanizeIdentifier(raw.slice(0, -4))} (m/s)`;

  return humanizeIdentifier(raw);
}

function prettyOperatorName(operatorId: string): string {
  const raw = String(operatorId || "").trim();
  if (!raw) return "";
  const fromMap = OPERATOR_FRIENDLY_NAMES[raw];
  if (fromMap) return fromMap;
  const tail = raw.split(".").pop() || raw;
  return humanizeIdentifier(tail) || raw;
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
    showAdvanced: false,
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
      showAdvanced: false,
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
  return prettyOperatorName(operator.id);
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
  const [camerasIndex, setCamerasIndex] = useState<CamerasIndexResponse>({ cameras: [] });
  const [cameraContextsById, setCameraContextsById] = useState<Record<string, CameraContextsResponse>>({});
  const [cameraContextsErrorById, setCameraContextsErrorById] = useState<Record<string, string>>({});

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

  const interactiveCameraId = useMemo(() => {
    const sourceStep = interactiveSteps.find((step) => step.operatorId === "camera.source");
    if (!sourceStep) return "";
    const parsed = safeJsonParse(sourceStep.configText || "{}");
    if (!parsed.ok) return "";
    if (!isRecord(parsed.data)) return "";
    return String((parsed.data as any).camera_id ?? "").trim();
  }, [interactiveSteps]);

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

  const activeCameraContexts = useMemo(() => {
    const cameraId = interactiveCameraId;
    if (!cameraId) return null;
    return cameraContextsById[cameraId] ?? null;
  }, [interactiveCameraId, cameraContextsById]);

  const activeCameraContextsError = useMemo(() => {
    const cameraId = interactiveCameraId;
    if (!cameraId) return null;
    return cameraContextsErrorById[cameraId] ?? null;
  }, [interactiveCameraId, cameraContextsErrorById]);

  const cameraAreaOptions = useMemo<SelectOption[]>(() => {
    const contexts = activeCameraContexts;
    if (!contexts) return [];
    const options: SelectOption[] = [];
    for (const composition of contexts.compositions ?? []) {
      for (const area of composition.areas ?? []) {
        options.push({
          value: `${composition.id}:${area.id}`,
          label: `${composition.name} / ${area.name}`,
        });
      }
    }
    options.sort((a, b) => a.label.localeCompare(b.label));
    return options;
  }, [activeCameraContexts]);

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
      const [flag, pipelineList, serverList, operatorList, cameras] = await Promise.all([
        getPipelinesFeatureFlag(),
        listPipelines(),
        listProcessingServers(),
        listPipelineOperators(),
        listCamerasIndex().catch(() => ({ cameras: [] })),
      ]);
      setFeatureFlag(Boolean(flag?.enabled));
      setPipelines(pipelineList);
      setServers(serverList);
      setOperators(operatorList);
      setCamerasIndex(cameras);
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

  useEffect(() => {
    if (mode !== "interactive") return;
    const cameraId = interactiveCameraId;
    if (!cameraId) return;
    if (cameraContextsById[cameraId]) return;
    if (cameraContextsErrorById[cameraId]) return;

    let cancelled = false;
    void (async () => {
      try {
        const contexts = await getCameraContexts(cameraId);
        if (cancelled) return;
        setCameraContextsById((prev) => ({ ...prev, [cameraId]: contexts }));
      } catch (err: any) {
        if (cancelled) return;
        setCameraContextsErrorById((prev) => ({ ...prev, [cameraId]: String(err?.message ?? err) }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode, interactiveCameraId, cameraContextsById, cameraContextsErrorById]);

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

  const updateInteractiveStepConfig = (uid: string, updater: (config: Record<string, unknown>) => Record<string, unknown>) => {
    setInteractiveSteps((prev) =>
      prev.map((step) => {
        if (step.uid !== uid) return step;
        const parsed = safeJsonParse(step.configText || "{}");
        const base = isRecord(parsed.ok ? parsed.data : null) ? { ...(parsed.data as Record<string, unknown>) } : {};
        const next = updater(base);
        return { ...step, configText: jsonPretty(next) };
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
              placeholder="Pipeline name"
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
	                          const configRecordOk = configParsed.ok && isRecord(configParsed.data);
	                          const config = configRecordOk ? (configParsed.data as Record<string, unknown>) : {};
	                          const configObjectError = !configParsed.ok
	                            ? `Invalid config JSON: ${configParsed.error}`
	                            : !configRecordOk
	                              ? "Config must be a JSON object."
	                              : null;

	                          const scalarEntries = Object.entries(config)
	                            .filter(([, value]) => {
	                              const valueType = typeof value;
	                              return valueType === "string" || valueType === "number" || valueType === "boolean";
	                            })
	                            .filter(([key]) => {
	                              if (step.operatorId === "core.notify") {
	                                return ![
	                                  "title",
	                                  "description",
	                                  "priority",
	                                  "realtime",
	                                  "update_interval_seconds",
	                                  "notification_type",
	                                  "dedupe_key_template",
	                                ].includes(key);
	                              }
	                              return true;
	                            });

	                          const isConfigScalarGridHidden =
	                            step.operatorId === "camera.source" ||
	                            step.operatorId === "camera.camera_mapping" ||
	                            step.operatorId === "camera.area_restriction" ||
	                            step.operatorId === "camera.velocity_estimation" ||
	                            step.operatorId === "core.throttle" ||
	                            step.operatorId === "core.debounce" ||
	                            step.operatorId === "core.notify" ||
	                            step.operatorId === "core.store_images" ||
	                            step.operatorId === "vision.object_tracking_yolo" ||
	                            step.operatorId === "vision.object_detection_yolo";
	                          const shouldShowScalarGrid = scalarEntries.length > 0 && (!isConfigScalarGridHidden || step.showAdvanced);

	                          const rowClass = ["pipelinesStepCard"];
	                          if (draggingStepUid === step.uid) rowClass.push("isDragSource");
	                          if (dragOverStep?.uid === step.uid) {
	                            rowClass.push(dragOverStep.position === "before" ? "isDropBefore" : "isDropAfter");
	                          }

	                          const cameraIdInConfig = String((config as any).camera_id ?? "").trim();
	                          const selectedCameraOption = cameraIdInConfig
	                            ? (cameraSelectOptionById.get(cameraIdInConfig) ?? { value: cameraIdInConfig, label: cameraIdInConfig })
	                            : null;

	                          const yoloCategoriesRaw = (config as any).categories;
	                          const yoloCategories = Array.isArray(yoloCategoriesRaw)
	                            ? yoloCategoriesRaw.map((value: any) => String(value || "").trim().toLowerCase()).filter((value: string) => value.length > 0)
	                            : [];

	                          const areaNamesRaw = (config as any).include_area_names;
	                          const selectedAreaKeys = Array.isArray(areaNamesRaw)
	                            ? areaNamesRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
	                            : [];
	                          const selectedAreaOptions = selectedAreaKeys.map((value) => cameraAreaOptions.find((option) => option.value === value) ?? { value, label: value });

	                          const storeImageArtifactsRaw = (config as any).artifact_names;
	                          const storeImageArtifactNames = Array.isArray(storeImageArtifactsRaw)
	                            ? storeImageArtifactsRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
	                            : [];
	                          const storeImageSelectedOptions = storeImageArtifactNames.map((value) => {
	                            const known = ARTIFACT_SUGGESTIONS.find((option) => option.value === value);
	                            return known ?? { value, label: humanizeIdentifier(value) || value };
	                          });

	                          const notifyFallbackRaw = (config as any).thumbnail_with_fallback;
	                          const notifyFallbackNames = Array.isArray(notifyFallbackRaw)
	                            ? notifyFallbackRaw.map((value: any) => String(value || "").trim()).filter((value: string) => value.length > 0)
	                            : [];
	                          const notifySelectedFallbackOptions = notifyFallbackNames.map((value) => {
	                            const known = ARTIFACT_SUGGESTIONS.find((option) => option.value === value);
	                            return known ?? { value, label: humanizeIdentifier(value) || value };
	                          });

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
	                                  aria-label={step.showAdvanced ? "Hide advanced config" : "Show advanced config"}
	                                  title={step.showAdvanced ? "Hide advanced" : "Advanced"}
	                                  onClick={() => updateInteractiveStep(step.uid, { showAdvanced: !step.showAdvanced })}
	                                >
	                                  <i className="fa-solid fa-gear" aria-hidden="true" />
	                                </button>

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
	                                  {operator ? (
	                                    <div className="pipelinesStepDescription">{operator.description || prettyOperatorName(operator.id)}</div>
	                                  ) : null}
	                                  {operator && operator.capabilities.length > 0 && step.showAdvanced ? (
	                                    <div className="pipelinesStepCapabilities">
	                                      caps: {operator.capabilities.map((cap) => humanizeIdentifier(cap) || cap).join(", ")}
	                                    </div>
	                                  ) : null}

	                                  {step.showAdvanced ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      <label className="pipelinesLabel">
	                                        <span>Step ID</span>
	                                        <input
	                                          className="pipelinesInput"
	                                          value={step.nodeId}
	                                          onChange={(event) => updateInteractiveStep(step.uid, { nodeId: event.target.value })}
	                                          placeholder="stepId"
	                                        />
	                                      </label>
	                                      <div className="pipelinesStepHint">Internal identifier used in storage paths, logs, and diagnostics.</div>
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "camera.source" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      <label className="pipelinesLabel">
	                                        <span>Camera</span>
	                                        <Select<SelectOption, false>
	                                          styles={pipelinesReactSelectStyles}
	                                          options={cameraSelectOptions}
	                                          value={selectedCameraOption}
	                                          isClearable
	                                          placeholder="Select a camera…"
	                                          onChange={(value: SingleValue<SelectOption>) => {
	                                            updateInteractiveStepConfig(step.uid, (prev) => {
	                                              const next = { ...prev };
	                                              next.camera_id = value?.value ?? "";
	                                              if (value?.value) {
	                                                next.rtsp_url = "";
	                                                next.username = "";
	                                                next.password = "";
	                                              }
	                                              return next;
	                                            });
	                                          }}
	                                        />
	                                      </label>
	                                      <div className="pipelinesStepHint">
	                                        RTSP URL, credentials, and FPS are inferred from the camera registry. Toggle Advanced to override.
	                                      </div>
	                                      {cameraSelectOptions.length === 0 ? (
	                                        <div className="pipelinesStepHint">No cameras found. Configure cameras in the Cameras extension settings.</div>
	                                      ) : null}
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "vision.object_tracking_yolo" || step.operatorId === "vision.object_detection_yolo" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      <label className="pipelinesLabel">
	                                        <span>Categories</span>
	                                        <CreatableSelect<SelectOption, true>
	                                          isMulti
	                                          styles={pipelinesReactSelectStyles}
	                                          options={YOLO_CATEGORY_OPTIONS}
	                                          value={yoloCategories.map((value) => YOLO_CATEGORY_OPTIONS.find((opt) => opt.value === value) ?? { value, label: value })}
	                                          placeholder="All categories"
	                                          onChange={(value: MultiValue<SelectOption>) => {
	                                            updateInteractiveStepConfig(step.uid, (prev) => ({
	                                              ...prev,
	                                              categories: value.map((item) => item.value),
	                                            }));
	                                          }}
	                                        />
	                                      </label>
	                                      <div className="pipelinesStepHint">Empty selection means “all categories”.</div>
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "camera.camera_mapping" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      <div className="pipelinesStepHint">
	                                        Uses control points defined in your compositions to map image → world coordinates. Configure control points in the Composition editor.
	                                      </div>
	                                      {!interactiveCameraId ? (
	                                        <div className="pipelinesInlineError">Select a camera in the Camera Source step to show mapping status.</div>
	                                      ) : activeCameraContexts ? (
	                                        <div className="pipelinesContextList">
	                                          {activeCameraContexts.compositions.map((composition) => {
	                                            const hasMapping = composition.camera_elements.some((element) => element.has_mapping);
	                                            const areasCount = composition.areas.length;
	                                            const elementNames = composition.camera_elements.map((item) => item.name).filter((value) => value.length > 0);
	                                            return (
	                                              <div key={composition.id} className="pipelinesContextRow">
	                                                <div className="pipelinesContextMain">
	                                                  <div className="pipelinesContextName">{composition.name}</div>
	                                                  <div className="pipelinesContextMeta">
	                                                    {hasMapping ? "mapping ready" : "missing mapping"}
	                                                    {areasCount ? ` • areas: ${areasCount}` : ""}
	                                                    {elementNames.length ? ` • camera nodes: ${elementNames.join(", ")}` : ""}
	                                                  </div>
	                                                </div>
	                                              </div>
	                                            );
	                                          })}
	                                        </div>
	                                      ) : activeCameraContextsError ? (
	                                        <div className="pipelinesInlineError">Failed to load camera contexts: {activeCameraContextsError}</div>
	                                      ) : (
	                                        <div className="pipelinesStepHint">Loading camera contexts…</div>
	                                      )}
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "camera.area_restriction" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      <label className="pipelinesLabel">
	                                        <span>Areas</span>
	                                        <Select<SelectOption, true>
	                                          isMulti
	                                          styles={pipelinesReactSelectStyles}
	                                          options={cameraAreaOptions}
	                                          value={selectedAreaOptions}
	                                          isDisabled={
	                                            !interactiveCameraId || !activeCameraContexts || Boolean(activeCameraContextsError) || cameraAreaOptions.length === 0
	                                          }
	                                          placeholder={!interactiveCameraId ? "Select a camera first…" : "Select areas…"}
	                                          onChange={(value: MultiValue<SelectOption>) => {
	                                            updateInteractiveStepConfig(step.uid, (prev) => ({
	                                              ...prev,
	                                              areas: [],
	                                              exclude_area_names: [],
	                                              include_area_names: value.map((item) => item.value),
	                                            }));
	                                          }}
	                                        />
	                                      </label>
	                                      {!interactiveCameraId ? (
	                                        <div className="pipelinesInlineError">Select a camera in the Camera Source step first.</div>
	                                      ) : activeCameraContextsError ? (
	                                        <div className="pipelinesInlineError">Failed to load camera contexts: {activeCameraContextsError}</div>
	                                      ) : !activeCameraContexts ? (
	                                        <div className="pipelinesStepHint">Loading camera contexts…</div>
	                                      ) : cameraAreaOptions.length === 0 ? (
	                                        <div className="pipelinesStepHint">No areas found in compositions for this camera.</div>
	                                      ) : (
	                                        <div className="pipelinesStepHint">Uses areas from the compositions where the selected camera is present.</div>
	                                      )}
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "camera.velocity_estimation" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      {(() => {
	                                        const modeRaw = String((config as any).filter_mode ?? "annotate").trim().toLowerCase() || "annotate";
	                                        const stoppedMpsRaw = Number((config as any).stopped_speed_threshold ?? 0.04);
	                                        const stoppedKmh = Number.isFinite(stoppedMpsRaw) ? stoppedMpsRaw * 3.6 : 0.0;
	                                        const hasMappingBefore = interactiveSteps.slice(0, index).some((item) => item.operatorId === "camera.camera_mapping");
	                                        const modeOptions: Array<{ value: string; label: string; hint: string }> = [
	                                          { value: "annotate", label: "Annotate only", hint: "Always emit packets; adds velocity payload." },
	                                          { value: "stopped_now", label: "Only when stopped", hint: "Emit packets only while the object is stopped." },
	                                          { value: "moving_now", label: "Only when moving", hint: "Emit packets only while the object is moving." },
	                                        ];
	                                        if (step.showAdvanced) {
	                                          modeOptions.push(
	                                            { value: "stopped_once", label: "Only after it stopped once", hint: "Drops packets until it stops at least once, then passes all." },
	                                            { value: "always_moving", label: "Only while it never stopped", hint: "Passes packets until it stops once, then drops the rest." },
	                                          );
	                                        }
	                                        const selected = modeOptions.find((item) => item.value === modeRaw) ?? modeOptions[0];

	                                        return (
	                                          <>
	                                            <label className="pipelinesLabel">
	                                              <span>Flow mode</span>
	                                              <select
	                                                className="pipelinesSelect"
	                                                value={selected.value}
	                                                onChange={(event) => {
	                                                  const nextMode = String(event.target.value || "annotate").trim().toLowerCase();
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, filter_mode: nextMode }));
	                                                }}
	                                              >
	                                                {modeOptions.map((item) => (
	                                                  <option key={item.value} value={item.value}>
	                                                    {item.label}
	                                                  </option>
	                                                ))}
	                                              </select>
	                                            </label>
	                                            <div className="pipelinesStepHint">{selected.hint}</div>

	                                            <label className="pipelinesLabel">
	                                              <span>Stopped threshold (km/h)</span>
	                                              <input
	                                                className="pipelinesInput"
	                                                type="number"
	                                                min={0}
	                                                max={4000}
	                                                step={0.05}
	                                                value={Number.isFinite(stoppedKmh) ? String(Math.max(0, stoppedKmh)) : "0"}
	                                                onChange={(event) => {
	                                                  const kmh = Number(event.target.value || 0);
	                                                  const mps = Number.isFinite(kmh) ? Math.max(0, kmh) / 3.6 : 0;
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, stopped_speed_threshold: mps }));
	                                                }}
	                                              />
	                                            </label>
	                                            <div className="pipelinesStepHint">
	                                              Computes speed from mapped world coordinates (Camera Mapping step). Uses m/s internally and also displays km/h.
	                                            </div>
	                                            {!hasMappingBefore ? (
	                                              <div className="pipelinesInlineError">Add Camera Mapping before this step to get world speed.</div>
	                                            ) : null}
	                                          </>
	                                        );
	                                      })()}
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "core.throttle" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      {(() => {
	                                        const intervalSeconds = Number((config as any).interval_seconds ?? 1.0);
	                                        const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
	                                        const keyFieldRaw = String((config as any).key_field ?? "stream_id").trim() || "stream_id";

	                                        return (
	                                          <>
	                                            <label className="pipelinesLabel">
	                                              <span>Interval (seconds)</span>
	                                              <input
	                                                className="pipelinesInput"
	                                                type="number"
	                                                min={0.01}
	                                                max={120}
	                                                step={0.05}
	                                                value={Number.isFinite(intervalSeconds) ? String(intervalSeconds) : "1.0"}
	                                                onChange={(event) => {
	                                                  const nextValue = Number(event.target.value || 1);
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({
	                                                    ...prev,
	                                                    interval_seconds: Number.isFinite(nextValue) ? nextValue : 1.0,
	                                                  }));
	                                                }}
	                                              />
	                                            </label>

	                                            <label className="pipelinesLabel">
	                                              <span>Mode</span>
	                                              <select
	                                                className="pipelinesSelect"
	                                                value={modeRaw}
	                                                onChange={(event) => {
	                                                  const nextMode = String(event.target.value || "first").trim().toLowerCase();
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, mode: nextMode }));
	                                                }}
	                                              >
	                                                <option value="first">First (recommended)</option>
	                                              </select>
	                                            </label>

	                                            {step.showAdvanced ? (
	                                              <label className="pipelinesLabel">
	                                                <span>Key</span>
	                                                <select
	                                                  className="pipelinesSelect"
	                                                  value={keyFieldRaw}
	                                                  onChange={(event) => {
	                                                    const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
	                                                    updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, key_field: nextKey }));
	                                                  }}
	                                                >
	                                                  <option value="stream_id">Stream (per object/camera)</option>
	                                                  <option value="payload.tracking_id">Tracking ID</option>
	                                                  <option value="payload.correlation_id">Correlation ID</option>
	                                                  <option value="payload.camera_id">Camera ID</option>
	                                                </select>
	                                              </label>
	                                            ) : null}

	                                            <div className="pipelinesStepHint">
	                                              Emits OPEN/CLOSE packets always. Mode “first” emits the first UPDATE in each interval window (keyed).
	                                            </div>
	                                          </>
	                                        );
	                                      })()}
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "core.debounce" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      {(() => {
	                                        const quietSeconds = Number((config as any).quiet_period_seconds ?? 1.0);
	                                        const modeRaw = String((config as any).mode ?? "first").trim().toLowerCase() || "first";
	                                        const keyFieldRaw = String((config as any).key_field ?? "stream_id").trim() || "stream_id";

	                                        return (
	                                          <>
	                                            <label className="pipelinesLabel">
	                                              <span>Quiet period (seconds)</span>
	                                              <input
	                                                className="pipelinesInput"
	                                                type="number"
	                                                min={0.01}
	                                                max={120}
	                                                step={0.05}
	                                                value={Number.isFinite(quietSeconds) ? String(quietSeconds) : "1.0"}
	                                                onChange={(event) => {
	                                                  const nextValue = Number(event.target.value || 1);
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({
	                                                    ...prev,
	                                                    quiet_period_seconds: Number.isFinite(nextValue) ? nextValue : 1.0,
	                                                  }));
	                                                }}
	                                              />
	                                            </label>

	                                            <label className="pipelinesLabel">
	                                              <span>Mode</span>
	                                              <select
	                                                className="pipelinesSelect"
	                                                value={modeRaw}
	                                                onChange={(event) => {
	                                                  const nextMode = String(event.target.value || "first").trim().toLowerCase();
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, mode: nextMode }));
	                                                }}
	                                              >
	                                                <option value="first">First (recommended)</option>
	                                              </select>
	                                            </label>

	                                            {step.showAdvanced ? (
	                                              <label className="pipelinesLabel">
	                                                <span>Key</span>
	                                                <select
	                                                  className="pipelinesSelect"
	                                                  value={keyFieldRaw}
	                                                  onChange={(event) => {
	                                                    const nextKey = String(event.target.value || "stream_id").trim() || "stream_id";
	                                                    updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, key_field: nextKey }));
	                                                  }}
	                                                >
	                                                  <option value="stream_id">Stream (per object/camera)</option>
	                                                  <option value="payload.tracking_id">Tracking ID</option>
	                                                  <option value="payload.correlation_id">Correlation ID</option>
	                                                  <option value="payload.camera_id">Camera ID</option>
	                                                </select>
	                                              </label>
	                                            ) : null}

	                                            <div className="pipelinesStepHint">
	                                              Emits OPEN/CLOSE packets always. Mode “first” emits the first UPDATE after an idle gap of at least the quiet period (keyed).
	                                            </div>
	                                          </>
	                                        );
	                                      })()}
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "core.store_images" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      <label className="pipelinesLabel">
	                                        <span>Artifacts to store</span>
	                                        <CreatableSelect<SelectOption, true>
	                                          isMulti
	                                          styles={pipelinesReactSelectStyles}
	                                          options={ARTIFACT_SUGGESTIONS}
	                                          value={storeImageSelectedOptions}
	                                          placeholder="Full frame (default)"
	                                          onChange={(value: MultiValue<SelectOption>) => {
	                                            updateInteractiveStepConfig(step.uid, (prev) => ({
	                                              ...prev,
	                                              artifact_names: value.map((item) => item.value),
	                                            }));
	                                          }}
	                                        />
	                                      </label>
	                                      <div className="pipelinesStepHint">
	                                        Stores image artifacts already present in the payload (default: Full frame). Type a custom artifact name to match future steps.
	                                      </div>
	                                    </div>
	                                  ) : null}

	                                  {step.operatorId === "core.notify" ? (
	                                    <div className="pipelinesOperatorConfigCard">
	                                      {(() => {
	                                        const title = String((config as any).title ?? "{{object_category_label}} detected");
	                                        const description = String((config as any).description ?? "");
	                                        const priorityRaw = String((config as any).priority ?? "medium").trim().toLowerCase() || "medium";
	                                        const realtime = Boolean((config as any).realtime ?? true);
	                                        const updateIntervalSeconds = Number((config as any).update_interval_seconds ?? 1.0);
	                                        const notificationType = String((config as any).notification_type ?? "pipelines.event");
	                                        const dedupeKeyTemplate = String((config as any).dedupe_key_template ?? "");
	                                        const priority = ["low", "medium", "high"].includes(priorityRaw) ? priorityRaw : "medium";

	                                        return (
	                                          <>
	                                            <label className="pipelinesLabel">
	                                              <span>Title</span>
	                                              <input
	                                                className="pipelinesInput"
	                                                type="text"
	                                                value={title}
	                                                placeholder="Person detected!"
	                                                onChange={(event) => {
	                                                  const nextTitle = String(event.target.value ?? "");
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, title: nextTitle }));
	                                                }}
	                                              />
	                                            </label>
	                                            <div className="pipelinesStepHint">
	                                              Supports templates like <code>{"{{object_category_label}}"}</code>, <code>{"{{area_label}}"}</code>,{" "}
	                                              <code>{"{{pose_label}}"}</code>.
	                                            </div>

	                                            <label className="pipelinesLabel">
	                                              <span>Description</span>
	                                              <textarea
	                                                className="pipelinesInputTextarea"
	                                                value={description}
	                                                placeholder="Front yard • Standing"
	                                                onChange={(event) => {
	                                                  const nextDescription = String(event.target.value ?? "");
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, description: nextDescription }));
	                                                }}
	                                              />
	                                            </label>

	                                            <label className="pipelinesLabel">
	                                              <span>Priority</span>
	                                              <select
	                                                className="pipelinesSelect"
	                                                value={priority}
	                                                onChange={(event) => {
	                                                  const nextPriority = String(event.target.value || "medium").trim().toLowerCase();
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, priority: nextPriority }));
	                                                }}
	                                              >
	                                                <option value="low">Low</option>
	                                                <option value="medium">Medium</option>
	                                                <option value="high">High</option>
	                                              </select>
	                                            </label>

	                                            <label className="pipelinesLabel">
	                                              <span>Realtime updates</span>
	                                              <input
	                                                type="checkbox"
	                                                checked={realtime}
	                                                onChange={(event) => {
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, realtime: event.target.checked }));
	                                                }}
	                                              />
	                                            </label>

	                                            <label className="pipelinesLabel">
	                                              <span>Update interval (seconds)</span>
	                                              <input
	                                                className="pipelinesInput"
	                                                type="number"
	                                                min={0}
	                                                max={60}
	                                                step={0.1}
	                                                value={Number.isFinite(updateIntervalSeconds) ? String(updateIntervalSeconds) : "1.0"}
	                                                onChange={(event) => {
	                                                  const nextValue = Number(event.target.value || 0);
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({
	                                                    ...prev,
	                                                    update_interval_seconds: Number.isFinite(nextValue) ? Math.max(0, Math.min(60, nextValue)) : 1.0,
	                                                  }));
	                                                }}
	                                              />
	                                            </label>
	                                            <div className="pipelinesStepHint">Avoids spamming UI updates while an event is open. Set to 0 to emit every change.</div>

	                                            <label className="pipelinesLabel">
	                                              <span>Thumbnail fallback</span>
	                                              <CreatableSelect<SelectOption, true>
	                                                isMulti
	                                                styles={pipelinesReactSelectStyles}
	                                                options={ARTIFACT_SUGGESTIONS}
	                                                value={notifySelectedFallbackOptions}
	                                                placeholder="Best frame → Face → Segmented → Full frame"
	                                                onChange={(value: MultiValue<SelectOption>) => {
	                                                  updateInteractiveStepConfig(step.uid, (prev) => ({
	                                                    ...prev,
	                                                    thumbnail_with_fallback: value.map((item) => item.value),
	                                                  }));
	                                                }}
	                                              />
	                                            </label>
	                                            <div className="pipelinesStepHint">
	                                              Registers notifications only (never stores images). To include images, add Store Images before this step.
	                                            </div>

	                                            {step.showAdvanced ? (
	                                              <>
	                                                <label className="pipelinesLabel">
	                                                  <span>Notification type</span>
	                                                  <input
	                                                    className="pipelinesInput"
	                                                    type="text"
	                                                    value={notificationType}
	                                                    placeholder="pipelines.event"
	                                                    onChange={(event) => {
	                                                      const nextType = String(event.target.value ?? "");
	                                                      updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, notification_type: nextType }));
	                                                    }}
	                                                  />
	                                                </label>

	                                                <label className="pipelinesLabel">
	                                                  <span>Dedupe key template</span>
	                                                  <input
	                                                    className="pipelinesInput"
	                                                    type="text"
	                                                    value={dedupeKeyTemplate}
	                                                    placeholder="Leave empty for default"
	                                                    onChange={(event) => {
	                                                      const nextValue = String(event.target.value ?? "");
	                                                      updateInteractiveStepConfig(step.uid, (prev) => ({ ...prev, dedupe_key_template: nextValue }));
	                                                    }}
	                                                  />
	                                                </label>
	                                                <div className="pipelinesStepHint">
	                                                  Use templates like <code>{"{{tracking_id}}"}</code>, <code>{"{{camera_id}}"}</code>,{" "}
	                                                  <code>{"{{object_category_label}}"}</code>.
	                                                </div>
	                                              </>
	                                            ) : null}
	                                          </>
	                                        );
	                                      })()}
	                                    </div>
	                                  ) : null}

	                                  {shouldShowScalarGrid ? (
	                                    <div className="pipelinesScalarGrid">
	                                      {scalarEntries.map(([key, value]) => (
	                                        <label key={`${step.uid}:${key}`} className="pipelinesLabel pipelinesScalarLabel">
	                                          <span>{prettyConfigKeyLabel(key)}</span>
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

	                                  {configObjectError ? <div className="pipelinesInlineError">{configObjectError}</div> : null}
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
