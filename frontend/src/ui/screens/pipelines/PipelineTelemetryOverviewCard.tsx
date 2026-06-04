import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import Select, { type MultiValue } from "react-select";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import {
  getPipelineTelemetryImageMarkers,
  getPipelineTelemetryNumeric,
  getPipelinesTelemetryImageMarkers,
  getPipelinesTelemetryNumericOverview,
  type PipelineTelemetryAggregateNumeric,
  type PipelineTelemetryImageMarker,
  type PipelineTelemetryNumeric,
} from "../../../util/api";
import { i18n } from "../../../util/i18n";
import { FullscreenImageViewer, requestFullscreenImageViewer, type FullscreenImageViewerItem } from "../../FullscreenImageViewer";
import { pipelinesReactSelectStyles } from "./constants";
import type { InteractiveStep, SelectOption } from "./types";

type MetricTarget = {
  seriesKey: string;
  nodeId: string;
  metricId: string;
  label: string;
};

type Props = {
  aggregate?: boolean;
  pipelineName: string | null;
  steps: InteractiveStep[];
  availablePipelines?: SelectOption[];
  externalRefreshNonce?: number;
  resetting?: boolean;
  onReset?: () => void | Promise<void>;
};

type MetricSeries = {
  seriesKey: string;
  nodeId: string;
  metricId: string;
  label: string;
  color: string;
  points: AggregatedPoint[];
  bucketSeconds: number;
};

type AggregatedPoint = {
  bucket_start_s: number;
  count: number;
  min: number;
  max: number;
  avg: number;
};

type TimelineSegment = {
  points: AggregatedPoint[];
  startS: number;
  endS: number;
  min: number;
  max: number;
};

type HoverTimelineSample = {
  seriesKey: string;
  metricId: string;
  label: string;
  color: string;
  bucketStartS: number;
  avg: number;
  min: number;
  max: number;
  y: number;
};

type HoverTimelineState = {
  chartX: number;
  cursorTs: number;
  samples: HoverTimelineSample[];
};

type TranslateFn = (key: string, params?: Record<string, unknown>, fallback?: string) => string;

type MarkerPoint = {
  marker: PipelineTelemetryImageMarker;
  x: number;
  score01: number | null;
  accentColor: string | null;
  zIndex: number;
};

type MarkerCluster = {
  key: string;
  markers: PipelineTelemetryImageMarker[];
  x: number;
  y: number;
  count: number;
  countLabel: string;
  score01: number | null;
  accentColor: string | null;
  zIndex: number;
  earliestTs: number;
  latestTs: number;
  visualWidth: number;
};

type EventColorAssignment = {
  color: string;
  textColor: string;
};

type EventColorAllocatorState = {
  colorsByKey: Map<string, EventColorAssignment>;
  nextIndexByPipeline: Map<string, number>;
};

type EventColorStyle = React.CSSProperties & {
  "--event-color": string;
  "--event-text-color": string;
};

function TelemetryMarkerImage({ src, alt }: { src: string; alt: string }): React.ReactElement {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="pipelinesTelemetryClusterTileMissing" aria-label={alt}>
        <i className="fa-regular fa-image" aria-hidden="true" />
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={alt}
      className="pipelinesTelemetryClusterTileImage"
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

const RANGE_SHORT_SECONDS = 2 * 60 * 60;
const RANGE_DEFAULT_SECONDS = 24 * 60 * 60;
const RANGE_LONG_SECONDS = 3 * 24 * 60 * 60;
const AGGREGATE_METRIC_IDS = ["motion.score", "onvif.gate_open", "vision.confidence", "ai.condition_filter.confidence"];
const MARKER_FETCH_LIMIT = 40_000;
const MARKER_CLUSTER_DISTANCE = 9;
const MARKER_CLUSTER_SPAN_LIMIT = 18;
const MARKER_CLUSTER_LANE_COUNT = 4;
const MARKER_CLUSTER_LANE_SPACING = 11;
const MARKER_CLUSTER_GAP = 5;
const MARKER_CLUSTER_PREVIEW_LIMIT = 96;
const METRIC_SERIES_COLORS = [
  "#4f9dff",
  "#ff7a59",
  "#34c98a",
  "#b06bff",
  "#ffb13d",
  "#ec5d8e",
  "#26c5d8",
  "#c9d23a",
  "#ff5e7e",
  "#7c8cff",
  "#52d3a4",
  "#f57aaa",
  "#36a3ff",
  "#ffd166",
  "#9a7bff",
  "#5ed4c6",
  "#ff8c42",
  "#a3e048",
  "#e26ad6",
  "#5b9aff",
  "#ff6b6b",
  "#74e09a",
  "#cc7aff",
  "#ffae73",
];
const EVENT_BADGE_COLORS = [
  "#2563eb",
  "#f97316",
  "#16a34a",
  "#dc2626",
  "#7c3aed",
  "#0891b2",
  "#ca8a04",
  "#db2777",
  "#0d9488",
  "#9333ea",
  "#65a30d",
  "#ea580c",
  "#0284c7",
  "#be123c",
  "#4f46e5",
  "#059669",
  "#c2410c",
  "#a21caf",
  "#1d4ed8",
  "#84cc16",
  "#e11d48",
  "#14b8a6",
  "#d97706",
  "#8b5cf6",
  "#15803d",
  "#f43f5e",
  "#0369a1",
  "#b45309",
  "#c026d3",
  "#22c55e",
  "#ef4444",
  "#06b6d4",
  "#a855f7",
  "#f59e0b",
  "#10b981",
  "#e879f9",
  "#1e40af",
  "#fb7185",
  "#0f766e",
  "#facc15",
  "#6d28d9",
  "#9f1239",
  "#38bdf8",
  "#4d7c0f",
  "#f472b6",
  "#4338ca",
  "#2dd4bf",
  "#fb923c",
];

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function metricLabel(metricId: string, t: TranslateFn): string {
  if (metricId === "motion.score") return t("core.ui.pipelines.telemetry.metric.motion_score", {}, "Motion score");
  if (metricId === "onvif.gate_open") return t("core.ui.pipelines.telemetry.metric.onvif_gate_open", {}, "ONVIF condition");
  if (metricId === "vision.confidence") return t("core.ui.pipelines.telemetry.metric.yolo_confidence", {}, "Vision confidence");
  if (metricId === "ai.condition_filter.confidence") return t("core.ui.pipelines.telemetry.metric.ai_condition_confidence", {}, "AI filter confidence");
  return metricId;
}

function metricAccentColor(index: number): string {
  return METRIC_SERIES_COLORS[((index % METRIC_SERIES_COLORS.length) + METRIC_SERIES_COLORS.length) % METRIC_SERIES_COLORS.length];
}

function buildMetricTargetLabel(target: { metricId: string; nodeId: string }, duplicateMetricIds: Set<string>, t: TranslateFn): string {
  const base = metricLabel(target.metricId, t);
  if (!duplicateMetricIds.has(target.metricId)) return base;
  return `${base} · ${target.nodeId}`;
}

function markerEventCode(marker: PipelineTelemetryImageMarker): string {
  return compactTrackedEventCode(String(marker.event_code || marker.event_id || marker.tracking_id || legacyMarkerEventCode(marker) || ""));
}

function compactTrackedEventCode(value: string): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (raw.startsWith("trk:")) {
    const tail = raw.split(":").filter(Boolean).pop();
    return tail || raw;
  }
  if (raw.startsWith("trk_")) {
    const tail = raw.split("_").filter(Boolean).pop();
    return tail || raw;
  }
  return raw;
}

function parseHexColor(value: string): { r: number; g: number; b: number } | null {
  const hex = String(value || "").trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return null;
  return {
    r: Number.parseInt(hex.slice(0, 2), 16),
    g: Number.parseInt(hex.slice(2, 4), 16),
    b: Number.parseInt(hex.slice(4, 6), 16),
  };
}

function relativeLuminance(color: string): number {
  const rgb = parseHexColor(color);
  if (!rgb) return 0;
  const channel = (value: number) => {
    const normalized = value / 255;
    return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(rgb.r) + 0.7152 * channel(rgb.g) + 0.0722 * channel(rgb.b);
}

function eventTextColor(color: string): string {
  return relativeLuminance(color) > 0.46 ? "#111827" : "#ffffff";
}

function eventColorPipelineKey(marker: PipelineTelemetryImageMarker, fallbackPipelineName: string | null): string {
  return String(marker.pipeline_name || fallbackPipelineName || "__pipeline__").trim() || "__pipeline__";
}

function eventColorKey(pipelineKey: string, eventCode: string): string {
  return `${pipelineKey}\u0000${eventCode}`;
}

function eventColorStyle(assignment: EventColorAssignment): EventColorStyle {
  return {
    "--event-color": assignment.color,
    "--event-text-color": assignment.textColor,
  };
}

function safeStoredFilenameComponent(value: string, maxLength: number): string {
  const cleaned = String(value || "")
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, "_")
    .replace(/^[._-]+|[._-]+$/g, "");
  return cleaned.slice(0, maxLength);
}

function legacyMarkerEventCode(marker: PipelineTelemetryImageMarker): string {
  const relPath = String(marker.rel_path || "").trim();
  const filename = relPath.split("/").pop() || "";
  const stem = filename.replace(/\.[^.]*$/, "");
  const parts = stem
    .split("__")
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length < 4) return "";

  const artifactName = safeStoredFilenameComponent(String(marker.image_key || ""), 32);
  const artifactCandidates = [artifactName, "main", "crop", "debug"].filter(Boolean);
  for (const candidate of artifactCandidates) {
    const index = parts.findIndex((part) => part === candidate);
    if (index >= 0 && index + 1 < parts.length) return parts[index + 1];
  }
  return "";
}

function aggregateMetricPoints(items: Array<Pick<PipelineTelemetryNumeric, "points">>): AggregatedPoint[] {
  const byBucket = new Map<number, { count: number; sum: number; min: number; max: number }>();
  for (const item of items) {
    const points = Array.isArray(item.points) ? item.points : [];
    for (const point of points) {
      const ts = Number(point.bucket_start_s || 0);
      if (!Number.isFinite(ts) || ts <= 0) continue;
      const count = Math.max(0, Number(point.count) || 0);
      if (count <= 0) continue;
      const avg = Number(point.avg || 0);
      const min = Number(point.min || 0);
      const max = Number(point.max || 0);
      const cur = byBucket.get(ts);
      if (!cur) {
        byBucket.set(ts, {
          count,
          sum: avg * count,
          min,
          max,
        });
        continue;
      }
      cur.count += count;
      cur.sum += avg * count;
      cur.min = Math.min(cur.min, min);
      cur.max = Math.max(cur.max, max);
    }
  }

  const out: AggregatedPoint[] = [];
  for (const [ts, item] of byBucket.entries()) {
    if (item.count <= 0) continue;
    out.push({
      bucket_start_s: ts,
      count: item.count,
      min: item.min,
      max: item.max,
      avg: item.sum / item.count,
    });
  }
  out.sort((a, b) => a.bucket_start_s - b.bucket_start_s);
  return out;
}

function buildLinePath(points: AggregatedPoint[], x: (ts: number) => number, y: (value01: number) => number, min: number, max: number): string {
  const span = Math.max(1e-9, max - min);
  return points
    .map((point, index) => {
      const nx = x(point.bucket_start_s);
      const ny = y((point.avg - min) / span);
      return `${index === 0 ? "M" : "L"} ${nx.toFixed(2)} ${ny.toFixed(2)}`;
    })
    .join(" ");
}

function buildBandPath(points: AggregatedPoint[], x: (ts: number) => number, y: (value01: number) => number, min: number, max: number): string {
  if (points.length < 2) return "";
  const span = Math.max(1e-9, max - min);
  const top = points
    .map((point, index) => {
      const nx = x(point.bucket_start_s);
      const ny = y((point.max - min) / span);
      return `${index === 0 ? "M" : "L"} ${nx.toFixed(2)} ${ny.toFixed(2)}`;
    })
    .join(" ");
  const bottom = points
    .slice()
    .reverse()
    .map((point) => {
      const nx = x(point.bucket_start_s);
      const ny = y((point.min - min) / span);
      return `L ${nx.toFixed(2)} ${ny.toFixed(2)}`;
    })
    .join(" ");
  return `${top} ${bottom} Z`;
}

function findNearestPoint(points: AggregatedPoint[], targetTs: number): AggregatedPoint | null {
  const len = points.length;
  if (len <= 0) return null;
  if (len === 1) return points[0];
  if (targetTs <= points[0].bucket_start_s) return points[0];
  if (targetTs >= points[len - 1].bucket_start_s) return points[len - 1];

  let low = 0;
  let high = len - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    const ts = points[mid].bucket_start_s;
    if (ts < targetTs) {
      low = mid + 1;
      continue;
    }
    if (ts > targetTs) {
      high = mid - 1;
      continue;
    }
    return points[mid];
  }

  const rightIndex = Math.max(0, Math.min(len - 1, low));
  const leftIndex = Math.max(0, rightIndex - 1);
  const left = points[leftIndex];
  const right = points[rightIndex];
  return Math.abs(left.bucket_start_s - targetTs) <= Math.abs(right.bucket_start_s - targetTs) ? left : right;
}

function splitIntoSegments(points: AggregatedPoint[], bucketSeconds: number): AggregatedPoint[][] {
  if (points.length <= 0) return [];
  const out: AggregatedPoint[][] = [];
  const gapThreshold = bucketSeconds > 0 ? bucketSeconds * 1.5 : 0;
  let cur: AggregatedPoint[] = [];

  for (const point of points) {
    if (cur.length === 0) {
      cur = [point];
      continue;
    }
    const prev = cur[cur.length - 1];
    const dt = Number(point.bucket_start_s) - Number(prev.bucket_start_s);
    if (gapThreshold > 0 && dt > gapThreshold) {
      out.push(cur);
      cur = [point];
      continue;
    }
    cur.push(point);
  }
  if (cur.length) out.push(cur);
  return out;
}

function segmentRangeFromZero(points: AggregatedPoint[]): { min: number; max: number } {
  if (points.length <= 0) return { min: 0, max: 0 };
  const max = Math.max(0, ...points.map((point) => point.max));
  return { min: 0, max };
}

function findSegmentAtTs(segments: TimelineSegment[], ts: number): TimelineSegment | null {
  for (const segment of segments) {
    if (ts >= segment.startS && ts <= segment.endS) return segment;
  }
  return null;
}

function buildMarkerClusterKey(items: MarkerPoint[]): string {
  const first = items[0]?.marker;
  const last = items[items.length - 1]?.marker;
  return [
    String(first?.rel_path || ""),
    String(first?.ts || 0),
    String(last?.rel_path || ""),
    String(last?.ts || 0),
    String(items.length),
  ].join("|");
}

function estimateClusterVisualWidth(count: number): number {
  if (count <= 1) return 10;
  return Math.max(16, Math.min(24, 12 + Math.log2(count) * 3.2));
}

function markerClusterCountLabel(count: number): string {
  if (count <= 1) return "";
  if (count > 99) return "99+";
  return String(count);
}

function isAbortError(err: unknown): boolean {
  return (
    typeof DOMException !== "undefined" &&
    err instanceof DOMException &&
    err.name === "AbortError"
  );
}

function buildMarkerClusters(points: MarkerPoint[], options: { baseY: number }): MarkerCluster[] {
  const { baseY } = options;
  if (points.length <= 0) return [];

  const sorted = points.slice().sort((a, b) => {
    const dx = a.x - b.x;
    if (Math.abs(dx) > 0.001) return dx;
    return Number(a.marker.ts || 0) - Number(b.marker.ts || 0);
  });

  const grouped: MarkerPoint[][] = [];
  let current: MarkerPoint[] = [];
  for (const point of sorted) {
    const prev = current[current.length - 1];
    const first = current[0];
    const withinDistance = !prev || (point.x - prev.x) <= MARKER_CLUSTER_DISTANCE;
    const withinSpan = !first || (point.x - first.x) <= MARKER_CLUSTER_SPAN_LIMIT;
    if (!prev || (withinDistance && withinSpan)) {
      current.push(point);
      continue;
    }
    grouped.push(current);
    current = [point];
  }
  if (current.length) grouped.push(current);

  const laneRightEdges = Array.from({ length: MARKER_CLUSTER_LANE_COUNT }, () => Number.NEGATIVE_INFINITY);
  return grouped.map((group) => {
    const markers = group
      .slice()
      .sort((a, b) => Number(b.marker.ts || 0) - Number(a.marker.ts || 0))
      .map((item) => item.marker);
    const x = group.reduce((sum, item) => sum + item.x, 0) / group.length;
    const score01 = group.reduce<number | null>((best, item) => {
      if (item.score01 == null) return best;
      if (best == null) return item.score01;
      return Math.max(best, item.score01);
    }, null);
    const accentSource = group.reduce<MarkerPoint | null>((best, item) => {
      if (!item.accentColor) return best;
      if (!best) return item;
      return (item.score01 ?? -1) > (best.score01 ?? -1) ? item : best;
    }, null);
    const earliestTs = group.reduce((min, item) => Math.min(min, Number(item.marker.ts || 0)), Number.POSITIVE_INFINITY);
    const latestTs = group.reduce((max, item) => Math.max(max, Number(item.marker.ts || 0)), 0);
    const visualWidth = estimateClusterVisualWidth(markers.length);
    const halfWidth = visualWidth / 2;

    let laneIndex = laneRightEdges.findIndex((edge) => (x - halfWidth) >= (edge + MARKER_CLUSTER_GAP));
    if (laneIndex < 0) {
      laneIndex = laneRightEdges.reduce((bestIndex, edge, index, all) => (edge < all[bestIndex] ? index : bestIndex), 0);
    }
    laneRightEdges[laneIndex] = x + halfWidth;

    return {
      key: buildMarkerClusterKey(group),
      markers,
      x,
      y: baseY + laneIndex * MARKER_CLUSTER_LANE_SPACING,
      count: markers.length,
      countLabel: markerClusterCountLabel(markers.length),
      score01,
      accentColor: accentSource?.accentColor ?? null,
      zIndex: Math.max(...group.map((item) => item.zIndex)) + markers.length,
      earliestTs: Number.isFinite(earliestTs) ? earliestTs : 0,
      latestTs,
      visualWidth,
    } satisfies MarkerCluster;
  });
}

export function PipelineTelemetryOverviewCard({
  aggregate,
  pipelineName,
  steps,
  availablePipelines,
  externalRefreshNonce,
  resetting,
  onReset,
}: Props): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const isAggregate = aggregate === true;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [series, setSeries] = useState<MetricSeries[]>([]);
  const [markers, setMarkers] = useState<PipelineTelemetryImageMarker[]>([]);
  const [loadingInBackground, setLoadingInBackground] = useState(false);
  const [rangeSeconds, setRangeSeconds] = useState(RANGE_DEFAULT_SECONDS);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [hoveredClusterKey, setHoveredClusterKey] = useState<string | null>(null);
  const [pinnedClusterKey, setPinnedClusterKey] = useState<string | null>(null);
  const [hoverTimeline, setHoverTimeline] = useState<HoverTimelineState | null>(null);
  const [hiddenSeriesKeys, setHiddenSeriesKeys] = useState<Set<string>>(() => new Set());
  const [fullscreenImageOpen, setFullscreenImageOpen] = useState(false);
  const [fullscreenImageItems, setFullscreenImageItems] = useState<FullscreenImageViewerItem[]>([]);
  const [fullscreenImageIndex, setFullscreenImageIndex] = useState(0);
  const latestSuccessfulTelemetryRequestKeyRef = useRef<string | null>(null);
  const hasTelemetryContentRef = useRef(false);
  const eventColorAllocatorRef = useRef<EventColorAllocatorState>({
    colorsByKey: new Map(),
    nextIndexByPipeline: new Map(),
  });
  const [eventColorRevision, setEventColorRevision] = useState(0);
  const pipelineOptions = useMemo(() => {
    const items = Array.isArray(availablePipelines) ? availablePipelines : [];
    const next = items
      .map((item) => {
        const value = String(item?.value || "").trim();
        const label = String(item?.label || "").trim();
        if (!value) return null;
        return { value, label: label || value };
      })
      .filter(Boolean) as SelectOption[];
    next.sort((a, b) => a.label.localeCompare(b.label));
    return next;
  }, [availablePipelines]);
  const [selectedPipelineOptions, setSelectedPipelineOptions] = useState<SelectOption[]>([]);
  const decimalFormatter = useMemo(
    () =>
      new Intl.NumberFormat(locale, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 4,
      }),
    [locale],
  );
  const timeFormatter = useMemo(
    () =>
      new Intl.DateTimeFormat(locale, {
        dateStyle: "short",
        timeStyle: "medium",
      }),
    [locale],
  );
  const timeOnlyFormatter = useMemo(
    () =>
      new Intl.DateTimeFormat(locale, {
        timeStyle: "medium",
      }),
    [locale],
  );
  const buildMarkerImageViewerItem = useCallback(
    (marker: PipelineTelemetryImageMarker): FullscreenImageViewerItem => {
      const relPath = String(marker.rel_path || "");
      const label = markerEventCode(marker);
      const metaParts: string[] = [];
      if (marker.size_bytes) metaParts.push(`${Math.round(Number(marker.size_bytes || 0) / 1024)} KB`);
      metaParts.push(timeFormatter.format(new Date(Number(marker.ts || 0) * 1000)));
      return {
        id: `${relPath}|${marker.ts}|${marker.node_id}`,
        url: resolveToposyncUrl(`/files/${encodeURI(relPath)}`),
        label: label || t("core.ui.pipelines.telemetry.overview.images", {}, "Stored images"),
        meta: metaParts.join(" • "),
      };
    },
    [t, timeFormatter],
  );
  useEffect(() => {
    const allocator = eventColorAllocatorRef.current;
    let changed = false;
    for (const marker of markers) {
      const eventCode = markerEventCode(marker);
      if (!eventCode) continue;
      const pipelineKey = eventColorPipelineKey(marker, pipelineName);
      const key = eventColorKey(pipelineKey, eventCode);
      if (allocator.colorsByKey.has(key)) continue;
      const nextIndex = allocator.nextIndexByPipeline.get(pipelineKey) ?? 0;
      const colorIndex =
        ((nextIndex % EVENT_BADGE_COLORS.length) + EVENT_BADGE_COLORS.length) %
        EVENT_BADGE_COLORS.length;
      const color = EVENT_BADGE_COLORS[colorIndex];
      allocator.colorsByKey.set(key, {
        color,
        textColor: eventTextColor(color),
      });
      allocator.nextIndexByPipeline.set(pipelineKey, (nextIndex + 1) % EVENT_BADGE_COLORS.length);
      changed = true;
    }
    if (changed) setEventColorRevision((value) => value + 1);
  }, [markers, pipelineName]);
  const eventColorStyleForMarker = useCallback(
    (marker: PipelineTelemetryImageMarker, eventCode: string): EventColorStyle | undefined => {
      const code = String(eventCode || "").trim();
      if (!code) return undefined;
      const pipelineKey = eventColorPipelineKey(marker, pipelineName);
      const assignment = eventColorAllocatorRef.current.colorsByKey.get(eventColorKey(pipelineKey, code));
      return assignment ? eventColorStyle(assignment) : undefined;
    },
    [eventColorRevision, pipelineName],
  );
  const openFullscreenMarkerImages = useCallback((items: FullscreenImageViewerItem[], index: number) => {
    requestFullscreenImageViewer();
    setFullscreenImageItems(items);
    setFullscreenImageIndex(index);
    setFullscreenImageOpen(true);
  }, []);
  const metricTargets = useMemo(() => {
    if (isAggregate) {
      return AGGREGATE_METRIC_IDS.map((metricId) => ({
        seriesKey: `aggregate:${metricId}`,
        nodeId: "__aggregate__",
        metricId,
        label: metricLabel(metricId, t),
      })) satisfies MetricTarget[];
    }
    if (!pipelineName) return [];
    const unique = new Map<string, { nodeId: string; metricId: string }>();
    for (const step of steps) {
      if (
        step.operatorId === "camera.motion_gate" ||
        step.operatorId === "camera.motion_bgsub_adaptive" ||
        step.operatorId === "camera.motion_sample_bg"
      ) {
        const item = { nodeId: step.nodeId, metricId: "motion.score" };
        unique.set(`${item.metricId}:${item.nodeId}`, item);
      }
      if (step.operatorId === "camera.onvif_state_gate") {
        const item = { nodeId: step.nodeId, metricId: "onvif.gate_open" };
        unique.set(`${item.metricId}:${item.nodeId}`, item);
      }
      if (
        step.operatorId === "vision.track" ||
        step.operatorId === "vision.classify_image" ||
        step.operatorId === "vision.detect" ||
        step.operatorId === "vision.segment_instances"
      ) {
        const item = { nodeId: step.nodeId, metricId: "vision.confidence" };
        unique.set(`${item.metricId}:${item.nodeId}`, item);
      }
      if (step.operatorId === "ai.condition_filter") {
        const item = { nodeId: step.nodeId, metricId: "ai.condition_filter.confidence" };
        unique.set(`${item.metricId}:${item.nodeId}`, item);
      }
    }
    const baseTargets = Array.from(unique.values());
    const countsByMetric = new Map<string, number>();
    for (const item of baseTargets) {
      countsByMetric.set(item.metricId, (countsByMetric.get(item.metricId) ?? 0) + 1);
    }
    const duplicateMetricIds = new Set(
      Array.from(countsByMetric.entries())
        .filter(([, count]) => count > 1)
        .map(([metricId]) => metricId),
    );
    return baseTargets.map((item) => ({
      seriesKey: `${item.metricId}:${item.nodeId}`,
      nodeId: item.nodeId,
      metricId: item.metricId,
      label: buildMetricTargetLabel(item, duplicateMetricIds, t),
    })) satisfies MetricTarget[];
  }, [isAggregate, pipelineName, steps, t]);

  const metricTargetsKey = useMemo(
    () => metricTargets.map((item) => item.seriesKey).join("|"),
    [metricTargets],
  );
  const selectedPipelineNames = useMemo(
    () => selectedPipelineOptions.map((item) => item.value),
    [selectedPipelineOptions],
  );
  const selectedPipelineNamesKey = useMemo(
    () => selectedPipelineNames.join("|"),
    [selectedPipelineNames],
  );
  const telemetryRequestKey = useMemo(
    () =>
      JSON.stringify({
        aggregate: isAggregate,
        metricTargetsKey,
        pipelineName: pipelineName ?? "",
        rangeSeconds,
        selectedPipelineNamesKey,
      }),
    [isAggregate, metricTargetsKey, pipelineName, rangeSeconds, selectedPipelineNamesKey],
  );

  useEffect(() => {
    if (!isAggregate) {
      setSelectedPipelineOptions([]);
      return;
    }
    if (!pipelineOptions.length) {
      setSelectedPipelineOptions([]);
      return;
    }
    setSelectedPipelineOptions((prev) => {
      if (prev.length === 0) return pipelineOptions;
      const optionByValue = new Map(pipelineOptions.map((item) => [item.value, item]));
      const preserved = prev
        .map((item) => optionByValue.get(item.value))
        .filter(Boolean) as SelectOption[];
      const hadAllSelected = preserved.length === pipelineOptions.length && prev.length === pipelineOptions.length;
      return hadAllSelected ? pipelineOptions : preserved;
    });
  }, [isAggregate, pipelineOptions]);

  useEffect(() => {
    if (isAggregate && pipelineOptions.length > 0 && selectedPipelineNames.length === 0) {
      setSeries([]);
      setMarkers([]);
      setError(null);
      setLastUpdatedAt(null);
      setLoading(false);
      setLoadingInBackground(false);
      return;
    }
    if (!isAggregate && !pipelineName) {
      setSeries([]);
      setMarkers([]);
      setError(null);
      setLastUpdatedAt(null);
      setLoading(false);
      setLoadingInBackground(false);
      return;
    }

    const controller = new AbortController();
    const shouldLoadInBackground =
      latestSuccessfulTelemetryRequestKeyRef.current === telemetryRequestKey && hasTelemetryContentRef.current;
    setLoadingInBackground(shouldLoadInBackground);
    setLoading(true);
    setError(null);

    const run = async () => {
      try {
        const pointLimit = 5000;
        const [numericResponses, markerResponse] = isAggregate
          ? await Promise.all([
              getPipelinesTelemetryNumericOverview({
                metricIds: metricTargets.map((target) => target.metricId),
                pipelineNames: selectedPipelineNames,
                pointLimit,
                windowSeconds: rangeSeconds,
                aggregation: "max",
                signal: controller.signal,
              }).then((response) => response.series),
              getPipelinesTelemetryImageMarkers({
                metricId: "store.image",
                limit: MARKER_FETCH_LIMIT,
                pipelineNames: selectedPipelineNames,
                windowSeconds: rangeSeconds,
                aggregation: "max",
                signal: controller.signal,
              }),
            ])
          : await Promise.all([
              Promise.all(
                metricTargets.map((target) =>
                  getPipelineTelemetryNumeric(
                    String(pipelineName || ""),
                    target.nodeId,
                    target.metricId,
                    pointLimit,
                    rangeSeconds,
                    controller.signal,
                  ),
                ),
              ),
              getPipelineTelemetryImageMarkers(String(pipelineName || ""), {
                metricId: "store.image",
                limit: MARKER_FETCH_LIMIT,
                windowSeconds: rangeSeconds,
                signal: controller.signal,
              }),
            ]);
        if (controller.signal.aborted) return;

        const nextSeries: MetricSeries[] = [];
        for (const [index, item] of numericResponses.entries()) {
          const target = metricTargets[index];
          if (!target) continue;
          const points = isAggregate ? aggregateMetricPoints([item as PipelineTelemetryAggregateNumeric]) : aggregateMetricPoints([item as PipelineTelemetryNumeric]);
          if (points.length === 0) continue;
          const bucketSeconds = Math.max(0, Number(item?.bucket_seconds ?? 0));
          nextSeries.push({
            seriesKey: target.seriesKey,
            nodeId: target.nodeId,
            metricId: target.metricId,
            label: target.label,
            color: metricAccentColor(index),
            points,
            bucketSeconds,
          });
        }

        setSeries(nextSeries);
        setMarkers(Array.isArray(markerResponse.markers) ? markerResponse.markers : []);
        latestSuccessfulTelemetryRequestKeyRef.current = telemetryRequestKey;
        setLastUpdatedAt(Date.now());
      } catch (err: any) {
        if (controller.signal.aborted || isAbortError(err)) return;
        if (!shouldLoadInBackground) {
          latestSuccessfulTelemetryRequestKeyRef.current = null;
          setSeries([]);
          setMarkers([]);
        }
        setError(String(err?.message ?? err));
      } finally {
        if (controller.signal.aborted) return;
        setLoading(false);
        setLoadingInBackground(false);
      }
    };

    void run();
    return () => {
      controller.abort();
    };
  }, [
    externalRefreshNonce,
    isAggregate,
    metricTargets,
    metricTargetsKey,
    pipelineName,
    pipelineOptions.length,
    rangeSeconds,
    refreshNonce,
    selectedPipelineNames,
    selectedPipelineNamesKey,
    telemetryRequestKey,
  ]);

  useEffect(() => {
    setPinnedClusterKey(null);
    setHoveredClusterKey(null);
    setHoverTimeline(null);
  }, [isAggregate, pipelineName, markers.length, rangeSeconds]);

  useEffect(() => {
    setHiddenSeriesKeys((prev) => {
      if (prev.size === 0) return prev;
      const valid = new Set(series.map((item) => item.seriesKey));
      let changed = false;
      const next = new Set<string>();
      for (const key of prev) {
        if (valid.has(key)) {
          next.add(key);
        } else {
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [series]);

  const toggleSeriesHidden = (seriesKey: string) => {
    setHiddenSeriesKeys((prev) => {
      const next = new Set(prev);
      if (next.has(seriesKey)) next.delete(seriesKey);
      else next.add(seriesKey);
      return next;
    });
  };

  const visibleSeries = useMemo(
    () => series.filter((item) => !hiddenSeriesKeys.has(item.seriesKey)),
    [series, hiddenSeriesKeys],
  );

  const latestDataS = useMemo(() => {
    let latest = 0;
    for (const item of series) {
      const lastPoint = item.points[item.points.length - 1];
      if (!lastPoint) continue;
      latest = Math.max(latest, Number(lastPoint.bucket_start_s || 0) + Math.max(0, Number(item.bucketSeconds || 0)));
    }
    for (const marker of markers) {
      latest = Math.max(latest, Number(marker.ts || 0));
    }
    return latest;
  }, [series, markers]);

  const axisEndS = useMemo(() => {
    const baseMs = lastUpdatedAt ?? Date.now();
    return Math.max(Math.floor(baseMs / 1000), Math.ceil(latestDataS));
  }, [lastUpdatedAt, latestDataS]);
  const xMin = useMemo(() => Math.max(0, axisEndS - rangeSeconds), [axisEndS, rangeSeconds]);
  const xMax = axisEndS;

  const chartWidth = 940;
  const chartHeight = 320;
  const paddingLeft = 16;
  const paddingRight = 16;
  const paddingTop = 18;
  const paddingBottom = 34;
  const innerWidth = chartWidth - paddingLeft - paddingRight;
  const innerHeight = chartHeight - paddingTop - paddingBottom;

  const xScale = (ts: number) => {
    const span = Math.max(1, xMax - xMin);
    return paddingLeft + ((ts - xMin) / span) * innerWidth;
  };
  const yScale = (value01: number) => paddingTop + (1 - Math.max(0, Math.min(1, value01))) * innerHeight;

  const timelineSegmentsBySeries = useMemo(() => {
    const out = new Map<string, TimelineSegment[]>();
    for (const item of visibleSeries) {
      const segments = splitIntoSegments(item.points, item.bucketSeconds).map((points) => {
        const range = segmentRangeFromZero(points);
        return {
          points,
          startS: points[0]?.bucket_start_s ?? 0,
          endS: points[points.length - 1]?.bucket_start_s ?? 0,
          min: range.min,
          max: range.max,
        };
      });
      out.set(item.seriesKey, segments);
    }
    return out;
  }, [visibleSeries]);

  const markerPoints: MarkerPoint[] = useMemo(() => {
    if (!markers.length || xMax <= xMin) return [];
    return markers
      .map((marker) => {
        const ts = Number(marker.ts || 0);
        if (!Number.isFinite(ts) || ts < xMin || ts > xMax) return null;
        const x = xScale(ts);
        let foundMetricValue = false;
        let bestMetricValue = -1;
        let bestSeriesKey: string | null = null;
        let combined = 0;
        for (const metric of series) {
          const nearest = findNearestPoint(metric.points, ts);
          if (!nearest) continue;
          if (metric.bucketSeconds > 0) {
            const distance = Math.abs(Number(nearest.bucket_start_s) - ts);
            if (distance > metric.bucketSeconds * 0.9) continue;
          }
          const value01 = clamp01(Number(nearest.avg));
          foundMetricValue = true;
          combined = Math.max(combined, value01);
          if (value01 > bestMetricValue) {
            bestMetricValue = value01;
            bestSeriesKey = metric.seriesKey;
          }
        }

        const confidenceValue = marker.confidence == null ? null : clamp01(Number(marker.confidence));
        const score01 = foundMetricValue ? combined : confidenceValue;
        const accentColor = bestSeriesKey ? (series.find((item) => item.seriesKey === bestSeriesKey)?.color ?? null) : null;
        const zIndexScore = Math.round((score01 ?? 0) * 1000);
        const zIndexAge = Math.round(((ts - xMin) / Math.max(1, xMax - xMin)) * 80);

        return {
          marker,
          x,
          score01,
          accentColor,
          zIndex: 10 + zIndexScore + zIndexAge,
        } satisfies MarkerPoint;
      })
      .filter(Boolean) as MarkerPoint[];
  }, [markers, series, xMin, xMax, rangeSeconds]);

  const markerClusters = useMemo(
    () => buildMarkerClusters(markerPoints, { baseY: paddingTop + innerHeight + 6 }),
    [markerPoints, paddingTop, innerHeight],
  );
  const hasTelemetryContent = series.length > 0 || markerClusters.length > 0;
  const showTelemetryContent = hasTelemetryContent && (!loading || loadingInBackground || Boolean(error));
  const showLoadingHint = loading && !showTelemetryContent;
  const showNoTelemetryData = !loading && !error && !hasTelemetryContent;

  useEffect(() => {
    hasTelemetryContentRef.current = hasTelemetryContent;
  }, [hasTelemetryContent]);

  const markerClustersByKey = useMemo(
    () => new Map(markerClusters.map((cluster) => [cluster.key, cluster])),
    [markerClusters],
  );

  useEffect(() => {
    if (hoveredClusterKey && !markerClustersByKey.has(hoveredClusterKey)) setHoveredClusterKey(null);
    if (pinnedClusterKey && !markerClustersByKey.has(pinnedClusterKey)) setPinnedClusterKey(null);
  }, [markerClustersByKey, hoveredClusterKey, pinnedClusterKey]);

  const activeCluster = useMemo(() => {
    if (pinnedClusterKey) return markerClustersByKey.get(pinnedClusterKey) ?? null;
    if (hoveredClusterKey) return markerClustersByKey.get(hoveredClusterKey) ?? null;
    return null;
  }, [markerClustersByKey, pinnedClusterKey, hoveredClusterKey]);

  const sortedClusters = useMemo(
    () => markerClusters.slice().sort((a, b) => a.earliestTs - b.earliestTs),
    [markerClusters],
  );
  const pinnedClusterIndex = useMemo(() => {
    if (!pinnedClusterKey) return -1;
    return sortedClusters.findIndex((cluster) => cluster.key === pinnedClusterKey);
  }, [sortedClusters, pinnedClusterKey]);
  const hasPrevCluster = pinnedClusterIndex > 0;
  const hasNextCluster = pinnedClusterIndex >= 0 && pinnedClusterIndex < sortedClusters.length - 1;
  const goToCluster = (offset: number) => {
    if (pinnedClusterIndex < 0) return;
    const next = sortedClusters[pinnedClusterIndex + offset];
    if (!next) return;
    setPinnedClusterKey(next.key);
  };

  useEffect(() => {
    if (!pinnedClusterKey) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPinnedClusterKey(null);
        return;
      }
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || (target && target.isContentEditable)) return;
      if (event.key === "ArrowLeft") {
        if (pinnedClusterIndex > 0) {
          event.preventDefault();
          const prev = sortedClusters[pinnedClusterIndex - 1];
          if (prev) setPinnedClusterKey(prev.key);
        }
        return;
      }
      if (event.key === "ArrowRight") {
        if (pinnedClusterIndex >= 0 && pinnedClusterIndex < sortedClusters.length - 1) {
          event.preventDefault();
          const next = sortedClusters[pinnedClusterIndex + 1];
          if (next) setPinnedClusterKey(next.key);
        }
        return;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [pinnedClusterKey, pinnedClusterIndex, sortedClusters]);
  const hoverTooltipStyle = useMemo(() => {
    if (!hoverTimeline) return null;
    const leftPercent = (hoverTimeline.chartX / chartWidth) * 100;
    if (hoverTimeline.chartX > chartWidth * 0.7) return { left: `${leftPercent}%`, transform: "translateX(-100%)" };
    return { left: `${leftPercent}%`, transform: "translateX(8px)" };
  }, [hoverTimeline, chartWidth]);
  const activeClusterMarkers = useMemo(() => {
    if (!activeCluster) return [];
    if (pinnedClusterKey) return activeCluster.markers;
    return activeCluster.markers.slice(0, MARKER_CLUSTER_PREVIEW_LIMIT);
  }, [activeCluster, pinnedClusterKey]);
  const activeClusterTimeLabel = useMemo(() => {
    if (!activeCluster) return "";
    const earliest = activeCluster.earliestTs;
    const latest = activeCluster.latestTs;
    if (!Number.isFinite(earliest) || !Number.isFinite(latest) || earliest <= 0 || latest <= 0) return "";
    if (Math.abs(latest - earliest) < 0.5) return timeFormatter.format(new Date(latest * 1000));
    return `${timeFormatter.format(new Date(earliest * 1000))} -> ${timeFormatter.format(new Date(latest * 1000))}`;
  }, [activeCluster, timeFormatter]);
  const clusterOverlayNode = useMemo(() => {
    if (!activeCluster) return null;
    const viewerItems = activeClusterMarkers.map((marker) => buildMarkerImageViewerItem(marker));
    return (
      <div
        className={[
          "pipelinesTelemetryClusterOverlay",
          pinnedClusterKey ? "isPinned" : "isTransient",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <div className="pipelinesTelemetryClusterOverlayHeader">
          {pinnedClusterKey ? (
            <div className="pipelinesTelemetryClusterNav">
              <button
                className="iconButton"
                type="button"
                onClick={() => goToCluster(-1)}
                disabled={!hasPrevCluster}
                title={t("core.ui.pipelines.telemetry.overview.cluster.prev", {}, "Previous cluster")}
                aria-label={t("core.ui.pipelines.telemetry.overview.cluster.prev", {}, "Previous cluster")}
              >
                <i className="fa-solid fa-chevron-left" aria-hidden="true" />
              </button>
              <span className="pipelinesTelemetryClusterNavCounter" aria-live="polite">
                {pinnedClusterIndex >= 0
                  ? t(
                      "core.ui.pipelines.telemetry.overview.cluster.position",
                      { current: pinnedClusterIndex + 1, total: sortedClusters.length },
                      `${pinnedClusterIndex + 1} / ${sortedClusters.length}`,
                    )
                  : ""}
              </span>
              <button
                className="iconButton"
                type="button"
                onClick={() => goToCluster(1)}
                disabled={!hasNextCluster}
                title={t("core.ui.pipelines.telemetry.overview.cluster.next", {}, "Next cluster")}
                aria-label={t("core.ui.pipelines.telemetry.overview.cluster.next", {}, "Next cluster")}
              >
                <i className="fa-solid fa-chevron-right" aria-hidden="true" />
              </button>
            </div>
          ) : null}
          <div className="pipelinesTelemetryClusterTimeLabel">{activeClusterTimeLabel}</div>
          {pinnedClusterKey ? (
            <button className="iconButton" type="button" onClick={() => setPinnedClusterKey(null)} title={t("core.actions.close")}>
              <i className="fa-solid fa-xmark" aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <div className="pipelinesTelemetryClusterOverlayBody">
          <div className="pipelinesTelemetryClusterMasonry">
            {activeClusterMarkers.map((marker, index) => {
              const markerKey = `${marker.rel_path}|${marker.ts}|${marker.node_id}`;
              const markerUrl = resolveToposyncUrl(`/files/${encodeURI(String(marker.rel_path || ""))}`);
              const displayLabel = markerEventCode(marker);
              const eventStyle = displayLabel ? eventColorStyleForMarker(marker, displayLabel) : undefined;
              return (
                <button
                  key={markerKey}
                  className={[
                    "pipelinesTelemetryClusterTile",
                    eventStyle ? "hasEventColor" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  type="button"
                  title={timeFormatter.format(new Date(Number(marker.ts || 0) * 1000))}
                  style={eventStyle}
                  onClick={() => openFullscreenMarkerImages(viewerItems, index)}
                >
                  <TelemetryMarkerImage
                    src={markerUrl}
                    alt={t("core.ui.pipelines.telemetry.overview.image_removed", {}, "Image removed by retention")}
                  />
                  <div className="pipelinesTelemetryClusterTileMeta">
                    {displayLabel ? (
                      <span className="pipelinesTelemetryClusterTileCode" title={displayLabel}>
                        {displayLabel}
                      </span>
                    ) : null}
                    {marker.size_bytes ? <span>{Math.round(Number(marker.size_bytes || 0) / 1024)} KB</span> : null}
                    <span>{timeFormatter.format(new Date(Number(marker.ts || 0) * 1000))}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
        {!pinnedClusterKey && activeCluster.markers.length > activeClusterMarkers.length ? (
          <div className="pipelinesHint">
            {t(
              "core.ui.pipelines.telemetry.overview.images_cluster_more",
              { count: activeCluster.markers.length - activeClusterMarkers.length },
              `+${activeCluster.markers.length - activeClusterMarkers.length} more. Click to pin and browse the full cluster.`,
            )}
          </div>
        ) : null}
      </div>
    );
  }, [
    activeCluster,
    activeClusterMarkers,
    activeClusterTimeLabel,
    buildMarkerImageViewerItem,
    pinnedClusterKey,
    pinnedClusterIndex,
    openFullscreenMarkerImages,
    sortedClusters,
    hasPrevCluster,
    hasNextCluster,
    t,
    timeFormatter,
  ]);

  return (
    <div className="card">
      <div className="cardTitle">{t("core.ui.pipelines.telemetry.overview.title", {}, "Telemetry timeline")}</div>
      <div className="cardBody">
        <div className="pipelinesStepStatsHeader">
          <div className="pipelinesHint">
            {isAggregate
              ? t(
                  "core.ui.pipelines.telemetry.aggregate.subtitle",
                  {},
                  "Merged across all pipelines. Each line keeps the strongest bucket seen for that metric.",
                )
              : t(
                  "core.ui.pipelines.telemetry.overview.subtitle",
                  {},
                  "Bands show min/max per time bucket. Solid lines show weighted averages.",
                )}
          </div>
          <div className="pipelinesStepStatsControls">
            {isAggregate ? (
              <label className="pipelinesLabel pipelinesTelemetryFilterLabel">
                <span>{t("core.ui.pipelines.telemetry.aggregate.filter.label", {}, "Pipelines")}</span>
                <Select<SelectOption, true>
                  isMulti
                  closeMenuOnSelect={false}
                  hideSelectedOptions={false}
                  styles={pipelinesReactSelectStyles}
                  options={pipelineOptions}
                  value={selectedPipelineOptions}
                  placeholder={t("core.ui.pipelines.telemetry.aggregate.filter.placeholder", {}, "Select pipelines")}
                  noOptionsMessage={() => t("core.ui.pipelines.telemetry.aggregate.filter.empty", {}, "No pipelines available")}
                  onChange={(value: MultiValue<SelectOption>) => setSelectedPipelineOptions(value as SelectOption[])}
                />
              </label>
            ) : null}
            <div className="pipelinesModes">
              <button
                className={["pillButton", rangeSeconds === RANGE_SHORT_SECONDS ? "isActive" : ""].filter(Boolean).join(" ")}
                type="button"
                onClick={() => setRangeSeconds(RANGE_SHORT_SECONDS)}
              >
                {t("core.ui.pipelines.telemetry.overview.range.short", {}, "Short")}
              </button>
              <button
                className={["pillButton", rangeSeconds === RANGE_DEFAULT_SECONDS ? "isActive" : ""].filter(Boolean).join(" ")}
                type="button"
                onClick={() => setRangeSeconds(RANGE_DEFAULT_SECONDS)}
              >
                {t("core.ui.pipelines.telemetry.overview.range.default", {}, "Default")}
              </button>
              <button
                className={["pillButton", rangeSeconds === RANGE_LONG_SECONDS ? "isActive" : ""].filter(Boolean).join(" ")}
                type="button"
                onClick={() => setRangeSeconds(RANGE_LONG_SECONDS)}
              >
                {t("core.ui.pipelines.telemetry.overview.range.long", {}, "Long")}
              </button>
              <button className="pillButton" type="button" onClick={() => setRefreshNonce((value) => value + 1)} disabled={loading}>
                <i className="fa-solid fa-rotate" aria-hidden="true" />
                {t("core.actions.refresh")}
              </button>
            </div>
          </div>
        </div>
        {lastUpdatedAt ? (
          <div className="pipelinesHint">
            {t(
              "core.ui.pipelines.telemetry.overview.last_updated",
              { time: timeOnlyFormatter.format(new Date(lastUpdatedAt)) },
              "Last updated: {{time}}",
            )}
          </div>
        ) : null}

        {showLoadingHint ? <div className="pipelinesHint">{t("core.ui.pipelines.telemetry.loading", {}, "Loading telemetry…")}</div> : null}
        {error ? <div className="pipelinesInlineError">{t("core.ui.pipelines.telemetry.error", { error }, "Telemetry unavailable: {{error}}")}</div> : null}
        {showNoTelemetryData ? (
          <div className="pipelinesHint">
            {isAggregate && pipelineOptions.length > 0 && selectedPipelineOptions.length === 0
              ? t(
                  "core.ui.pipelines.telemetry.aggregate.filter.none_selected",
                  {},
                  "Select at least one pipeline to view aggregate telemetry.",
                )
              : t(
                  "core.ui.pipelines.telemetry.no_data",
                  {},
                  "No telemetry samples yet. Let the pipeline run and reopen this panel.",
                )}
          </div>
        ) : null}

        {showTelemetryContent ? (
          <>
            <div className="pipelinesTelemetryLegend">
              {series.map((item) => {
                const hidden = hiddenSeriesKeys.has(item.seriesKey);
                return (
                  <button
                    key={`legend:${item.seriesKey}`}
                    type="button"
                    className={[
                      "pipelinesTelemetryLegendItem",
                      "isToggleable",
                      hidden ? "isHidden" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    aria-pressed={!hidden}
                    title={
                      hidden
                        ? t("core.ui.pipelines.telemetry.overview.legend.show", {}, "Show this series")
                        : t("core.ui.pipelines.telemetry.overview.legend.hide", {}, "Hide this series")
                    }
                    onClick={() => toggleSeriesHidden(item.seriesKey)}
                  >
                    <span
                      className="pipelinesTelemetryLegendSwatch"
                      style={hidden ? undefined : { backgroundColor: item.color }}
                    />
                    <span>{item.label}</span>
                  </button>
                );
              })}
              {markerClusters.length ? (
                <div className="pipelinesTelemetryLegendItem">
                  <span className="pipelinesTelemetryLegendSwatch isImage" />
                  <span>{t("core.ui.pipelines.telemetry.overview.images", {}, "Stored images")}</span>
                </div>
              ) : null}
            </div>

            <div className="pipelinesTelemetryTimelineWrap">
	              <div
	                className="pipelinesTelemetryTimelineStage"
	                onMouseMove={(event) => {
	                  if (xMax <= xMin || visibleSeries.length <= 0) return;
	                  const rect = event.currentTarget.getBoundingClientRect();
	                  if (rect.width <= 0) return;
	                  const rawX = ((event.clientX - rect.left) / rect.width) * chartWidth;
	                  const chartX = Math.max(paddingLeft, Math.min(chartWidth - paddingRight, rawX));
	                  const ratio = (chartX - paddingLeft) / Math.max(1, innerWidth);
	                  const cursorTs = xMin + Math.max(0, Math.min(1, ratio)) * Math.max(1e-9, xMax - xMin);
	                  const samples: HoverTimelineSample[] = [];
	                  for (const metric of visibleSeries) {
	                    const nearest = findNearestPoint(metric.points, cursorTs);
	                    if (!nearest) continue;
	                    if (metric.bucketSeconds > 0) {
	                      const distance = Math.abs(Number(nearest.bucket_start_s) - cursorTs);
	                      if (distance > metric.bucketSeconds * 0.9) continue;
	                    }
	                    const segments = timelineSegmentsBySeries.get(metric.seriesKey) ?? [];
	                    const segment = findSegmentAtTs(segments, nearest.bucket_start_s);
	                    if (!segment) continue;
	                    const span = Math.max(1e-9, segment.max - segment.min);
	                    samples.push({
	                      seriesKey: metric.seriesKey,
	                      metricId: metric.metricId,
	                      label: metric.label,
	                      color: metric.color,
	                      bucketStartS: nearest.bucket_start_s,
	                      avg: nearest.avg,
	                      min: nearest.min,
	                      max: nearest.max,
	                      y: yScale((nearest.avg - segment.min) / span),
	                    });
	                  }
	                  if (!samples.length) {
	                    setHoverTimeline(null);
	                    return;
	                  }
	                  setHoverTimeline({ chartX, cursorTs, samples });
	                }}
	                onMouseLeave={() => {
	                  setHoverTimeline(null);
	                  if (!pinnedClusterKey) setHoveredClusterKey(null);
	                }}
	              >
	                <svg
	                  className="pipelinesTelemetryTimeline"
	                  viewBox={`0 0 ${chartWidth} ${chartHeight}`}
	                  preserveAspectRatio="none"
	                  aria-hidden="true"
	                >
	                  <line
	                    x1={paddingLeft}
	                    x2={chartWidth - paddingRight}
	                    y1={paddingTop + innerHeight}
	                    y2={paddingTop + innerHeight}
	                    className="pipelinesTelemetryAxis"
	                  />
	                  {visibleSeries.map((item) => {
	                    const segments = timelineSegmentsBySeries.get(item.seriesKey) ?? [];
	                    if (!segments.length) return null;
	                    return (
	                      <g key={`series:${item.seriesKey}`}>
	                        {segments.map((segment, segIndex) => {
	                          const linePath = buildLinePath(segment.points, xScale, yScale, segment.min, segment.max);
	                          const bandPath = buildBandPath(segment.points, xScale, yScale, segment.min, segment.max);
	                          return (
	                            <g key={`seg:${segIndex}`}>
	                              {bandPath ? <path d={bandPath} fill={item.color} fillOpacity={0.16} stroke="none" /> : null}
	                              <path d={linePath} fill="none" stroke={item.color} strokeWidth={2.2} />
	                            </g>
	                          );
	                        })}
	                      </g>
	                    );
	                  })}
	                  {hoverTimeline && !activeCluster ? (
	                    <>
	                      <line
	                        x1={hoverTimeline.chartX}
	                        x2={hoverTimeline.chartX}
	                        y1={paddingTop}
	                        y2={paddingTop + innerHeight}
	                        className="pipelinesTelemetryCursorLine"
	                      />
	                      {hoverTimeline.samples.map((sample) => (
	                        <circle
	                          key={`hover:${sample.seriesKey}`}
	                          cx={hoverTimeline.chartX}
	                          cy={sample.y}
	                          r={3.5}
	                          fill={sample.color}
	                          stroke="var(--panelSolid)"
	                          strokeWidth={1}
	                        />
	                      ))}
	                    </>
	                  ) : null}
	                </svg>

	                {markerClusters.length ? (
	                  <div className="pipelinesTelemetryMarkerLayer">
	                    {markerClusters.map((cluster) => {
	                      const leftPercent = (cluster.x / chartWidth) * 100;
	                      const topPercent = (cluster.y / chartHeight) * 100;
	                      const score01 = cluster.score01;
	                      const accent = cluster.accentColor ?? "var(--muted)";
	                      const countBoost = Math.min(36, Math.round(Math.log2(cluster.count + 1) * 10));
	                      const intensity = Math.round(18 + countBoost + (score01 ?? 0.24) * 44);
	                      const backgroundColor = `color-mix(in srgb, ${accent} ${Math.min(92, intensity)}%, var(--panelSolid))`;
	                      const borderColor = `color-mix(in srgb, ${accent} ${Math.min(96, intensity + 10)}%, var(--panelSolid))`;
	                      const markerStyle = {
	                        left: `${leftPercent}%`,
	                        top: `${topPercent}%`,
	                        backgroundColor,
	                        borderColor,
	                        width: `${cluster.visualWidth}px`,
	                        height: `${cluster.visualWidth}px`,
	                        ["--marker-z" as any]: cluster.zIndex,
	                      } as React.CSSProperties;
	                      const title =
	                        cluster.count <= 1
	                          ? timeFormatter.format(new Date(cluster.latestTs * 1000))
	                          : `${cluster.count} · ${timeFormatter.format(new Date(cluster.earliestTs * 1000))} -> ${timeFormatter.format(new Date(cluster.latestTs * 1000))}`;

	                      return (
	                        <button
	                          key={cluster.key}
	                          className={[
	                            "pipelinesTelemetryMarkerCluster",
	                            cluster.count > 1 ? "isGrouped" : "isSingle",
	                            pinnedClusterKey === cluster.key ? "isPinned" : "",
	                          ]
	                            .filter(Boolean)
	                            .join(" ")}
	                          type="button"
	                          aria-label={t("core.ui.pipelines.telemetry.overview.images", {}, "Stored images")}
	                          aria-expanded={pinnedClusterKey === cluster.key}
	                          title={title}
	                          style={markerStyle}
	                          onMouseEnter={() => {
	                            if (pinnedClusterKey) return;
	                            setHoverTimeline(null);
	                            setHoveredClusterKey(cluster.key);
	                          }}
	                          onMouseLeave={() => {
	                            if (pinnedClusterKey) return;
	                            setHoveredClusterKey((prev) => (prev === cluster.key ? null : prev));
	                          }}
	                          onFocus={() => {
	                            if (pinnedClusterKey) return;
	                            setHoverTimeline(null);
	                            setHoveredClusterKey(cluster.key);
	                          }}
	                          onBlur={() => {
	                            if (pinnedClusterKey) return;
	                            setHoveredClusterKey((prev) => (prev === cluster.key ? null : prev));
	                          }}
	                          onClick={() => {
	                            setPinnedClusterKey((prev) => (prev === cluster.key ? null : cluster.key));
	                            setHoveredClusterKey(null);
	                            setHoverTimeline(null);
	                          }}
	                        >
	                          {cluster.count > 1 ? <span className="pipelinesTelemetryMarkerClusterCount">{cluster.countLabel}</span> : null}
	                        </button>
	                      );
	                    })}
	                  </div>
	                ) : null}

	                {hoverTimeline && hoverTooltipStyle ? (
	                  <div className="pipelinesTelemetryHoverTooltip" style={hoverTooltipStyle}>
	                    <div className="pipelinesTelemetryHoverTime">{timeFormatter.format(new Date(hoverTimeline.cursorTs * 1000))}</div>
	                    {hoverTimeline.samples.map((sample) => (
	                      <div key={`hover:row:${sample.seriesKey}`} className="pipelinesTelemetryHoverRow">
	                        <span className="pipelinesTelemetryHoverSwatch" style={{ backgroundColor: sample.color }} />
	                        <div className="pipelinesTelemetryHoverRowText">
	                          <span className="pipelinesTelemetryHoverRowLabel">{sample.label}</span>
	                          <span className="pipelinesTelemetryHoverRowStats">
	                            <span>{t("core.ui.pipelines.telemetry.total_avg", {}, "Avg")} {decimalFormatter.format(sample.avg)}</span>
	                            <span aria-hidden="true">·</span>
	                            <span>{t("core.ui.pipelines.telemetry.total_min", {}, "Min")} {decimalFormatter.format(sample.min)}</span>
	                            <span aria-hidden="true">·</span>
	                            <span>{t("core.ui.pipelines.telemetry.total_max", {}, "Max")} {decimalFormatter.format(sample.max)}</span>
	                          </span>
	                        </div>
	                      </div>
	                    ))}
	                  </div>
	                ) : null}
	              </div>
            </div>
          </>
        ) : null}

        {onReset && pipelineName && !isAggregate ? (
          <div className="rowWrap" style={{ justifyContent: "flex-end", marginTop: 10 }}>
            <button
              className="iconButton"
              type="button"
              disabled={Boolean(resetting)}
              aria-label={
                resetting
                  ? t("core.ui.pipelines.telemetry.overview.resetting", {}, "Clearing…")
                  : t("core.ui.pipelines.telemetry.overview.reset", {}, "Clear stats & telemetry")
              }
              title={
                resetting
                  ? t("core.ui.pipelines.telemetry.overview.resetting", {}, "Clearing…")
                  : t("core.ui.pipelines.telemetry.overview.reset", {}, "Clear stats & telemetry")
              }
              onClick={() => {
                if (!confirm(t("core.ui.pipelines.telemetry.overview.confirm_reset", { name: pipelineName }, `Clear stats & telemetry for '${pipelineName}'?`))) return;
                void onReset();
              }}
            >
              <i className={["fa-solid", resetting ? "fa-rotate fa-spin" : "fa-broom"].join(" ")} aria-hidden="true" />
            </button>
          </div>
        ) : null}
      </div>
      {clusterOverlayNode && typeof document !== "undefined" ? createPortal(clusterOverlayNode, document.body) : null}
      <FullscreenImageViewer
        open={fullscreenImageOpen}
        items={fullscreenImageItems}
        index={fullscreenImageIndex}
        onIndexChange={setFullscreenImageIndex}
        onClose={() => setFullscreenImageOpen(false)}
      />
    </div>
  );
}
