import React, { useEffect, useRef, useState } from "react";
import { readVisualModels } from "./api";
import { selectVisualModel, type VisualConfiguration, type VisualModel } from "./visualState";

export function VisualStateModelSettings({ value, change, disabled, t }: {
  value: VisualConfiguration; change: (next: VisualConfiguration) => void; disabled: boolean;
  t: (key: string) => string;
}) {
  const [models, setModels] = useState<VisualModel[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    if (disabled) { request.current?.abort(); request.current = null; setLoading(false); }
    return () => { request.current?.abort(); request.current = null; };
  }, [disabled]);
  async function load() {
    if (disabled || request.current) return;
    const controller = new AbortController(); request.current = controller; setLoading(true); setFailed(false);
    const timeout = setTimeout(() => {
      controller.abort();
      if (request.current === controller) { request.current = null; setLoading(false); setFailed(true); }
    }, 15_000);
    try {
      const result = await readVisualModels(controller.signal);
      if (!controller.signal.aborted) setModels(result.choices);
    } catch { if (!controller.signal.aborted) setFailed(true); }
    finally { clearTimeout(timeout); if (request.current === controller) { request.current = null; setLoading(false); } }
  }
  const selected = models?.find(model => model.model_id === value.model_id);
  const changedRevision = selected && selected.revision !== value.model_revision;
  return <fieldset disabled={disabled}>
    <legend>{t("local_model")}</legend>
    <label className="identityField"><span>{t("model")}</span>
      <select value={value.model_id ?? ""} disabled={!models || loading} onChange={event => {
        const model = models?.find(item => item.model_id === event.target.value);
        if (model?.artifact_present) change(selectVisualModel(value, model));
      }}>
        <option value="" disabled>{t("choose_model")}</option>
        {value.model_id && !selected && <option value={value.model_id} disabled>{value.model_id}</option>}
        {models?.map(model => <option key={model.model_id} value={model.model_id} disabled={!model.artifact_present}>
          {model.name}{model.artifact_present ? "" : ` (${t("model_not_installed")})`}
        </option>)}
      </select>
    </label>
    <button type="button" className="chipButton" disabled={loading} onClick={() => void load()}>{t(loading ? "models_loading" : "load_models")}</button>
    {failed && <p role="alert">{t("models_failed")}</p>}
    {models?.length === 0 && <p role="status">{t("models_empty")}</p>}
    {models && value.model_id && (!selected || !selected.artifact_present) && <p role="alert">{t("model_unavailable")}</p>}
    {changedRevision && <p role="alert">{t("model_changed")}</p>}
    {changedRevision && selected.artifact_present && <button type="button" className="chipButton"
      onClick={() => change(selectVisualModel(value, selected))}>{t("use_model_revision")}</button>}
    <p className="identityMuted">{t("model_help")}</p>
  </fieldset>;
}
