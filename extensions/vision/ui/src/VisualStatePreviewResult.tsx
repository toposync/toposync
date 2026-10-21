import React from "react";
import type { VisualClass, VisualPreview } from "./visualState";

export function VisualStatePreviewResult({ result, classes, stale, t }: {
  result: VisualPreview; classes: VisualClass[]; stale: boolean; t: (key: string) => string;
}) {
  const state = result.state, analysis = state.last_analysis;
  const name = (id: string) => classes.find(item => item.class_id === id)?.label ?? id;
  const time = (value: number) => <time dateTime={new Date(value * 1000).toISOString()}>{new Date(value * 1000).toLocaleString()}</time>;
  const reason = (value: string) => {
    const translated = t(`reason_${value}`);
    return translated.includes(`reason_${value}`) ? t("reason_other") : translated;
  };
  const field = (label: string, value: React.ReactNode) => <div className="visualEvidenceField"><dt>{t(label)}</dt><dd>{value}</dd></div>;
  return <div className="identityInspector visualPreviewResult" role="status">
    <strong>{t("preview_result")}: {state.class_id ? name(state.class_id) : t(`status_${state.status}`)}</strong>
    <span>{t(`status_${state.status}`)}</span>
    <p>{reason(state.reason)}</p>
    {time(result.completed_at)}
    {stale && <p>{t("preview_stale")}</p>}
    <p>{t("preview_finished")}</p>
    <dl>
      {state.candidate && field("candidate", <>{name(state.candidate.class_id)} · {time(state.candidate.started_at)}</>)}
      {state.last_confirmed && field("last_confirmed", <>{name(state.last_confirmed.class_id)} · {time(state.last_confirmed.observed_at)}</>)}
    </dl>
    {analysis && <details><summary>{t("last_analysis")}</summary>
      <p>{t("analysis_history")}</p><p>{reason(analysis.reason)}</p>
      <dl>
        {field("analysis_time", time(analysis.observed_at))}
        {field("analysis_age", `${state.analysis_age_ms} ms`)}
        {field("analysis_duration", `${analysis.duration_ms} ms`)}
        {field("region_quality", t(`quality_${analysis.region_quality}`))}
        {field("freshness_basis", t(`freshness_${analysis.freshness_basis}`))}
        {field("analysis_method", t(`method_${analysis.method}`))}
        {analysis.similarity !== null && field("similarity", analysis.similarity.toFixed(4))}
        {analysis.margin !== null && field("margin", analysis.margin.toFixed(4))}
        {analysis.declared_confidence !== null && field("declared_confidence", analysis.declared_confidence.toFixed(4))}
        {analysis.reference_ids.length > 0 && field("matched_references", analysis.reference_ids.map(id => {
          const category = classes.find(item => item.reference_ids.includes(id));
          return category ? `${category.label} · ${category.reference_ids.indexOf(id) + 1}` : id;
        }).join(", "))}
      </dl><p>{t("scores_not_accuracy")}</p>
    </details>}
    <details><summary>{t("technical_evidence")}</summary><dl>
      {field("runtime_epoch", state.runtime_epoch)}
      {field("state_version", state.state_version)}
      {field("preparation_revision", state.preparation_revision)}
      {field("reference_revision", state.reference_revision)}
      {field("model_revision", state.model_revision)}
      {analysis && field("sample_identity", `${analysis.sample.capture_instance} / ${analysis.sample.generation} / ${analysis.sample.sequence}`)}
    </dl></details>
  </div>;
}
