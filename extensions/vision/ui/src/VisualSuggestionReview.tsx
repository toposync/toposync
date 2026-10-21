import React, { useEffect, useState } from "react";
import type { VisualConfiguration } from "./visualState";
import { acceptedSuggestion, applySuggestionLimits, suggestionPolicy, type VisualSuggestion, type VisualSuggestions } from "./visualStateSuggestions";
import { visualSuggestionUrl } from "./api";

export function VisualStateSuggestions({ value, change, disabled, stale, advanced, editing, data, draftId, offset,
  refresh, review, restore, enlarge, t }: {
  value: VisualConfiguration; change: (next: VisualConfiguration) => void; disabled: boolean; stale: boolean;
  advanced?: boolean; editing: (dirty: boolean) => void; data: VisualSuggestions | null; draftId: string | null; offset: number;
  refresh: (offset: number) => void; review: (item: VisualSuggestion, action: "accept" | "reject" | "undo", classId: string | null) => void;
  restore: (item: VisualSuggestion) => void; enlarge: (url: string, label: string) => void; t: (key: string) => string;
}) {
  const policy = suggestionPolicy(value), context = JSON.stringify(policy);
  const [maximum, setMaximum] = useState(String(policy.maximum_pending));
  const [days, setDays] = useState(String(policy.retention_days));
  const [selected, setSelected] = useState<Record<string, string>>({});
  const dirty = maximum !== String(policy.maximum_pending) || days !== String(policy.retention_days);
  useEffect(() => { setMaximum(String(policy.maximum_pending)); setDays(String(policy.retention_days)); }, [context]);
  useEffect(() => { editing(dirty); return () => editing(false); }, [dirty]);
  useEffect(() => { setSelected({}); }, [value.sensor_id]);
  let next: VisualConfiguration | null = null;
  try { next = applySuggestionLimits(value, maximum, days); } catch { /* Preserve incomplete input until applied. */ }
  const date = (timestamp: number) => new Date(timestamp * 1000).toLocaleString();
  const label = (id: string) => value.classes.find(category => category.class_id === id)?.label ?? id;
  return <fieldset disabled={disabled}>
    <legend>{t("suggestions")}</legend><p className="identityMuted">{t("suggestions_help")}</p>
    <label><input type="checkbox" checked={policy.mode === "review"} onChange={event => change({ ...value,
      suggestions: { ...policy, mode: event.target.checked ? "review" : "off" } })} />{t("suggestions_collect")}</label>
    {value.strategy === "examples" && <p className="identityMuted">{t("suggestions_examples")}</p>}
    <details open={advanced || undefined}><summary>{t("suggestions_limits")}</summary>
      <label className="identityField"><span>{t("suggestions_maximum")}</span><input type="number" min={1} max={100} step={1}
        value={maximum} onChange={event => setMaximum(event.target.value)} /></label>
      <label className="identityField"><span>{t("suggestions_days")}</span><input type="number" min={1} max={30} step={1}
        value={days} onChange={event => setDays(event.target.value)} /></label>
      {dirty && <p role="status">{t(next ? "settings_pending" : "suggestion_invalid_limits")}</p>}
      <button type="button" className="chipButton" disabled={!dirty || !next} onClick={() => { if (next) change(next); }}>{t("apply_settings")}</button>
      {dirty && <button type="button" className="chipButton" onClick={() => {
        setMaximum(String(policy.maximum_pending)); setDays(String(policy.retention_days));
      }}>{t("discard_settings")}</button>}
    </details>
    <button type="button" className="chipButton" disabled={dirty} onClick={() => refresh(0)}>{t("suggestions_refresh")}</button>
    {stale && data && <p role="status">{t("suggestions_stale")}</p>}
    {data && <>
      <p>{t("suggestions_pending")}: {data.summary.pending} / {data.summary.policy.maximum_pending}. {t("suggestions_per_class")}: {data.summary.maximum_per_class}.</p>
      {data.summary.pending >= data.summary.policy.maximum_pending && <p role="status">{t("suggestions_full")}</p>}
      {!data.items.length && <p>{t("suggestions_empty")}</p>}
      {data.items.map(item => {
        const accepted = acceptedSuggestion(value, item), expired = item.expires_at * 1000 <= Date.now();
        const target = selected[item.suggestion_id] ?? item.reference.class_id;
        const eligible = value.classes.some(category => category.class_id === target && category.enabled);
        const locked = stale || dirty;
        const title = `${label(item.reference.class_id)} · ${date(item.reference.evidence.observed_at)}`;
        const provenance = item.reference.provenance;
        return <div className="identityInspector visualSuggestionCard" key={item.suggestion_id}>
          <strong>{t("suggestions_proposed_class")}: {label(item.reference.class_id)} · {t(item.status === "pending" ? "suggestions_awaiting_review" : `suggestions_${item.status}`)}</strong>
          {draftId && (!expired || accepted?.attached) && <div className="visualStatePhotos"><div className="visualStatePhoto">
            <button type="button" className="identityPhotoButton" aria-label={`${t("enlarge")} ${title}`}
              onClick={() => enlarge(visualSuggestionUrl(draftId, item.suggestion_id, false), title)}>
              <img src={visualSuggestionUrl(draftId, item.suggestion_id)} alt={title} loading="lazy" />
            </button>
          </div></div>}
          <p>{t("suggestions_observed")}: {date(item.reference.evidence.observed_at)}. {t("suggestions_expires")}: {date(item.expires_at)}.</p>
          {!item.compatible && <p role="status">{t("suggestions_incompatible")}</p>}
          {expired && <p role="status">{t("suggestions_expired")}</p>}
          <details><summary>{t("suggestions_origin")}</summary>
            <p>{provenance.profile_id} · {provenance.provider_id} · {provenance.model}</p>
            <p>{provenance.reason}</p><p>{t("suggestions_confidence")}: {provenance.declared_confidence ?? "—"}. {t("suggestions_historical")}</p>
            <dl><dt>{t("suggestions_epoch")}</dt><dd>{provenance.runtime_epoch}</dd>
              <dt>{t("suggestions_sample")}</dt><dd>{item.reference.evidence.sample.capture_instance} · {item.reference.evidence.sample.generation} · {item.reference.evidence.sample.sequence}</dd>
              <dt>{t("suggestions_reference_revision")}</dt><dd>{provenance.reference_revision}</dd>
              <dt>{t("suggestions_ai_revision")}</dt><dd>{provenance.ai_configuration_revision}</dd></dl>
            {item.reviews.map((entry, index) => <p key={index}>{t(`suggestions_action_${entry.action}`)} · {entry.actor_id} · {date(entry.reviewed_at)}
              {entry.class_id && <> · {label(entry.class_id)}</>}</p>)}
          </details>
          {item.status === "pending" && !expired && <>
            <label className="identityField"><span>{t("suggestions_correct_class")}</span><select value={target} disabled={locked || !item.compatible}
              onChange={event => setSelected({ ...selected, [item.suggestion_id]: event.target.value })}>
              {!eligible && <option value={target} disabled>{label(target)}</option>}
              {value.classes.filter(category => category.enabled).map(category => <option key={category.class_id} value={category.class_id}>{category.label}</option>)}
            </select></label>
            <button type="button" className="chipButton" disabled={locked || !item.compatible || !eligible}
              onClick={() => review(item, "accept", target)}>{t("suggestions_action_accept")}</button>
            <button type="button" className="chipButton" disabled={locked} onClick={() => review(item, "reject", null)}>{t("suggestions_action_reject")}</button>
          </>}
          {accepted && <p>{t("suggestions_approved_class")}: {label(accepted.class_id!)}. {t(accepted.attached ? "suggestions_in_draft" : "suggestions_not_in_draft")}</p>}
          {accepted && !accepted.attached && <button type="button" className="chipButton" disabled={locked || !item.compatible
            || !value.classes.some(category => category.class_id === accepted.class_id && category.enabled)}
            onClick={() => restore(item)}>{t("suggestions_restore")}</button>}
          {(accepted?.attached || item.status === "rejected" && !expired) && <button type="button" className="chipButton" disabled={locked}
            onClick={() => review(item, "undo", null)}>{t("suggestions_action_undo")}</button>}
        </div>;
      })}
      <div className="identityToolbar">
        {offset > 0 && <button type="button" className="chipButton" disabled={dirty} onClick={() => refresh(Math.max(0, offset - 100))}>{t("suggestions_previous")}</button>}
        {data.next_offset !== null && <button type="button" className="chipButton" disabled={dirty} onClick={() => refresh(data.next_offset!)}>{t("suggestions_next")}</button>}
      </div>
    </>}
  </fieldset>;
}
