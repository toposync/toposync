import type {
  Pipeline,
  PipelineOperatorDefinition,
  PipelinePreviewFrameRequest,
} from "../../../../../util/api";
import { resolveToposyncUrl } from "@toposync/plugin-api";
import type { InteractiveStep } from "../../types";
import { buildGraphFromInteractiveSteps, isRecord, safeJsonParse } from "../../utils";

export type PipelineStepSnapshotKey = {
  pipelineName: string;
  nodeId: string;
  sourceId: string;
  filename: "input.png" | "input.jpg";
};

export type PipelineStepPreviewResolution =
  | { enabled: true; request: PipelinePreviewFrameRequest }
  | {
      enabled: false;
      reason: {
        code: "invalid_graph" | "no_camera_source" | "no_camera_selected" | "no_pipeline_name";
        detail?: string;
      };
    };

function normalizePipelineName(value: string): string {
  const raw = String(value || "").trim();
  if (raw.endsWith("__processing") && raw.length > "__processing".length) {
    return raw.slice(0, -1 * "__processing".length);
  }
  return raw;
}

function safeComponent(value: string, fallback: string, maxLen: number): string {
  const raw = String(value || "").trim() || fallback;
  const cleaned = raw.replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "");
  const normalized = (cleaned || fallback).slice(0, maxLen);
  return normalized || fallback;
}

function parseInteractiveStepConfig(step: InteractiveStep): Record<string, unknown> {
  const parsed = safeJsonParse(step.configText || "{}");
  if (!parsed.ok) return {};
  if (!isRecord(parsed.data)) return {};
  return parsed.data as Record<string, unknown>;
}

export function resolvePipelineStepSourceIdFromCameraSourceConfig(config: Record<string, unknown>): string | null {
  const cameraId = String((config as any).camera_id ?? "").trim();
  if (cameraId) return cameraId;
  const rtspUrl = String((config as any).rtsp_url ?? "").trim();
  if (!rtspUrl) return null;
  return "camera:adhoc";
}

export function buildPipelineStepSnapshotRelPath(key: PipelineStepSnapshotKey): string {
  const pipelineSafe = safeComponent(normalizePipelineName(key.pipelineName), "pipeline", 80);
  const nodeSafe = safeComponent(key.nodeId, "node", 80);
  const sourceSafe = safeComponent(key.sourceId, "source", 120);
  const filenameSafe = safeComponent(key.filename, key.filename, 80);
  return ["pipeline_snapshots", "v1", pipelineSafe, nodeSafe, sourceSafe, filenameSafe].join("/");
}

export function buildPipelineStepSnapshotUrl(key: PipelineStepSnapshotKey, nonce?: number): string {
  const relPath = buildPipelineStepSnapshotRelPath(key);
  const query = nonce ? `?t=${encodeURIComponent(String(nonce))}` : "";
  return resolveToposyncUrl(`/files/${relPath}${query}`);
}

export function buildPipelineStepPreviewRequest(
  steps: InteractiveStep[],
  currentIndex: number,
  pipelineName: string | null,
  nodeId: string,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): PipelineStepPreviewResolution {
  let sourceIndex = -1;
  for (let idx = currentIndex - 1; idx >= 0; idx -= 1) {
    if (steps[idx]?.operatorId === "camera.source") {
      sourceIndex = idx;
      break;
    }
  }
  if (sourceIndex < 0) {
    return { enabled: false, reason: { code: "no_camera_source" } };
  }

  const sourceConfig = parseInteractiveStepConfig(steps[sourceIndex]!);
  const sourceId = resolvePipelineStepSourceIdFromCameraSourceConfig(sourceConfig);
  if (!sourceId) {
    return { enabled: false, reason: { code: "no_camera_selected" } };
  }

  const safePipelineName = normalizePipelineName(String(pipelineName ?? "").trim());
  if (!safePipelineName) {
    return { enabled: false, reason: { code: "no_pipeline_name" } };
  }

  const built = buildGraphFromInteractiveSteps(steps.slice(0, currentIndex), operatorsById);
  if (!built.graph) {
    return {
      enabled: false,
      reason: {
        code: "invalid_graph",
        detail: built.error ?? "",
      },
    };
  }

  const previewPipeline: Pipeline = {
    name: `${safePipelineName}__editor_preview`,
    type: "final",
    enabled: false,
    processing_server_id: "local",
    editor_mode: "interactive",
    python_source: "",
    graph: built.graph,
  };
  return {
    enabled: true,
    request: {
      pipeline: previewPipeline,
      fallback_snapshot: {
        pipeline_name: safePipelineName,
        node_id: String(nodeId || "").trim() || "node",
        source_id: sourceId,
      },
      timeout_seconds: 12,
      format: "png",
      jpeg_quality: 85,
    },
  };
}
