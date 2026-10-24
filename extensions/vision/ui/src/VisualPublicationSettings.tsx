import React, { useEffect, useState } from "react";
import type { VisualConfiguration } from "./visualState";
import { applyVisualPublication, mapPublicationClass, publicationClassSide, publicationClassValue,
  publicationDefaults, publicationIssue, visualPublication, type VisualPublication } from "./visualPublication";

export function VisualPublicationSettings({ value, change, disabled, editing, t }: {
  value: VisualConfiguration; change: (next: VisualConfiguration) => void; disabled: boolean;
  editing: (dirty: boolean) => void; t: (key: string) => string;
}) {
  const context = JSON.stringify(visualPublication(value));
  const [fields, setFields] = useState(() => visualPublication(value));
  const dirty = JSON.stringify(fields) !== context;
  useEffect(() => { setFields(JSON.parse(context)); }, [context, value.sensor_id]);
  useEffect(() => { editing(dirty); return () => editing(false); }, [dirty]);
  const issue = publicationIssue(value, fields);
  const missing = [...new Set([fields.on_class_ids, fields.off_class_ids].flatMap(ids => Array.isArray(ids) ? ids : []))]
    .filter(id => typeof id === "string" && !value.classes.some(category => category?.class_id === id));
  const sideControl = (identifier: string, label: string) => <label className="identityField"><span>{label}</span>
    <select value={publicationClassSide(fields, identifier)} onChange={event =>
      setFields(mapPublicationClass(fields, identifier, event.target.value as "on" | "off" | "unknown"))}>
      <option value="unknown">{t("publication_unknown")}</option><option value="on">{t("publication_on")}</option>
      <option value="off">{t("publication_off")}</option>
    </select></label>;
  return <fieldset disabled={disabled} className="visualPublicationSettings">
    <legend>{t("publication_title")}</legend>
    <p>{t(visualPublication(value).enabled === true ? "publication_configured" : "publication_not_configured")}</p>
    <details open={fields.enabled === true || dirty || Boolean(issue) || undefined}>
      <summary>{t("publication_configure")}</summary>
      <p className="identityMuted">{t("publication_help")}</p>
      <label className="identityField"><span>{t("publication_kind")}</span><select value={typeof fields.kind === "string" ? fields.kind : ""}
        onChange={event => setFields({ ...fields, kind: event.target.value as VisualPublication["kind"] })}>
        <option value="enum">{t("publication_enum")}</option><option value="binary">{t("publication_binary")}</option>
      </select></label>
      <label className="identityField"><span>{t("publication_name")}</span><input maxLength={80}
        value={typeof fields.name === "string" ? fields.name : ""} placeholder={`Toposync ${value.sensor_id}`}
        onChange={event => setFields({ ...fields, name: event.target.value })} /></label>
      <h4>{t("publication_preview")}</h4><p className="identityMuted">{t("publication_preview_help")}</p>
      <div className="visualPublicationRows">{value.classes.map(category => category && typeof category === "object" && <div key={category.class_id} className="visualPublicationRow">
        {fields.kind === "binary" ? sideControl(category.class_id, category.label) : <strong>{category.label}</strong>}
        <span>{!category.enabled ? t("publication_disabled_class") : fields.kind === "enum"
          ? <code>{publicationClassValue(fields, category)}</code> : t(`publication_${publicationClassValue(fields, category)}`)}</span>
      </div>)}
        <div className="visualPublicationRow"><span>{t("publication_inconclusive")}</span><span>{t("publication_unknown")}</span></div>
        <div className="visualPublicationRow"><span>{t("publication_expired")}</span><span>{t("publication_unknown")}</span></div>
        <div className="visualPublicationRow"><span>{t("publication_disconnected")}</span><span>{t("publication_unavailable")}</span></div>
      </div>
      {fields.kind === "enum" && [fields.on_class_ids, fields.off_class_ids].some(ids => Array.isArray(ids) && ids.length > 0) &&
        <button type="button" className="chipButton" onClick={() => setFields({ ...fields, on_class_ids: [], off_class_ids: [] })}>{t("publication_clear_mapping")}</button>}
      {missing.map(identifier => <div key={identifier} className="visualPublicationRow">
        {sideControl(identifier, `${t("publication_missing_class")}: ${identifier}`)}
        <button type="button" className="chipButton" onClick={() => setFields(mapPublicationClass(fields, identifier, "unknown"))}>{t("publication_unmap")}</button>
      </div>)}
      <label><input type="checkbox" checked={fields.enabled === true} onChange={event => setFields({ ...fields, enabled: event.target.checked })} />{t("publication_enable")}</label>
      {issue && <p role="alert">{t(issue)}</p>}
      {dirty && <p role="status">{t("publication_pending")}</p>}
      <div className="identityToolbar">
        <button type="button" className="chipButton" disabled={!dirty || Boolean(issue)}
          onClick={() => change(applyVisualPublication(value, fields))}>{t("publication_apply")}</button>
        {dirty && <button type="button" className="chipButton" onClick={() => setFields(JSON.parse(context))}>{t("discard_settings")}</button>}
        {issue && <button type="button" className="chipButton" onClick={() => setFields(publicationDefaults())}>{t("publication_reset")}</button>}
      </div>
      <p className="identityMuted">{t("publication_identity")}</p><p className="identityMuted">{t("publication_legacy")}</p>
    </details>
  </fieldset>;
}
