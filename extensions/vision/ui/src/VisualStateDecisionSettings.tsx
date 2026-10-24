import React, { useEffect, useState } from "react";
import { applyVisualDecisionValues, visualComparisonFields, visualDecisionBounds, visualDecisionValues, visualTemporalDefaults, type VisualConfiguration } from "./visualState";

export function VisualStateDecisionSettings({ value, change, disabled, editing, t }: {
  value: VisualConfiguration; change: (next: VisualConfiguration) => void; disabled: boolean;
  editing: (dirty: boolean) => void; t: (key: string) => string;
}) {
  const [fields, setFields] = useState(() => visualDecisionValues(value));
  const context = JSON.stringify(visualDecisionValues(value));
  const dirty = JSON.stringify(fields) !== context;
  useEffect(() => { setFields(JSON.parse(context)); editing(false); }, [context]);
  useEffect(() => { editing(dirty); }, [dirty]);
  let next: VisualConfiguration | null = null;
  try { next = applyVisualDecisionValues(value, fields); } catch { /* Incomplete fields stay local until applied. */ }
  function input(key: string) {
    const [minimum, maximum, step] = visualDecisionBounds(key);
    return <label key={key} className="identityField"><span>{t(key)}</span><input type="number" min={minimum} max={maximum} step={step}
      value={fields[key] ?? ""} onChange={event => setFields({ ...fields, [key]: event.target.value })} /></label>;
  }
  return <fieldset disabled={disabled}>
    <legend>{t("decision_settings")}</legend>
    {value.strategy !== "direct" && <><p className="identityMuted">{t("comparison_help")}</p>
    <details open={!value.comparison || undefined}>
      <summary>{t("comparison_settings")}</summary>
      <div className="visualDecisionFields">{visualComparisonFields.map(input)}</div>
    </details></>}
    <details><summary>{t("temporal_settings")}</summary><p className="identityMuted">{t("temporal_help")}</p>
      <div className="visualDecisionFields">
        {input("evaluation_interval_ms")}
        {Object.keys(visualTemporalDefaults).map(input)}
      </div>
    </details>
    {dirty && <p role="status">{t(next ? "settings_pending" : "decision_invalid")}</p>}
    <div className="identityToolbar">
      <button type="button" className="chipButton" disabled={!dirty || !next} onClick={() => { if (next) change(next); }}>{t("apply_settings")}</button>
      {dirty && <button type="button" className="chipButton" onClick={() => setFields(JSON.parse(context))}>{t("discard_settings")}</button>}
    </div>
  </fieldset>;
}
