import type { PipelineOperatorPanel } from "@toposync/plugin-api";
import React, { useEffect, useRef, useState } from "react";
import { fetchAutomatic, previewAutomatic, saveAutomatic, useAutomaticInFlow } from "../../util/api";
import { i18n } from "../../util/i18n";
import { automaticDraft, replaceAutomaticIntent, replaceAutomaticResources, sameAutomaticTarget } from "../../util/automatic";
import type { AutomaticConfiguration, AutomaticField, AutomaticIntent, AutomaticPreview, AutomaticSnapshot, AutomaticTarget } from "../../util/automatic";
import { automaticText, AutomaticMessage } from "./messages";

export function AutomaticScreen({ onOpenFlows, onDirtyChange, operatorPanels }: { onOpenFlows: () => void; onDirtyChange: (dirty: boolean) => void; operatorPanels: Record<string, PipelineOperatorPanel> }) {
  const { locale } = i18n.useI18n();
  const t = (key: AutomaticMessage) => automaticText(locale, key);
  const title = (value: Record<string, string> | undefined, fallback: string) => value?.[locale] || value?.en || fallback;
  const [snapshot, setSnapshot] = useState<AutomaticSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [invalidPanels, setInvalidPanels] = useState<string[]>([]);
  const [flowReview, setFlowReview] = useState<{ id: string; revision: string } | null>(null);
  const [errorDuringLoad, setErrorDuringLoad] = useState(false);
  const [reload, setReload] = useState(0);
  const [editing, setEditing] = useState<{ intent: AutomaticIntent; configuration: AutomaticConfiguration; activate: boolean } | null>(null);
  const [review, setReview] = useState<{ configuration: AutomaticConfiguration; preview: AutomaticPreview } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const shortcutHandled = useRef(false);
  const shortcutTarget = useRef(new URLSearchParams(window.location.search).get("automatic_target"));
  const dirty = Boolean(editing || review);
  useEffect(() => { setInvalidPanels([]); }, [editing?.intent.id]);
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
        if (shortcutTarget.current && !shortcutHandled.current) {
          shortcutHandled.current = true;
          const location = new URL(window.location.href);
          location.searchParams.delete("automatic_target");
          window.history.replaceState(window.history.state, "", location.pathname + location.search + location.hash);
          const intent = automaticDraft(next, locale, shortcutTarget.current);
          if (intent) setEditing({ intent, configuration: next.configuration, activate: false });
          else setError(t("shortcutUnavailable"));
        }
        timer = setTimeout(load, 3000);
      } catch (reason) {
        if (!abort.signal.aborted) { setErrorDuringLoad(true); setError(reason instanceof Error ? reason.message : t("loadError")); }
      }
    };
    void load();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [reload]);
  useEffect(() => { if (editing || review) heading.current?.focus(); }, [Boolean(editing), Boolean(review)]);

  const commit = async (configuration: AutomaticConfiguration, reviewedPreparations?: Record<string, string>) => {
    setBusy(true); setError(null); setErrorDuringLoad(false);
    try {
      const saved = await saveAutomatic(configuration, configuration.revision, reviewedPreparations);
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
  const useInFlow = async (intentId: string, productionRevision?: string) => {
    if (!snapshot) return;
    setBusy(true); setError(null); setErrorDuringLoad(false);
    try { await useAutomaticInFlow(intentId, snapshot.configuration.revision, productionRevision); setFlowReview(null); setReload(value => value + 1); }
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
    const intent = automaticDraft(snapshot, locale);
    if (!intent) return;
    setError(null); setReview(null);
    setEditing({ configuration, activate, intent });
  };
  const choosePlace = (place: AutomaticTarget) => {
    const next = capability?.scopes?.includes(place.scope.kind) ? capability : observations.find(item => item.available && item.scopes?.includes(place.scope.kind));
    update({ scope: structuredClone(place.scope), condition: { ...draft!.condition, association: null }, capability: next?.id || draft!.capability });
  };
  const renderFields = (fields: AutomaticField[] = [], values: Record<string, any>, change: (next: Record<string, any>) => void) => fields.map(field => {
    const value = values[field.name] ?? field.default;
    if (field.type === "select") return <label className="field" key={field.name}><span>{title(field.title, field.name)}</span><select className="input" required={field.required} value={value ?? ""} onChange={event => change({ ...values, [field.name]: event.target.value })}><option value="" disabled>—</option>{value && !field.choices?.some(choice => choice.value === value) && <option value={value} disabled>{String(value)} · {t("unavailable")}</option>}{field.choices?.map(choice => <option key={choice.value} value={choice.value}>{title(choice.title, choice.value)}</option>)}</select></label>;
    if (field.type === "multiple") return <fieldset className="formSection" key={field.name}><legend>{title(field.title, field.name)}</legend><div className="formChoices">{field.choices?.map(choice => <label className="formChoice" key={choice.value}><input type="checkbox" checked={Array.isArray(value) && value.includes(choice.value)} onChange={event => change({ ...values, [field.name]: event.target.checked ? [...(value || []), choice.value] : (value || []).filter((entry: string) => entry !== choice.value) })} /> {title(choice.title, choice.value)}</label>)}</div></fieldset>;
    return <label className="field" key={field.name}><span>{title(field.title, field.name)}</span><input className="input" type={field.type} required={field.required} min={field.minimum} max={field.maximum} step={field.type === "number" ? field.step ?? "any" : undefined} value={value ?? ""} onChange={event => change({ ...values, [field.name]: field.type === "number" ? event.target.valueAsNumber : event.target.value })} /></label>;
  });
  const renderOperator = (operator: string, config: Record<string, unknown>, change: (value: Record<string, unknown>) => void) => {
    const panel = operatorPanels[operator];
    if (!panel || !draft) return <p role="status">{t("preparationUnavailable")}</p>;
    return panel.render({ i18n, operatorId: operator, stepUid: draft.id, nodeId: draft.id,
      processingServerId: "local", cameraId: draft.scope.camera_ids[0], purpose: "automatic",
      observationCapability: draft.capability,
      sources: target?.sources.filter(source => !draft.scope.excluded_camera_ids.includes(source.camera_id)),
      observationCategories: draft.condition.categories || capability?.fields?.find(field => field.name === "categories")?.default || ["person"],
      onValidityChange: valid => setInvalidPanels(previous => previous.includes(operator) === !valid ? previous : valid ? previous.filter(item => item !== operator) : [...previous, operator]),
      config, showAdvanced: true, updateConfig: patch => change({ ...config, ...patch }), replaceConfig: change });
  };
  const observationSummary = (intent: AutomaticIntent) => {
    const observation = observations.find(item => item.id === intent.capability);
    const condition = { ...configuration.defaults[intent.capability], ...intent.condition, ...intent.customizations };
    return <><dl>{[
      ...(observation?.fields || []).map(field => ({ field, values: condition })),
      ...(observation?.resource_fields || []).map(field => ({ field, values: condition.resource_parameters || {} })),
    ].map(({ field, values }) => {
      const value = values[field.name] ?? field.default;
      const label = (entry: unknown) => title(field.choices?.find(choice => choice.value === entry)?.title, String(entry ?? "—"));
      return <React.Fragment key={field.name}><dt>{title(field.title, field.name)}</dt><dd>{Array.isArray(value) ? value.map(label).join(", ") : label(value)}</dd></React.Fragment>;
    })}</dl>{observation?.resource_operator && operatorPanels[observation.resource_operator]?.renderSummary?.(condition.resource_parameters || {})}{observation?.configuration_panels?.map(panel => <React.Fragment key={panel.field}>{operatorPanels[panel.operator]?.renderSummary?.(condition[panel.field] || {})}</React.Fragment>)}</>;
  };
  const stateText = (state: string) => automaticText(locale, (['active', 'waiting', 'paused', 'blocked', 'unavailable', 'outside_schedule', 'preparing'].includes(state) ? state : 'unavailable') as AutomaticMessage);

  return <section aria-label={t("title")} className="automaticScreen formGrid">
    <header><h2>{t("title")}</h2><p>{t("description")}</p></header>
    {error && <div role="alert"><p>{t(errorDuringLoad ? "loadError" : "saveError")}</p><details><summary>{t("details")}</summary>{error}</details><button className="chipButton" onClick={() => setReload(value => value + 1)}>{t("retry")}</button></div>}
    {snapshot.provider_errors?.length > 0 && <p role="status">{t("providers")}</p>}
    <div className="formActions">
      <button className="primaryButton" disabled={busy || Boolean(editing) || Boolean(review) || (!configuration.enabled && !targets.length)} onClick={() => configuration.enabled ? void commit({ ...configuration, enabled: false }) : configuration.intents.length ? void prepare({ ...configuration, enabled: true }) : begin(true)}>{configuration.enabled ? t("pause") : t("enable")}</button>
      <button className="chipButton" disabled={busy || !targets.length || Boolean(editing) || Boolean(review)} onClick={() => begin()}>{t("add")}</button>
      <button className="chipButton" onClick={onOpenFlows}>{t("flows")}</button>
    </div><p>{t("pauseHint")}</p>
    {!configuration.intents.length && !editing && <p>{targets.length ? t("empty") : t("noTargets")}</p>}
    {editing && draft && !review && <form onSubmit={event => { event.preventDefault(); if (busy || invalidPanels.length) return; const next = replaceAutomaticIntent(editing.configuration, draft); void prepare({ ...next, enabled: editing.activate || next.enabled }); }} className="formGrid">
      <h3 ref={heading} tabIndex={-1}>{t("edit")}</h3>
      <fieldset disabled={busy} className="formSection"><legend>{t("where")}</legend>
        <label className="field"><span>{t("where")}</span><select className="input" required value={target?.id || ""} onChange={event => { const place = targets.find(item => item.id === event.target.value); if (place) choosePlace(place); }}><option value="" disabled>—</option>{targets.map(place => <option key={place.id} value={place.id}>{place.name} · {t(place.scope.kind === "physical_area" ? "area" : place.scope.kind === "image_region" ? "region" : "whole")}</option>)}</select></label>
        <p>{draft.scope.kind === "physical_area" ? t("area") : draft.scope.kind === "image_region" ? t("region") : t("whole")}</p>
        {target && <fieldset className="formSection"><legend>{t("sources")}</legend>{target.sources.map(source => <label className="formChoice" key={source.camera_id}><input type="checkbox" checked={!draft.scope.excluded_camera_ids.includes(source.camera_id)} onChange={event => update({ scope: { ...draft.scope, excluded_camera_ids: event.target.checked ? draft.scope.excluded_camera_ids.filter(id => id !== source.camera_id) : [...draft.scope.excluded_camera_ids, source.camera_id] } })} /> {source.camera_name} · {source.source_name}</label>)}</fieldset>}
        {draft.scope.kind === "physical_area" && <p>{t("coverage")}</p>}
        {target?.scope_operator && renderOperator(target.scope_operator, draft.scope.region || {}, region => update({ scope: { ...draft.scope, region } }))}
        {capability?.configuration_panels?.filter(panel => (target?.sources.filter(source => !draft.scope.excluded_camera_ids.includes(source.camera_id)).length || 0) >= panel.minimum_sources || draft.condition[panel.field]).map(panel => <details key={panel.operator} open><summary>{title(panel.title, t("prepareSources"))}</summary>{renderOperator(panel.operator, draft.condition[panel.field] || target?.preparations?.[panel.operator] || {}, value => update({ condition: { ...draft.condition, [panel.field]: value }, consents: panel.field === "association" ? draft.consents.filter(consent => consent !== "recognition") : draft.consents }))}</details>)}
      </fieldset>
      <fieldset disabled={busy} className="formSection"><legend>{t("what")}</legend>
        <label className="field"><span>{t("what")}</span><select className="input" value={draft.capability} onChange={event => update({ capability: event.target.value, condition: { ...draft.condition, resource_parameters: {} }, customizations: {}, consents: [] })}>{!capability && <option value={draft.capability} disabled>{draft.capability} · {t("unavailable")}</option>}{observations.filter(item => item.id === draft.capability || item.scopes?.includes(draft.scope.kind)).map(item => <option key={item.id} value={item.id} disabled={!item.available}>{title(item.title, item.id)}{!item.available ? ` · ${t("unavailable")}` : ""}</option>)}</select></label>
        {renderFields(capability?.fields, draft.condition, condition => update({ condition }))}
        {(!!capability?.resource_fields?.length || capability?.resource_operator) && <details open><summary>{t("refinements")}</summary><p>{title(capability.resource_hint, "")}</p>{capability.resource_operator ? renderOperator(capability.resource_operator, draft.condition.resource_parameters || {}, resource_parameters => update(replaceAutomaticResources(draft, capability, resource_parameters))) : renderFields(capability.resource_fields, draft.condition.resource_parameters || {}, resource_parameters => update(replaceAutomaticResources(draft, capability, resource_parameters)))}</details>}
        {Object.keys(draft.customizations).length > 0 && <p>{t("customizations")}: {Object.keys(draft.customizations).join(", ")}</p>}
      </fieldset>
      <fieldset disabled={busy} className="formSection"><legend>{t("when")}</legend>
        <label className="field"><span>{t("when")}</span><select className="input" value={draft.condition.schedule ? "schedule" : "always"} onChange={event => update({ condition: { ...draft.condition, schedule: event.target.value === "always" ? null : { enabled: true, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, weekdays: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], start_time: "08:00", end_time: "18:00" } } })}><option value="always">{t("always")}</option><option value="schedule">{t("schedule")}</option></select></label>
        {draft.condition.schedule && <><label className="field"><span>{t("timezone")}</span><input required className="input" value={draft.condition.schedule.timezone} onChange={event => update({ condition: { ...draft.condition, schedule: { ...draft.condition.schedule, timezone: event.target.value } } })} /></label><div className="formChoices">{(["start_time", "end_time"] as const).map((key, index) => <label className="field" key={key}><span>{t(index ? "end" : "start")}</span><input required className="input" type="time" value={draft.condition.schedule[key]} onChange={event => update({ condition: { ...draft.condition, schedule: { ...draft.condition.schedule, [key]: event.target.value } } })} /></label>)}</div><fieldset className="formSection"><legend>{t("weekdays")}</legend>{(["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const).map(day => <label className="formChoice" key={day}><input type="checkbox" checked={draft.condition.schedule.weekdays.includes(day)} onChange={event => update({ condition: { ...draft.condition, schedule: { ...draft.condition.schedule, weekdays: event.target.checked ? [...draft.condition.schedule.weekdays, day] : draft.condition.schedule.weekdays.filter((value: string) => value !== day) } } })} /> {t(day)}</label>)}</fieldset></>}
      </fieldset>
      <fieldset disabled={busy} className="formSection"><legend>{t("action")}</legend>
        <label className="field"><span>{t("action")}</span><select className="input" value={draft.consequence.capability} onChange={event => update({ consequence: { capability: event.target.value, parameters: {} }, consents: [] })}>{!consequence && <option value={draft.consequence.capability} disabled>{draft.consequence.capability} · {t("unavailable")}</option>}{consequences.map(item => <option key={item.id} value={item.id} disabled={!item.available}>{title(item.title, item.id)}</option>)}</select></label>
        {renderFields(consequence?.fields, draft.consequence.parameters, parameters => update({ consequence: { ...draft.consequence, parameters } }))}
        {[...new Set([...(capability?.required_consents || []), ...(consequence?.required_consents || [])])].map(consent => <label className="formChoice" key={consent}><input type="checkbox" required checked={draft.consents.includes(consent)} onChange={event => update({ consents: event.target.checked ? [...draft.consents, consent] : draft.consents.filter(value => value !== consent) })} /> {t(consent as AutomaticMessage)}</label>)}
      </fieldset>
      <label className="field"><span>{t("name")}</span><input className="input" required maxLength={160} value={draft.name} onChange={event => update({ name: event.target.value })} /></label>
      <div className="formActions"><button className="primaryButton" type="submit" disabled={busy || invalidPanels.length > 0}>{busy ? t("busy") : t("review")}</button> <button className="chipButton" type="button" disabled={busy} onClick={() => setEditing(null)}>{t("cancel")}</button></div>
    </form>}
    {review && <section aria-label={t("review")} className="card"><h3 ref={heading} tabIndex={-1}>{t("review")}</h3>{review.configuration.intents.map(intent => { const activity = review.preview.activities[intent.id]; return <div key={intent.id}><strong>{intent.name}</strong>{observationSummary(intent)}<p>{stateText(activity?.state === "preparing" && (!intent.requested || !review.configuration.enabled) ? "paused" : activity?.state || "unavailable")}</p><p>{activity?.sources.map(source => `${source.camera_name} · ${source.source_name}`).join(", ")}</p>{activity?.reason && <><p>{t("unavailableHint")}</p><details><summary>{t("details")}</summary><code>{activity.reason}</code></details></>}</div>; })}<button className="primaryButton" disabled={busy} onClick={() => void commit(review.configuration, review.preview.preparations)}>{t("confirm")}</button> <button className="chipButton" disabled={busy} onClick={() => setReview(null)}>{editing ? t("back") : t("cancel")}</button></section>}
    {!editing && !review && configuration.intents.map(intent => { const activity = snapshot.activities[intent.id]; const flow = snapshot.manual_flows?.["manual_" + intent.id]; const observation = observations.find(item => item.id === intent.capability); return <article className="card" key={intent.id}><h3>{intent.name}</h3>{observationSummary(intent)}<p>{t("requested")}: {intent.requested ? t("enabled") : t("disabled")} · {t("effective")}: {stateText(activity?.state || "unavailable")}</p><p>{activity?.sources.map(source => source.camera_name).join(", ")}</p>{activity?.diagnostics_unavailable && <p role="status">{t("diagnosticsUnavailable")}</p>}{observation?.configuration_panels?.map(panel => activity?.diagnostics?.[panel.field] && <React.Fragment key={panel.field}>{operatorPanels[panel.operator]?.renderStatus?.(activity.diagnostics[panel.field])}</React.Fragment>)}{activity?.reason === "source_preparation_changed" && <p role="status">{t("sourcePreparationChanged")}</p>}{activity?.reason && <details><summary>{t("details")}</summary><code>{activity.reason}</code></details>}<button className="chipButton" disabled={busy} onClick={() => setEditing({ intent: structuredClone(intent), configuration, activate: false })}>{t("edit")}</button> <button className="chipButton" disabled={busy} onClick={() => void commit(replaceAutomaticIntent(configuration, { ...intent, requested: !intent.requested }))}>{intent.requested ? t("pauseActivity") : t("resumeActivity")}</button><p>{t("manualFlowHint")}</p>{flow ? <div><p>{t("manualFlow")}: {stateText(flow.state)} <button className="chipButton" onClick={onOpenFlows}>{t("flows")}</button></p>{flow.production_revision && (flowReview?.id === intent.id ? <div role="group" aria-label={t("reviewFlow")}><p>{t("reviewFlowHint")}</p>
      <p>{title(observation?.title, intent.capability)} · {targets.find(item => sameAutomaticTarget(intent.scope, item))?.name}</p>
      {observationSummary(intent)}<button className="chipButton" disabled={busy} onClick={() => void useInFlow(intent.id, flowReview.revision)}>{t("acceptFlow")}</button> <button className="chipButton" disabled={busy} onClick={() => setFlowReview(null)}>{t("cancel")}</button></div> : <button className="chipButton" disabled={busy} onClick={() => setFlowReview({ id: intent.id, revision: flow.production_revision! })}>{t("reviewFlow")}</button>)}{flow.reason && <details><summary>{t("details")}</summary><code>{flow.reason}</code></details>}</div> : <button className="chipButton" disabled={busy || activity?.state === "blocked" || activity?.state === "unavailable"} onClick={() => void useInFlow(intent.id)}>{t("useInFlow")}</button>}</article>; })}
    <details><summary>{t("privacy")}</summary><p>{t("blockHint")}</p>{targets.filter(item => item.scope.kind === "camera").map(place => { const id = place.scope.camera_ids[0]; const blocked = configuration.blocked_camera_ids.includes(id); return <p key={place.id}>{place.name} <button className="chipButton" disabled={busy || Boolean(editing) || Boolean(review)} onClick={() => void commit({ ...configuration, blocked_camera_ids: blocked ? configuration.blocked_camera_ids.filter(value => value !== id) : [...configuration.blocked_camera_ids, id] })}>{blocked ? t("unblock") : t("block")}</button></p>; })}</details>
  </section>;
}
