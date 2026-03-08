import React, { useEffect, useMemo, useState } from "react";

import {
  getPipelineTelemetryImageMarkers,
  getPipelineTelemetryNumeric,
  type PipelineTelemetryImageMarker,
  type PipelineTelemetryNumeric,
} from "../../../util/api";
import { i18n } from "../../../util/i18n";
import type { InteractiveStep } from "./types";

type Props = {
  pipelineName: string | null;
  steps: InteractiveStep[];
  externalRefreshNonce?: number;
  resetting?: boolean;
  onReset?: () => void | Promise<void>;
};

type MetricSeries = {
  metricId: string;
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
  score01: number | null;
  accentColor: string | null;
  zIndex: number;
  earliestTs: number;
  latestTs: number;
  visualWidth: number;
};

const RANGE_SHORT_SECONDS = 2 * 60 * 60;
const RANGE_DEFAULT_SECONDS = 24 * 60 * 60;
const RANGE_LONG_SECONDS = 3 * 24 * 60 * 60;
const MARKER_FETCH_LIMIT = 5_000;
const MARKER_CLUSTER_DISTANCE = 9;
const MARKER_CLUSTER_SPAN_LIMIT = 18;
const MARKER_CLUSTER_LANE_COUNT = 4;
const MARKER_CLUSTER_LANE_SPACING = 11;
const MARKER_CLUSTER_GAP = 5;
const MARKER_CLUSTER_PREVIEW_LIMIT = 12;

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function metricLabel(metricId: string, t: TranslateFn): string {
  if (metricId === "motion.score") return t("core.ui.pipelines.telemetry.metric.motion_score", {}, "Motion score");
  if (metricId === "yolo.confidence") return t("core.ui.pipelines.telemetry.metric.yolo_confidence", {}, "YOLO confidence");
  return metricId;
}

function metricAccentColor(metricId: string): string {
  if (metricId === "motion.score") return "var(--color-accent-teal)";
  if (metricId === "yolo.confidence") return "var(--color-warning)";
  return "var(--accent)";
}

function aggregateMetricPoints(items: PipelineTelemetryNumeric[]): AggregatedPoint[] {
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
  const digits = String(count).length;
  return Math.max(22, Math.min(44, 16 + digits * 7));
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
  pipelineName,
  steps,
  externalRefreshNonce,
  resetting,
  onReset,
}: Props): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [series, setSeries] = useState<MetricSeries[]>([]);
  const [markers, setMarkers] = useState<PipelineTelemetryImageMarker[]>([]);
  const [rangeSeconds, setRangeSeconds] = useState(RANGE_DEFAULT_SECONDS);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [hoveredClusterKey, setHoveredClusterKey] = useState<string | null>(null);
  const [pinnedClusterKey, setPinnedClusterKey] = useState<string | null>(null);
  const [hoverTimeline, setHoverTimeline] = useState<HoverTimelineState | null>(null);
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

  const metricTargets = useMemo(() => {
    if (!pipelineName) return [];
    const unique = new Map<string, { nodeId: string; metricId: string }>();
    for (const step of steps) {
      if (step.operatorId === "camera.motion_gate") {
        const item = { nodeId: step.nodeId, metricId: "motion.score" };
        unique.set(`${item.metricId}:${item.nodeId}`, item);
      }
      if (step.operatorId === "vision.object_tracking_yolo" || step.operatorId === "vision.object_detection_yolo") {
        const item = { nodeId: step.nodeId, metricId: "yolo.confidence" };
        unique.set(`${item.metricId}:${item.nodeId}`, item);
      }
    }
    return Array.from(unique.values());
  }, [pipelineName, steps]);

  const metricTargetsKey = useMemo(
    () => metricTargets.map((item) => `${item.metricId}:${item.nodeId}`).join("|"),
    [metricTargets],
  );

  useEffect(() => {
    if (!pipelineName) {
      setSeries([]);
      setMarkers([]);
      setError(null);
      setLastUpdatedAt(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

        const run = async () => {
      try {
        const pointLimit = 5000;
        const numericPromises = metricTargets.map((target) =>
          getPipelineTelemetryNumeric(pipelineName, target.nodeId, target.metricId, pointLimit, rangeSeconds),
        );
        const [numericResponses, markerResponse] = await Promise.all([
          Promise.all(numericPromises),
          getPipelineTelemetryImageMarkers(pipelineName, {
            metricId: "store.image",
            limit: MARKER_FETCH_LIMIT,
            windowSeconds: rangeSeconds,
          }),
        ]);
        if (cancelled) return;

        const byMetric = new Map<string, PipelineTelemetryNumeric[]>();
        for (const item of numericResponses) {
          const metricId = String(item.metric_id || "").trim();
          if (!metricId) continue;
          const group = byMetric.get(metricId) ?? [];
          group.push(item);
          byMetric.set(metricId, group);
        }

        const nextSeries: MetricSeries[] = [];
        for (const [metricId, group] of byMetric.entries()) {
          const points = aggregateMetricPoints(group);
          if (points.length === 0) continue;
          const bucketSeconds = Math.max(0, Number(group[0]?.bucket_seconds ?? 0));
          nextSeries.push({
            metricId,
            color: metricAccentColor(metricId),
            points,
            bucketSeconds,
          });
        }

        setSeries(nextSeries);
        setMarkers(Array.isArray(markerResponse.markers) ? markerResponse.markers : []);
        setLastUpdatedAt(Date.now());
      } catch (err: any) {
        if (cancelled) return;
        setSeries([]);
        setMarkers([]);
        setError(String(err?.message ?? err));
      } finally {
        if (cancelled) return;
        setLoading(false);
      }
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [pipelineName, metricTargetsKey, rangeSeconds, refreshNonce, externalRefreshNonce]);

  useEffect(() => {
    setPinnedClusterKey(null);
    setHoveredClusterKey(null);
    setHoverTimeline(null);
  }, [pipelineName, markers.length, rangeSeconds]);

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

  const timelineSegmentsByMetric = useMemo(() => {
    const out = new Map<string, TimelineSegment[]>();
    for (const item of series) {
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
      out.set(item.metricId, segments);
    }
    return out;
  }, [series]);

  const markerPoints: MarkerPoint[] = useMemo(() => {
    if (!markers.length || xMax <= xMin) return [];
    return markers
      .map((marker) => {
        const ts = Number(marker.ts || 0);
        if (!Number.isFinite(ts) || ts < xMin || ts > xMax) return null;
        const x = xScale(ts);
        let foundMetricValue = false;
        let bestMetricValue = -1;
        let bestMetricId: string | null = null;
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
            bestMetricId = metric.metricId;
          }
        }

        const confidenceValue = marker.confidence == null ? null : clamp01(Number(marker.confidence));
        const score01 = foundMetricValue ? combined : confidenceValue;
        const accentColor = bestMetricId ? metricAccentColor(bestMetricId) : null;
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
  const markerClustersByKey = useMemo(
    () => new Map(markerClusters.map((cluster) => [cluster.key, cluster])),
    [markerClusters],
  );

  useEffect(() => {
    if (hoveredClusterKey && !markerClustersByKey.has(hoveredClusterKey)) setHoveredClusterKey(null);
    if (pinnedClusterKey && !markerClustersByKey.has(pinnedClusterKey)) setPinnedClusterKey(null);
  }, [markerClustersByKey, hoveredClusterKey, pinnedClusterKey]);

  useEffect(() => {
    if (!pinnedClusterKey) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPinnedClusterKey(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [pinnedClusterKey]);

  const activeCluster = useMemo(() => {
    if (pinnedClusterKey) return markerClustersByKey.get(pinnedClusterKey) ?? null;
    if (hoveredClusterKey) return markerClustersByKey.get(hoveredClusterKey) ?? null;
    return null;
  }, [markerClustersByKey, pinnedClusterKey, hoveredClusterKey]);
  const hoverTooltipStyle = useMemo(() => {
    if (!hoverTimeline) return null;
    const leftPercent = (hoverTimeline.chartX / chartWidth) * 100;
    if (hoverTimeline.chartX > chartWidth * 0.7) return { left: `${leftPercent}%`, transform: "translateX(-100%)" };
    return { left: `${leftPercent}%`, transform: "translateX(8px)" };
  }, [hoverTimeline, chartWidth]);
  const clusterOverlayStyle = useMemo(() => {
    if (!activeCluster) return null;
    const leftPercent = (activeCluster.x / chartWidth) * 100;
    if (activeCluster.x < chartWidth * 0.28) return { left: `${leftPercent}%`, transform: "translateX(0)" };
    if (activeCluster.x > chartWidth * 0.72) return { left: `${leftPercent}%`, transform: "translateX(-100%)" };
    return { left: `${leftPercent}%`, transform: "translateX(-50%)" };
  }, [activeCluster, chartWidth]);
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

  return (
    <div className="card">
      <div className="cardTitle">{t("core.ui.pipelines.telemetry.overview.title", {}, "Telemetry timeline")}</div>
      <div className="cardBody">
	        <div className="pipelinesStepStatsHeader">
	          <div className="pipelinesHint">
	            {t(
	              "core.ui.pipelines.telemetry.overview.subtitle",
	              {},
	              "Bands show min/max per time bucket. Solid lines show weighted averages.",
	            )}
	          </div>
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
        {lastUpdatedAt ? (
          <div className="pipelinesHint">
            {t(
              "core.ui.pipelines.telemetry.overview.last_updated",
              { time: timeOnlyFormatter.format(new Date(lastUpdatedAt)) },
              "Last updated: {{time}}",
            )}
          </div>
        ) : null}

	        {loading ? <div className="pipelinesHint">{t("core.ui.pipelines.telemetry.loading", {}, "Loading telemetry…")}</div> : null}
	        {error ? <div className="pipelinesInlineError">{t("core.ui.pipelines.telemetry.error", { error }, "Telemetry unavailable: {{error}}")}</div> : null}
	        {!loading && !error && series.length === 0 && markerClusters.length === 0 ? (
	          <div className="pipelinesHint">
	            {t(
	              "core.ui.pipelines.telemetry.no_data",
	              {},
	              "No telemetry samples yet. Let the pipeline run and reopen this panel.",
	            )}
	          </div>
	        ) : null}

		        {!loading && !error && (series.length > 0 || markerClusters.length > 0) ? (
		          <>
	            <div className="pipelinesTelemetryLegend">
	              {series.map((item) => (
	                <div key={`legend:${item.metricId}`} className="pipelinesTelemetryLegendItem">
	                  <span className="pipelinesTelemetryLegendSwatch" style={{ backgroundColor: item.color }} />
	                  <span>{metricLabel(item.metricId, t)}</span>
	                </div>
	              ))}
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
	                  if (xMax <= xMin || series.length <= 0) return;
	                  const rect = event.currentTarget.getBoundingClientRect();
	                  if (rect.width <= 0) return;
	                  const rawX = ((event.clientX - rect.left) / rect.width) * chartWidth;
	                  const chartX = Math.max(paddingLeft, Math.min(chartWidth - paddingRight, rawX));
	                  const ratio = (chartX - paddingLeft) / Math.max(1, innerWidth);
	                  const cursorTs = xMin + Math.max(0, Math.min(1, ratio)) * Math.max(1e-9, xMax - xMin);
	                  const samples: HoverTimelineSample[] = [];
	                  for (const metric of series) {
	                    const nearest = findNearestPoint(metric.points, cursorTs);
	                    if (!nearest) continue;
	                    if (metric.bucketSeconds > 0) {
	                      const distance = Math.abs(Number(nearest.bucket_start_s) - cursorTs);
	                      if (distance > metric.bucketSeconds * 0.9) continue;
	                    }
	                    const segments = timelineSegmentsByMetric.get(metric.metricId) ?? [];
	                    const segment = findSegmentAtTs(segments, nearest.bucket_start_s);
	                    if (!segment) continue;
	                    const span = Math.max(1e-9, segment.max - segment.min);
	                    samples.push({
	                      metricId: metric.metricId,
	                      label: metricLabel(metric.metricId, t),
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
	                  {series.map((item) => {
	                    const segments = timelineSegmentsByMetric.get(item.metricId) ?? [];
	                    if (!segments.length) return null;
	                    return (
	                      <g key={`series:${item.metricId}`}>
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
	                          key={`hover:${sample.metricId}`}
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
	                      const intensity = Math.round(20 + (score01 ?? 0.24) * 80);
	                      const backgroundColor = `color-mix(in srgb, ${accent} ${intensity}%, var(--panelSolid))`;
	                      const borderColor = `color-mix(in srgb, ${accent} ${Math.round(42 + (score01 ?? 0.18) * 58)}%, var(--panelSolid))`;
	                      const markerStyle = {
	                        left: `${leftPercent}%`,
	                        top: `${topPercent}%`,
	                        backgroundColor,
	                        borderColor,
	                        minWidth: `${cluster.visualWidth}px`,
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
	                          {cluster.count > 1 ? <span className="pipelinesTelemetryMarkerClusterCount">{cluster.count}</span> : null}
	                        </button>
	                      );
	                    })}
	                  </div>
	                ) : null}

	                {hoverTimeline && hoverTooltipStyle ? (
	                  <div className="pipelinesTelemetryHoverTooltip" style={hoverTooltipStyle}>
	                    <div className="pipelinesHint">{timeFormatter.format(new Date(hoverTimeline.cursorTs * 1000))}</div>
	                    {hoverTimeline.samples.map((sample) => (
	                      <div key={`hover:row:${sample.metricId}`} className="pipelinesTelemetryHoverRow">
	                        <span className="pipelinesTelemetryHoverSwatch" style={{ backgroundColor: sample.color }} />
	                        <span>{sample.label}</span>
	                        <span>
	                          {t("core.ui.pipelines.telemetry.total_avg", {}, "Avg")}: {decimalFormatter.format(sample.avg)} ·{" "}
	                          {t("core.ui.pipelines.telemetry.total_min", {}, "Min")}: {decimalFormatter.format(sample.min)} ·{" "}
	                          {t("core.ui.pipelines.telemetry.total_max", {}, "Max")}: {decimalFormatter.format(sample.max)}
	                        </span>
	                      </div>
	                    ))}
	                  </div>
	                ) : null}
	                {activeCluster && clusterOverlayStyle ? (
	                  <div
	                    className={[
	                      "pipelinesTelemetryClusterOverlay",
	                      pinnedClusterKey ? "isPinned" : "isTransient",
	                    ]
	                      .filter(Boolean)
	                      .join(" ")}
	                    style={clusterOverlayStyle}
	                  >
	                    <div className="pipelinesTelemetryClusterOverlayHeader">
	                      <div>
	                        <div className="pipelinesTelemetryClusterOverlayTitle">
	                          {activeCluster.count <= 1
	                            ? t("core.ui.pipelines.telemetry.overview.images", {}, "Stored images")
	                            : t(
	                                "core.ui.pipelines.telemetry.overview.images_cluster_count",
	                                { count: activeCluster.count },
	                                `${activeCluster.count} stored images`,
	                              )}
	                        </div>
	                        <div className="pipelinesHint">{activeClusterTimeLabel}</div>
	                      </div>
	                      {pinnedClusterKey ? (
	                        <button className="iconButton" type="button" onClick={() => setPinnedClusterKey(null)} title={t("core.actions.close")}>
	                          <i className="fa-solid fa-xmark" aria-hidden="true" />
	                        </button>
	                      ) : null}
	                    </div>
	                    <div className="pipelinesTelemetryClusterOverlayBody">
	                      <div className="pipelinesTelemetryClusterMasonry">
	                        {activeClusterMarkers.map((marker) => {
	                          const markerKey = `${marker.rel_path}|${marker.ts}|${marker.node_id}`;
	                          const markerTs = Number(marker.ts || 0);
	                          return (
	                            <a
	                              key={markerKey}
	                              className="pipelinesTelemetryClusterTile"
	                              href={`/files/${encodeURI(String(marker.rel_path || ""))}`}
	                              target={pinnedClusterKey ? "_blank" : undefined}
	                              rel={pinnedClusterKey ? "noreferrer" : undefined}
	                            >
	                              <img
	                                src={`/files/${encodeURI(String(marker.rel_path || ""))}`}
	                                alt="marker preview"
	                                className="pipelinesTelemetryClusterTileImage"
	                                loading="lazy"
	                              />
	                              <div className="pipelinesTelemetryClusterTileMeta">
	                                <span>{marker.image_key || t("core.ui.pipelines.telemetry.overview.images", {}, "Stored images")}</span>
	                                <span>{Number.isFinite(markerTs) && markerTs > 0 ? timeOnlyFormatter.format(new Date(markerTs * 1000)) : ""}</span>
	                              </div>
	                            </a>
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
	                ) : null}
	              </div>
		            </div>
		          </>
		        ) : null}

	        {onReset && pipelineName ? (
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
	    </div>
	  );
	}
