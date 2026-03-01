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

type ActiveMarkerSelection = {
  marker: PipelineTelemetryImageMarker;
  x: number;
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

export function PipelineTelemetryOverviewCard({ pipelineName, steps }: Props): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [series, setSeries] = useState<MetricSeries[]>([]);
  const [markers, setMarkers] = useState<PipelineTelemetryImageMarker[]>([]);
  const [pointLimit, setPointLimit] = useState(720);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [hoveredMarker, setHoveredMarker] = useState<ActiveMarkerSelection | null>(null);
  const [pinnedMarker, setPinnedMarker] = useState<ActiveMarkerSelection | null>(null);
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
        const numericPromises = metricTargets.map((target) =>
          getPipelineTelemetryNumeric(pipelineName, target.nodeId, target.metricId, pointLimit),
        );
        const [numericResponses, markerResponse] = await Promise.all([
          Promise.all(numericPromises),
          getPipelineTelemetryImageMarkers(pipelineName, { metricId: "store.image", limit: 400 }),
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
  }, [pipelineName, metricTargetsKey, pointLimit, refreshNonce]);

  useEffect(() => {
    setPinnedMarker(null);
    setHoveredMarker(null);
    setHoverTimeline(null);
  }, [pipelineName, markers.length]);

  const allPoints = useMemo(() => series.flatMap((item) => item.points), [series]);
  const xMin = useMemo(
    () => (allPoints.length ? Math.min(...allPoints.map((point) => Number(point.bucket_start_s || 0))) : 0),
    [allPoints],
  );
  const xMax = useMemo(
    () => (allPoints.length ? Math.max(...allPoints.map((point) => Number(point.bucket_start_s || 0))) : 0),
    [allPoints],
  );

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

  const seriesRanges = useMemo(() => {
    const out = new Map<string, { min: number; max: number }>();
    for (const item of series) {
      const values = item.points.flatMap((point) => [point.min, point.max]);
      if (!values.length) continue;
      out.set(item.metricId, { min: Math.min(...values), max: Math.max(...values) });
    }
    return out;
  }, [series]);

  const markerPoints = useMemo(() => {
    if (!markers.length || xMax <= xMin) return [];
    return markers
      .map((marker) => {
        const ts = Number(marker.ts || 0);
        if (!Number.isFinite(ts) || ts < xMin || ts > xMax) return null;
        return {
          marker,
          x: xScale(ts),
          y: paddingTop + innerHeight + 6,
        };
      })
      .filter(Boolean) as Array<{ marker: PipelineTelemetryImageMarker; x: number; y: number }>;
  }, [markers, xMin, xMax, chartWidth, chartHeight]);

  const activeMarker = pinnedMarker ?? hoveredMarker;
  const hoverTooltipStyle = useMemo(() => {
    if (!hoverTimeline) return null;
    const leftPercent = (hoverTimeline.chartX / chartWidth) * 100;
    if (hoverTimeline.chartX > chartWidth * 0.7) return { left: `${leftPercent}%`, transform: "translateX(-100%)" };
    return { left: `${leftPercent}%`, transform: "translateX(8px)" };
  }, [hoverTimeline, chartWidth]);
  const markerOverlayStyle = useMemo(() => {
    if (!activeMarker) return null;
    const leftPercent = (activeMarker.x / chartWidth) * 100;
    if (activeMarker.x > chartWidth * 0.55) return { left: `${leftPercent}%`, transform: "translateX(calc(-100% - 10px))" };
    return { left: `${leftPercent}%`, transform: "translateX(10px)" };
  }, [activeMarker, chartWidth]);

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
              className={["pillButton", pointLimit === 360 ? "isActive" : ""].filter(Boolean).join(" ")}
              type="button"
              onClick={() => setPointLimit(360)}
            >
              {t("core.ui.pipelines.telemetry.overview.range.short", {}, "Short")}
            </button>
            <button
              className={["pillButton", pointLimit === 720 ? "isActive" : ""].filter(Boolean).join(" ")}
              type="button"
              onClick={() => setPointLimit(720)}
            >
              {t("core.ui.pipelines.telemetry.overview.range.default", {}, "Default")}
            </button>
            <button
              className={["pillButton", pointLimit === 1440 ? "isActive" : ""].filter(Boolean).join(" ")}
              type="button"
              onClick={() => setPointLimit(1440)}
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
        {!loading && !error && series.length === 0 ? (
          <div className="pipelinesHint">
            {t(
              "core.ui.pipelines.telemetry.no_data",
              {},
              "No telemetry samples yet. Let the pipeline run and reopen this panel.",
            )}
          </div>
        ) : null}

        {!loading && !error && series.length > 0 ? (
          <>
            <div className="pipelinesTelemetryLegend">
              {series.map((item) => (
                <div key={`legend:${item.metricId}`} className="pipelinesTelemetryLegendItem">
                  <span className="pipelinesTelemetryLegendSwatch" style={{ backgroundColor: item.color }} />
                  <span>{metricLabel(item.metricId, t)}</span>
                </div>
              ))}
            </div>

            <div className="pipelinesTelemetryTimelineWrap">
              <svg
                className="pipelinesTelemetryTimeline"
                viewBox={`0 0 ${chartWidth} ${chartHeight}`}
                preserveAspectRatio="none"
                aria-hidden="true"
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
                    const range = seriesRanges.get(metric.metricId);
                    if (!range) continue;
                    const span = Math.max(1e-9, range.max - range.min);
                    samples.push({
                      metricId: metric.metricId,
                      label: metricLabel(metric.metricId, t),
                      color: metric.color,
                      bucketStartS: nearest.bucket_start_s,
                      avg: nearest.avg,
                      min: nearest.min,
                      max: nearest.max,
                      y: yScale((nearest.avg - range.min) / span),
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
                  if (!pinnedMarker) setHoveredMarker(null);
                }}
              >
                <line x1={paddingLeft} x2={chartWidth - paddingRight} y1={paddingTop + innerHeight} y2={paddingTop + innerHeight} className="pipelinesTelemetryAxis" />
                {series.map((item) => {
                  const range = seriesRanges.get(item.metricId);
                  if (!range) return null;
                  const minValue = range.min;
                  const maxValue = range.max;
                  const segments = splitIntoSegments(item.points, item.bucketSeconds);
                  return (
                    <g key={`series:${item.metricId}`}>
                      {segments.map((segment, segIndex) => {
                        const linePath = buildLinePath(segment, xScale, yScale, minValue, maxValue);
                        const bandPath = buildBandPath(segment, xScale, yScale, minValue, maxValue);
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
                {markerPoints.map((item, index) => (
                  <circle
                    key={`marker:${index}:${item.marker.rel_path}`}
                    cx={item.x}
                    cy={item.y}
                    r={3.5}
                    className="pipelinesTelemetryMarkerDot"
                    role="button"
                    tabIndex={0}
                    aria-label={t("core.ui.pipelines.telemetry.overview.images", {}, "Stored images")}
                    onMouseEnter={() => {
                      if (pinnedMarker) return;
                      setHoveredMarker({ marker: item.marker, x: item.x });
                    }}
                    onMouseLeave={() => {
                      if (pinnedMarker) return;
                      setHoveredMarker((prev) => {
                        if (!prev) return prev;
                        const prevKey = `${prev.marker.rel_path}|${prev.marker.ts}`;
                        const curKey = `${item.marker.rel_path}|${item.marker.ts}`;
                        return prevKey === curKey ? null : prev;
                      });
                    }}
                    onFocus={() => {
                      if (pinnedMarker) return;
                      setHoveredMarker({ marker: item.marker, x: item.x });
                    }}
                    onBlur={() => {
                      if (pinnedMarker) return;
                      setHoveredMarker((prev) => {
                        if (!prev) return prev;
                        const prevKey = `${prev.marker.rel_path}|${prev.marker.ts}`;
                        const curKey = `${item.marker.rel_path}|${item.marker.ts}`;
                        return prevKey === curKey ? null : prev;
                      });
                    }}
                    onClick={() => {
                      const curKey = `${item.marker.rel_path}|${item.marker.ts}`;
                      setPinnedMarker((prev) => {
                        if (!prev) return { marker: item.marker, x: item.x };
                        const prevKey = `${prev.marker.rel_path}|${prev.marker.ts}`;
                        return prevKey === curKey ? null : { marker: item.marker, x: item.x };
                      });
                      setHoveredMarker(null);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        const curKey = `${item.marker.rel_path}|${item.marker.ts}`;
                        setPinnedMarker((prev) => {
                          if (!prev) return { marker: item.marker, x: item.x };
                          const prevKey = `${prev.marker.rel_path}|${prev.marker.ts}`;
                          return prevKey === curKey ? null : { marker: item.marker, x: item.x };
                        });
                        setHoveredMarker(null);
                      }
                    }}
                  />
                ))}
                {hoverTimeline ? (
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
              {activeMarker && markerOverlayStyle ? (
                <div
                  className={["pipelinesTelemetryMarkerOverlay", pinnedMarker ? "" : "isTransient"].filter(Boolean).join(" ")}
                  style={markerOverlayStyle}
                >
                  <div className="pipelinesStepStatsHeader">
                    <div className="pipelinesHint">{timeFormatter.format(new Date(Number(activeMarker.marker.ts || 0) * 1000))}</div>
                    {pinnedMarker ? (
                      <button className="iconButton" type="button" onClick={() => setPinnedMarker(null)} title={t("core.actions.close")}>
                        <i className="fa-solid fa-xmark" aria-hidden="true" />
                      </button>
                    ) : null}
                  </div>
                  <img
                    src={`/files/${encodeURI(String(activeMarker.marker.rel_path || ""))}`}
                    alt="marker preview"
                    className="pipelinesTelemetryMarkerImage"
                  />
                </div>
              ) : null}
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
