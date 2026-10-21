import React, { useEffect, useRef, useState } from "react";
import { readVisualAiOptions } from "./api";
import type { VisualConfiguration } from "./visualState";
import { aiPeriods, applyVisualAiValues, visualAiBudget, visualAiIssue, visualAiPauses, visualAiProfiles,
  visualAiValues, type VisualAiOptions } from "./visualStateAi";

export function VisualStateAiSettings({ value, change, disabled, advanced, editing, readiness, t }: {
  value: VisualConfiguration; change: (next: VisualConfiguration) => void; disabled: boolean; advanced?: boolean;
  editing: (dirty: boolean) => void; readiness: (ready: boolean) => void; t: (key: string) => string;
}) {
  const [options, setOptions] = useState<VisualAiOptions | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const request = useRef<AbortController | null>(null);
  const [fields, setFields] = useState(() => visualAiValues(value));
  const context = JSON.stringify(visualAiValues(value));
  const dirty = JSON.stringify(fields) !== context;
  const usesAi = value.strategy !== "examples";
  useEffect(() => { setFields(JSON.parse(context)); }, [context]);
  useEffect(() => { editing(usesAi && dirty); return () => editing(false); }, [usesAi, dirty]);
  useEffect(() => { setOptions(null); }, [value.sensor_id]);
  useEffect(() => {
    if (disabled || !usesAi) { request.current?.abort(); request.current = null; setLoading(false); }
    return () => { request.current?.abort(); request.current = null; };
  }, [disabled, usesAi, value.sensor_id]);
  const selected = options?.profiles.find(profile => profile.id === value.ai_profile_id);
  const profiles = options ? visualAiProfiles(value, options) : [];
  const ready = Boolean(options?.usage.available && selected?.enabled && profiles.some(profile =>
    !visualAiIssue(value, profile) && aiPeriods.every(period => visualAiBudget(value, options!, profile, period).remaining !== 0)));
  useEffect(() => { readiness(ready && !dirty && !failed); }, [ready, dirty, failed]);
  let next: VisualConfiguration | null = null;
  try { next = applyVisualAiValues(value, fields); } catch { /* Preserve incomplete input until applied. */ }
  async function load() {
    if (disabled || request.current) return;
    const controller = new AbortController(); request.current = controller; setLoading(true); setFailed(false);
    const timer = setTimeout(() => { controller.abort(); if (request.current === controller) {
      request.current = null; setLoading(false); setFailed(true); setOptions(null);
    } }, 15000);
    try {
      const result = await readVisualAiOptions(value.sensor_id, controller.signal);
      if (!controller.signal.aborted) setOptions(result);
    } catch { if (!controller.signal.aborted) { setFailed(true); setOptions(null); } }
    finally { clearTimeout(timer); if (request.current === controller) { request.current = null; setLoading(false); } }
  }
  const date = (timestamp: number) => new Date(timestamp * 1000).toLocaleString();
  return <fieldset disabled={disabled}>
    <legend>{t("strategy")}</legend>
    <label className="identityField"><span>{t("strategy")}</span><select value={value.strategy}
      onChange={event => change({ ...value, strategy: event.target.value as VisualConfiguration["strategy"] })}>
      {["examples", "fallback", "direct"].map(mode => <option key={mode} value={mode}>{t(`strategy_${mode}`)}</option>)}
    </select></label><p className="identityMuted">{t(`strategy_${value.strategy}_help`)}</p>
    {usesAi && <>
      <label className="identityField"><span>{t("ai_profile")}</span><select value={String(value.ai_profile_id ?? "")} disabled={!options || loading}
        onChange={event => change({ ...value, ai_profile_id: event.target.value || null })}>
        <option value="">{t("ai_choose_profile")}</option>
        {value.ai_profile_id && !selected && <option value={String(value.ai_profile_id)} disabled>{String(value.ai_profile_id)}</option>}
        {options?.profiles.map(profile => <option key={profile.id} value={profile.id} disabled={!profile.enabled}>
          {profile.name || profile.id} · {t(profile.local ? "ai_local" : "ai_remote")}</option>)}
      </select></label>
      <button type="button" className="chipButton" disabled={loading} onClick={() => void load()}>{t(loading ? "ai_loading" : "ai_refresh")}</button>
      <p className="identityMuted">{t("ai_settings_location")}</p>
      {failed && <p role="alert">{t("ai_options_failed")}</p>}
      {options && !selected && <p role="status">{t("ai_profile_unavailable")}</p>}
      {options && !options.usage.available && <p role="alert">{t("ai_usage_unavailable")}</p>}
      <label><input type="checkbox" checked={value.ai_allow_remote_images === true}
        onChange={event => change({ ...value, ai_allow_remote_images: event.target.checked })} />{t("ai_remote_consent")}</label>
      <p className="identityMuted">{t("ai_consent_scope")}</p>
      {options && profiles.map(profile => {
        const issue = visualAiIssue(value, profile);
        const temporal = value.temporal as { maximum_frame_age_ms?: number; validity_ms?: number; maximum_gap_ms?: number } | undefined;
        const deadline = Math.min(Number(value.ai_timeout_ms ?? 60000), temporal?.maximum_frame_age_ms ?? 750, profile.timeout_seconds * 1000);
        const exhausted = aiPeriods.some(period => visualAiBudget(value, options, profile, period).remaining === 0);
        const budgetSlow = aiPeriods.some((period, index) => {
          const limit = visualAiBudget(value, options, profile, period).limit;
          const interval = limit === null ? 0 : [60000, 3600000, 86400000, 2419200000][index] / limit;
          return interval > (temporal?.maximum_gap_ms ?? 750) || interval >= (temporal?.validity_ms ?? 2000);
        });
        return <div key={profile.id} className="identityInspector">
          <strong>{profile.name || profile.id} · {profile.model} · {t(profile.local ? "ai_local" : "ai_remote")}</strong>
          {issue && <p role="alert">{t(issue)}</p>}
          <p>{t("ai_effective_deadline")}: {deadline} ms. {t("ai_deadline_help")}</p>
          <p>{t("ai_image_capacity_label")}: {profile.max_images_per_request}</p>
          {exhausted && <p role="alert">{t("ai_limit_reached")}</p>}
          {value.strategy === "direct" && budgetSlow && <p role="alert">{t("ai_budget_too_slow")}</p>}
          {visualAiPauses(options, profile).map(pause => <p role="status" key={`${pause.scope}:${pause.id}`}>
            {t(pause.retry_at === null ? "ai_intervention" : pause.recovery_pending ? "ai_recovery_pending" : "ai_paused")}
            {pause.retry_at !== null && <> {t("ai_retry_at")}: {date(pause.retry_at)}.</>}
          </p>)}
          <details><summary>{t("ai_effective_limits")}</summary>
            <div className="visualAiLimits" role="region" aria-label={t("ai_effective_limits")} tabIndex={0}>
            <table><thead><tr><th>{t("ai_period")}</th><th>{t("ai_limit")}</th><th>{t("ai_remaining")}</th><th>{t("ai_reset")}</th></tr></thead>
              <tbody>{aiPeriods.map(period => { const budget = visualAiBudget(value, options, profile, period); return <tr key={period}>
                <th scope="row">{t(`ai_${period}`)}</th><td>{budget.limit ?? t("ai_unlimited")}</td>
                <td>{!options.usage.available ? t("ai_unknown") : budget.remaining ?? t("ai_unlimited")}</td>
                <td>{options.usage.resets_at[period] ? date(options.usage.resets_at[period]) : t("ai_unknown")}</td>
              </tr>; })}</tbody></table>
            </div>
            <p>{t("ai_limits_snapshot")}: {date(options.checked_at)}. {t("ai_limits_help")}</p>
          </details>
        </div>;
      })}
      <label className="identityField"><span>{t("scene_description")}</span><textarea className="input" rows={3} maxLength={2000}
        value={value.scene_description} onChange={event => change({ ...value, scene_description: event.target.value })} /></label>
      <details open={advanced || undefined}><summary>{t("ai_advanced")}</summary><p>{t("ai_limits_local_help")}</p>
        <div className="visualDecisionFields">{Object.keys(fields).map(key => <label key={key} className="identityField"><span>{t(key)}</span>
          <input type="number" min={1} step={1} max={key === "ai_timeout_ms" ? 120000 : key === "ai_max_attempts" ? 3 : key === "ai_references_per_class" ? 30 : undefined}
            value={fields[key]} onChange={event => setFields({ ...fields, [key]: event.target.value })} /></label>)}</div>
        <p>{t("ai_references_help")}</p>
        {dirty && <p role="status">{t(next ? "settings_pending" : "ai_settings_invalid")}</p>}
        <button type="button" className="chipButton" disabled={!dirty || !next} onClick={() => { if (next) change(next); }}>{t("apply_settings")}</button>
        {dirty && <button type="button" className="chipButton" onClick={() => setFields(JSON.parse(context))}>{t("discard_settings")}</button>}
      </details>
    </>}
  </fieldset>;
}
