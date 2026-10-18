import type { PipelineOperatorPanel } from "@toposync/plugin-api";
import React, { useEffect, useRef, useState } from "react";
import { fetchAutomatic, previewAutomatic, saveAutomatic } from "../../util/api";
import { i18n } from "../../util/i18n";
import { replaceAutomaticIntent, sameAutomaticTarget } from "../../util/automatic";
import type { AutomaticConfiguration, AutomaticField, AutomaticIntent, AutomaticPreview, AutomaticSnapshot, AutomaticTarget } from "../../util/automatic";
import { automaticText, AutomaticMessage } from "./messages";

export function AutomaticScreen({ onOpenFlows, onDirtyChange, operatorPanels }: { onOpenFlows: () => void; onDirtyChange: (dirty: boolean) => void; operatorPanels: Record<string, PipelineOperatorPanel> }) {
  const { locale } = i18n.useI18n();
  const t = (key: AutomaticMessage) => automaticText(locale, key);
  const title = (value: Record<string, string> | undefined, fallback: string) => value?.[locale] || value?.en || fallback;
  const [snapshot, setSnapshot] = useState<AutomaticSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [errorDuringLoad, setErrorDuringLoad] = useState(false);
  const [reload, setReload] = useState(0);
  const [editing, setEditing] = useState<{ intent: AutomaticIntent; configuration: AutomaticConfiguration; activate: boolean } | null>(null);
  const [review, setReview] = useState<{ configuration: AutomaticConfiguration; preview: AutomaticPreview } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const dirty = Boolean(editing || review);
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    setError(null);
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const next = await fetchAutomatic(abort.signal);
        if (abort.signal.aborted) return;
        setSnapshot(previous => !previous || next.configuration.revision >= previous.configuration.revision ? next : previous);
        timer = setTimeout(load, 3000);
      } catch (reason) {
        if (!abort.signal.aborted) { setErrorDuringLoad(true); setError(reason instanceof Error ? reason.message : t("loadError")); }
      }
    };
    void load();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [reload]);
  useEffect(() => { if (editing || review) heading.current?.focus(); }, [Boolean(editing), Boolean(review)]);

  const commit = async (configuration: AutomaticConfiguration) => {
    setBusy(true); setError(null); setErrorDuringLoad(false);
    try {
      const saved = await saveAutomatic(configuration, configuration.revision);
      setSnapshot(previous => ({ ...previous!, ...saved }));
      setEditing(null); setReview(null); setReload(value => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : t("saveError")); }
    finally { setBusy(false); }
  };
  const prepare = async (configuration: AutomaticConfiguration) => {
    setBusy(true); setError(null); setErrorDuringLoad(false);
    try { setReview({ configuration, preview: await previewAutomatic(configuration, configuration.revision) }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t("saveError")); }
    finally { setBusy(false); }
  };
  if (!snapshot) return <div role="status">{error || t("loading")}{error && <button className="chipButton" onClick={() => setReload(value => value + 1)}>{t("retry")}</button>}</div>;
  const { configuration, catalog, targets } = snapshot;
  const observations = catalog.filter(item => item.kind === "observation");
  const consequences = catalog.filter(item => item.kind === "consequence");
  const draft = editing?.intent;
  const target = draft ? targets.find(item => sameAutomaticTarget(draft.scope, item)) : undefined;
  const capability = observations.find(item => item.id === draft?.capability);
  const consequence = consequences.find(item => item.id === draft?.consequence.capability);
  const update = (values: Partial<AutomaticIntent>) => { setEditing(current => current && { ...current, intent: { ...current.intent, ...values } }); setReview(null); };
  const begin = (activate = false) => {
    const place = targets[0];
    const first = observations.find(item => item.available && item.scopes?.includes(place?.scope.kind));
    if (!place || !first) return;
    setError(null); setReview(null);
    setEditing({ configuration, activate, intent: {
      id: crypto.randomUUID().replace(/-/g, ""), revision: 1,
      name: `${title(first.title, first.id)} · ${place.name}`, capability: first.id,
      scope: structuredClone(place.scope), requested: true, owner_id: "", condition: {},
      consequence: { capability: "core.record", parameters: {} }, customizations: {}, consents: [],
    } });
  };
  const choosePlace = (place: AutomaticTarget) => {
    const next = capability?.scopes?.includes(place.scope.kind) ? capability : observations.find(item => item.available && item.scopes?.includes(place.scope.kind));
    update({ scope: structuredClone(place.scope), condition: { ...draft!.condition, association: null }, capability: next?.id || draft!.capability });
  };
  const renderFields = (fields: AutomaticField[] = [], values: Record<string, any>, change: (next: Record<string, any>) => void) => fields.map(field => {
    const value = values[field.name] ?? field.default;
    if (field.type === "multiple") return <fieldset key={field.name}><legend>{title(field.title, field.name)}</legend><div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>{field.choices?.map(choice => <label key={choice.value}><input type="checkbox" checked={Array.isArray(value) && value.includes(choice.value)} onChange={event => change({ ...values, [field.name]: event.target.checked ? [...(value || []), choice.value] : (value || []).filter((entry: string) => entry !== choice.value) })} /> {title(choice.title, choice.value)}</label>)}</div></fieldset>;
    return <label className="field" key={field.name}><span>{title(field.title, field.name)}</span><input className="input" type={field.type} min={field.minimum} max={field.maximum} step={field.type === "number" ? "any" : undefined} value={value ?? ""} onChange={event => change({ ...values, [field.name]: field.type === "number" ? event.target.valueAsNumber : event.target.value })} /></label>;
  });
  const renderOperator = (operator: string, config: Record<string, unknown>, change: (value: Record<string, unknown>) => void) => {
    const panel = operatorPanels[operator];
    if (!panel || !draft) return <p role="status">{t("preparationUnavailable")}</p>;
    return panel.render({ i18n, operatorId: operator, stepUid: draft.id, nodeId: draft.id,
      processingServerId: "local", cameraId: draft.scope.camera_ids[0], purpose: "automatic",
      config, showAdvanced: true, updateConfig: patch => change({ ...config, ...patch }), replaceConfig: change });
  };
  const stateText = (state: string) => automaticText(locale, (['active', 'waiting', 'paused', 'blocked', 'unavailable', 'outside_schedule', 'preparing'].includes(state) ? state : 'unavailable') as AutomaticMessage);

  return <section aria-label={t("title")} style={{ display: "grid", gap: 16, minWidth: 0 }}>
    <header><h2>{t("title")}</h2><p>{t("description")}</p></header>
    {error && <div role="alert"><p>{t(errorDuringLoad ? "loadError" : "saveError")}</p><details><summary>{t("details")}</summary>{error}</details><button className="chipButton" onClick={() => setReload(value => value + 1)}>{t("retry")}</button></div>}
    {snapshot.provider_errors?.length > 0 && <p role="status">{t("providers")}</p>}
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      <button className="primaryButton" disabled={busy || Boolean(editing) || Boolean(review) || (!configuration.enabled && !targets.length)} onClick={() => configuration.enabled ? void commit({ ...configuration, enabled: false }) : configuration.intents.length ? void prepare({ ...configuration, enabled: true }) : begin(true)}>{configuration.enabled ? t("pause") : t("enable")}</button>
      <button className="chipButton" disabled={busy || !targets.length || Boolean(editing) || Boolean(review)} onClick={() => begin()}>{t("add")}</button>
      <button className="chipButton" onClick={onOpenFlows}>{t("flows")}</button>
    </div><p>{t("pauseHint")}</p>
    {!configuration.intents.length && !editing && <p>{targets.length ? t("empty") : t("noTargets")}</p>}
    {editing && draft && !review && <form onSubmit={event => { event.preventDefault(); const next = replaceAutomaticIntent(editing.configuration, draft); void prepare({ ...next, enabled: editing.activate || next.enabled }); }} style={{ display: "grid", gap: 16 }}>
      <h3 ref={heading} tabIndex={-1}>{t("edit")}</h3>
      <fieldset disabled={busy} style={{ display: "grid", gap: 12, minWidth: 0 }}><legend>{t("where")}</legend>
        <label className="field"><span>{t("where")}</span><select className="input" required value={target?.id || ""} onChange={event => { const place = targets.find(item => item.id === event.target.value); if (place) choosePlace(place); }}><option value="" disabled>—</option>{targets.map(place => <option key={place.id} value={place.id}>{place.name} · {t(place.scope.kind === "physical_area" ? "area" : place.scope.kind === "image_region" ? "region" : "whole")}</option>)}</select></label>
        <p>{draft.scope.kind === "physical_area" ? t("area") : draft.scope.kind === "image_region" ? t("region") : t("whole")}</p>
        {target && <fieldset><legend>{t("sources")}</legend>{target.sources.map(source => <label key={source.camera_id} style={{ display: "block" }}><input type="checkbox" checked={!draft.scope.excluded_camera_ids.includes(source.camera_id)} onChange={event => update({ scope: { ...draft.scope, excluded_camera_ids: event.target.checked ? draft.scope.excluded_camera_ids.filter(id => id !== source.camera_id) : [...draft.scope.excluded_camera_ids, source.camera_id] } })} /> {source.camera_name} · {source.source_name}</label>)}</fieldset>}
        {draft.scope.kind === "physical_area" && <p>{t("coverage")}</p>}
        {target?.scope_operator && renderOperator(target.scope_operator, draft.scope.region || {}, region => update({ scope: { ...draft.scope, region } }))}
        {capability?.configuration_panels?.filter(panel => (target?.sources.filter(source => !draft.scope.excluded_camera_ids.includes(source.camera_id)).length || 0) >= panel.minimum_sources).map(panel => <details key={panel.operator} open><summary>{t("prepareSources")}</summary>{renderOperator(panel.operator, draft.condition[panel.field] || target?.preparations?.[panel.operator] || {}, value => update({ condition: { ...draft.condition, [panel.field]: value } }))}</details>)}
      </fieldset>
      <fieldset disabled={busy} style={{ display: "grid", gap: 12, minWidth: 0 }}><legend>{t("what")}</legend>
        <label className="field"><span>{t("what")}</span><select className="input" value={draft.capability} onChange={event => update({ capability: event.target.value, condition: { ...draft.condition, resource_parameters: {} }, customizations: {}, consents: [] })}>{observations.filter(item => item.id === draft.capability || item.scopes?.includes(draft.scope.kind)).map(item => <option key={item.id} value={item.id} disabled={!item.available}>{title(item.title, item.id)}{!item.available ? ` · ${t("unavailable")}` : ""}</option>)}</select></label>
        {renderFields(capability?.fields, draft.condition, condition => update({ condition }))}
        {Object.keys(draft.customizations).length > 0 && <p>{t("customizations")}: {Object.keys(draft.customizations).join(", ")}</p>}
      </fieldset>
      <fieldset disabled={busy} style={{ display: "grid", gap: 12, minWidth: 0 }}><legend>{t("when")}</legend>
        <label className="field"><span>{t("when")}</span><select className="input" value={draft.condition.schedule ? "schedule" : "always"} onChange={event => update({ condition: { ...draft.condition, schedule: event.target.value === "always" ? null : { enabled: true, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, weekdays: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], start_time: "08:00", end_time: "18:00" } } })}><option value="always">{t("always")}</option><option value="schedule">{t("schedule")}</option></select></label>
        {draft.condition.schedule && <><label className="field"><span>{t("timezone")}</span><input required className="input" value={draft.condition.schedule.timezone} onChange={event => update({ condition: { ...draft.condition, schedule: { ...draft.condition.schedule, timezone: event.target.value } } })} /></label><div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>{(["start_time", "end_time"] as const).map((key, index) => <label className="field" key={key}><span>{t(index ? "end" : "start")}</span><input required className="input" type="time" value={draft.condition.schedule[key]} onChange={event => update({ condition: { ...draft.condition, schedule: { ...draft.condition.schedule, [key]: event.target.value } } })} /></label>)}</div><fieldset><legend>{t("weekdays")}</legend>{(["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const).map(day => <label key={day} style={{ marginRight: 12 }}><input type="checkbox" checked={draft.condition.schedule.weekdays.includes(day)} onChange={event => update({ condition: { ...draft.condition, schedule: { ...draft.condition.schedule, weekdays: event.target.checked ? [...draft.condition.schedule.weekdays, day] : draft.condition.schedule.weekdays.filter((value: string) => value !== day) } } })} /> {t(day)}</label>)}</fieldset></>}
      </fieldset>
      <fieldset disabled={busy} style={{ display: "grid", gap: 12, minWidth: 0 }}><legend>{t("action")}</legend>
        <label className="field"><span>{t("action")}</span><select className="input" value={draft.consequence.capability} onChange={event => update({ consequence: { capability: event.target.value, parameters: {} }, consents: [] })}>{consequences.map(item => <option key={item.id} value={item.id} disabled={!item.available}>{title(item.title, item.id)}</option>)}</select></label>
        {renderFields(consequence?.fields, draft.consequence.parameters, parameters => update({ consequence: { ...draft.consequence, parameters } }))}
        {[...new Set([...(capability?.required_consents || []), ...(consequence?.required_consents || [])])].map(consent => <label key={consent}><input type="checkbox" required checked={draft.consents.includes(consent)} onChange={event => update({ consents: event.target.checked ? [...draft.consents, consent] : draft.consents.filter(value => value !== consent) })} /> {t(consent as AutomaticMessage)}</label>)}
      </fieldset>
      <label className="field"><span>{t("name")}</span><input className="input" required maxLength={160} value={draft.name} onChange={event => update({ name: event.target.value })} /></label>
      <div><button className="primaryButton" type="submit" disabled={busy}>{busy ? t("busy") : t("review")}</button> <button className="chipButton" type="button" disabled={busy} onClick={() => setEditing(null)}>{t("cancel")}</button></div>
    </form>}
    {review && <section aria-label={t("review")} className="card"><h3 ref={heading} tabIndex={-1}>{t("review")}</h3>{review.configuration.intents.map(intent => { const activity = review.preview.activities[intent.id]; return <div key={intent.id}><strong>{intent.name}</strong><p>{stateText(activity?.state || "unavailable")}</p><p>{activity?.sources.map(source => `${source.camera_name} · ${source.source_name}`).join(", ")}</p>{activity?.reason && <><p>{t("unavailableHint")}</p><details><summary>{t("details")}</summary><code>{activity.reason}</code></details></>}</div>; })}<button className="primaryButton" disabled={busy} onClick={() => void commit(review.configuration)}>{t("confirm")}</button> <button className="chipButton" disabled={busy} onClick={() => setReview(null)}>{editing ? t("back") : t("cancel")}</button></section>}
    {!editing && !review && configuration.intents.map(intent => { const activity = snapshot.activities[intent.id]; return <article className="card" key={intent.id}><h3>{intent.name}</h3><p>{t("requested")}: {intent.requested ? t("enabled") : t("disabled")} · {t("effective")}: {stateText(activity?.state || "unavailable")}</p><p>{activity?.sources.map(source => source.camera_name).join(", ")}</p>{activity?.reason && <details><summary>{t("details")}</summary><code>{activity.reason}</code></details>}<button className="chipButton" disabled={busy} onClick={() => setEditing({ intent: structuredClone(intent), configuration, activate: false })}>{t("edit")}</button> <button className="chipButton" disabled={busy} onClick={() => void commit(replaceAutomaticIntent(configuration, { ...intent, requested: !intent.requested }))}>{intent.requested ? t("pauseActivity") : t("resumeActivity")}</button></article>; })}
    <details><summary>{t("privacy")}</summary><p>{t("blockHint")}</p>{targets.filter(item => item.scope.kind === "camera").map(place => { const id = place.scope.camera_ids[0]; const blocked = configuration.blocked_camera_ids.includes(id); return <p key={place.id}>{place.name} <button className="chipButton" disabled={busy || Boolean(editing) || Boolean(review)} onClick={() => void commit({ ...configuration, blocked_camera_ids: blocked ? configuration.blocked_camera_ids.filter(value => value !== id) : [...configuration.blocked_camera_ids, id] })}>{blocked ? t("unblock") : t("block")}</button></p>; })}</details>
  </section>;
}
