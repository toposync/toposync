import React, { useEffect, useMemo, useState } from "react";

import { getPipelineTelemetryNumeric, type PipelineTelemetryNumeric } from "../../../util/api";
import { i18n } from "../../../util/i18n";
import { Modal } from "../../Modal";
import type { TelemetryFieldInspectorRequest } from "./types";

type Props = {
  open: boolean;
  pipelineName: string | null;
  request: TelemetryFieldInspectorRequest | null;
  refreshNonce?: number;
  onClose: () => void;
  onApplyValue: (value: number) => void;
};

function percentileFromHistogram(
  histogram: number[],
  histogramMin: number,
  histogramMax: number,
  percentile: number,
): number | null {
  if (!Array.isArray(histogram) || histogram.length === 0) return null;
  const total = histogram.reduce((acc, item) => acc + Math.max(0, Number(item) || 0), 0);
  if (total <= 0) return null;
  const target = total * Math.max(0, Math.min(1, percentile));
  let cumulative = 0;
  const span = Math.max(1e-9, histogramMax - histogramMin);
  for (let index = 0; index < histogram.length; index += 1) {
    cumulative += Math.max(0, Number(histogram[index]) || 0);
    if (cumulative >= target) {
      const ratio = (index + 0.5) / histogram.length;
      return histogramMin + ratio * span;
    }
  }
  return histogramMax;
}

function clamp(value: number, minValue: number, maxValue: number): number {
  if (!Number.isFinite(value)) return minValue;
  return Math.min(maxValue, Math.max(minValue, value));
}

export function PipelineTelemetryFieldModal({
  open,
  pipelineName,
  request,
  refreshNonce,
  onClose,
  onApplyValue,
}: Props): React.ReactElement | null {
  const { t, locale } = i18n.useI18n();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<PipelineTelemetryNumeric | null>(null);
  const [pendingValue, setPendingValue] = useState(0);

  const integerFormatter = useMemo(() => new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }), [locale]);
  const decimalFormatter = useMemo(
    () =>
      new Intl.NumberFormat(locale, {
        minimumFractionDigits: 4,
        maximumFractionDigits: 4,
      }),
    [locale],
  );

  useEffect(() => {
    if (!open) return;
    if (!request) return;
    setPendingValue(Number.isFinite(request.value) ? request.value : 0);
  }, [open, request]);

	  useEffect(() => {
	    if (!open || !pipelineName || !request) {
	      setData(null);
      setError(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    getPipelineTelemetryNumeric(pipelineName, request.nodeId, request.metricId, 800)
      .then((response) => {
        if (cancelled) return;
        setData(response);
      })
      .catch((err: any) => {
        if (cancelled) return;
        setData(null);
        setError(String(err?.message ?? err));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });

	    return () => {
	      cancelled = true;
	    };
	  }, [open, pipelineName, request?.nodeId, request?.metricId, refreshNonce]);

  const histogram = data?.histogram_bins ?? [];
  const histogramMaxCount = useMemo(
    () => Math.max(1, ...histogram.map((item) => Math.max(0, Number(item) || 0))),
    [histogram],
  );

  const histogramMin = Number(data?.histogram_min ?? 0);
  const histogramMax = Number(data?.histogram_max ?? 1);
  const markerValue = clamp(pendingValue, histogramMin, histogramMax);

  const p90 = useMemo(
    () => percentileFromHistogram(histogram, histogramMin, histogramMax, 0.9),
    [histogram, histogramMin, histogramMax],
  );
  const p95 = useMemo(
    () => percentileFromHistogram(histogram, histogramMin, histogramMax, 0.95),
    [histogram, histogramMin, histogramMax],
  );
  const p99 = useMemo(
    () => percentileFromHistogram(histogram, histogramMin, histogramMax, 0.99),
    [histogram, histogramMin, histogramMax],
  );

  const chartWidth = 720;
  const chartHeight = 220;
  const usableHeight = chartHeight - 24;

  return (
    <Modal
      open={open}
      title={t("core.ui.pipelines.telemetry.field.title", { label: request?.label ?? "" }, "Parameter insights")}
      onClose={onClose}
      panelClassName="pipelinesTelemetryModalPanel"
      bodyClassName="pipelinesTelemetryModalBody"
    >
      <div className="pipelinesHint">
        {request
          ? t(
              "core.ui.pipelines.telemetry.field.subtitle",
              { node: request.nodeId, metric: request.metricId },
              "Node {{node}} • Metric {{metric}}",
            )
          : ""}
      </div>

      {loading ? <div className="pipelinesHint">{t("core.ui.pipelines.telemetry.loading", {}, "Loading telemetry…")}</div> : null}
      {error ? <div className="pipelinesInlineError">{t("core.ui.pipelines.telemetry.error", { error }, "Telemetry unavailable: {{error}}")}</div> : null}

      {!loading && !error && data && Number(data.total_count || 0) <= 0 ? (
        <div className="pipelinesHint">
          {t(
            "core.ui.pipelines.telemetry.no_data",
            {},
            "No telemetry samples yet. Let the pipeline run and reopen this panel.",
          )}
        </div>
      ) : null}

      {!loading && !error && data && Number(data.total_count || 0) > 0 ? (
        <>
          <div className="pipelinesTelemetrySummaryRow">
            <div className="pipelinesStatsItem">
              <div className="pipelinesHint">{t("core.ui.pipelines.telemetry.total_count", {}, "Samples")}</div>
              <div className="pipelinesStatsValue">{integerFormatter.format(Number(data.total_count || 0))}</div>
            </div>
            <div className="pipelinesStatsItem">
              <div className="pipelinesHint">{t("core.ui.pipelines.telemetry.total_min", {}, "Min")}</div>
              <div className="pipelinesStatsValue">{decimalFormatter.format(Number(data.total_min || 0))}</div>
            </div>
            <div className="pipelinesStatsItem">
              <div className="pipelinesHint">{t("core.ui.pipelines.telemetry.total_avg", {}, "Avg")}</div>
              <div className="pipelinesStatsValue">{decimalFormatter.format(Number(data.total_avg || 0))}</div>
            </div>
            <div className="pipelinesStatsItem">
              <div className="pipelinesHint">{t("core.ui.pipelines.telemetry.total_max", {}, "Max")}</div>
              <div className="pipelinesStatsValue">{decimalFormatter.format(Number(data.total_max || 0))}</div>
            </div>
          </div>

          <div className="pipelinesTelemetryHistogramWrap">
            <svg className="pipelinesTelemetryHistogram" viewBox={`0 0 ${chartWidth} ${chartHeight}`} preserveAspectRatio="none" aria-hidden="true">
              {histogram.map((countRaw, index) => {
                const count = Math.max(0, Number(countRaw) || 0);
                const barWidth = chartWidth / Math.max(1, histogram.length);
                const x = index * barWidth;
                const barHeight = (count / histogramMaxCount) * usableHeight;
                const y = chartHeight - barHeight - 12;
                return <rect key={`hist:${index}`} x={x} y={y} width={Math.max(1, barWidth - 1)} height={barHeight} rx={1} className="pipelinesTelemetryHistogramBar" />;
              })}
              {(() => {
                const span = Math.max(1e-9, histogramMax - histogramMin);
                const x = ((markerValue - histogramMin) / span) * chartWidth;
                return <line x1={x} x2={x} y1={0} y2={chartHeight} className="pipelinesTelemetryMarkerLine" />;
              })()}
            </svg>
            <div className="pipelinesTelemetryHistogramAxis">
              <span>{decimalFormatter.format(histogramMin)}</span>
              <span>{decimalFormatter.format(histogramMax)}</span>
            </div>
          </div>

          <div className="pipelinesTelemetryValueRow">
            <label className="pipelinesLabel">
              <span>{t("core.ui.pipelines.telemetry.current_value", {}, "Current value")}</span>
              <input
                className="pipelinesInput"
                type="number"
                step="0.001"
                value={Number.isFinite(pendingValue) ? pendingValue : 0}
                onChange={(event) => setPendingValue(Number(event.target.value))}
              />
            </label>
            <button
              className="pillButton pillButtonPrimary"
              type="button"
              onClick={() => {
                onApplyValue(pendingValue);
                onClose();
              }}
            >
              <i className="fa-solid fa-check" aria-hidden="true" />
              {t("core.ui.pipelines.telemetry.apply_value", {}, "Apply")}
            </button>
          </div>

          <div className="pipelinesTelemetryQuickRow">
            {p90 !== null ? (
              <button className="pillButton" type="button" onClick={() => setPendingValue(p90)}>
                P90 {decimalFormatter.format(p90)}
              </button>
            ) : null}
            {p95 !== null ? (
              <button className="pillButton" type="button" onClick={() => setPendingValue(p95)}>
                P95 {decimalFormatter.format(p95)}
              </button>
            ) : null}
            {p99 !== null ? (
              <button className="pillButton" type="button" onClick={() => setPendingValue(p99)}>
                P99 {decimalFormatter.format(p99)}
              </button>
            ) : null}
          </div>
        </>
      ) : null}
    </Modal>
  );
}
