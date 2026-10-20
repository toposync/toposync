import { ManualAssociationResult } from "./ManualAssociationResult";
import React, { useEffect, useRef, useState } from "react";
import { requestJson, type HostI18n } from "@toposync/plugin-api";

type Source = { camera_id: string; source_id: string; identity_policy: string; clock_domain: string; clock_uncertainty_seconds: number | null; coordinate_reference: string; position_error_metres: number | null };
type Transition = { source_camera: string; target_camera: string; mode: string; max_seconds: number; residual_metres: number };
type Review = { revision: number; editable: boolean; restart_gap: boolean; observations: Record<string, { entity: string; reason: string; gap: boolean; observation: { camera: string; track: string; observed_at: number } }>; entities: Record<string, { recognition_status: string; identities: Record<string, { name?: string; evidence_camera: string }> }>; groups: { id: string; members: string[]; start: number; end: number | null }[] };
const words = {
  historical: ["Historical entity", "Entidade histórica"],
  recognized: ["Recognized", "Reconhecida"], unknown: ["Unknown", "Desconhecida"], pending: ["Pending", "Pendente"], unavailable: ["Unavailable", "Indisponível"], ambiguous: ["Ambiguous", "Ambígua"], suggested: ["Suggested", "Sugerida"], insufficient_quality: ["Insufficient quality", "Qualidade insuficiente"], insufficient_evidence: ["Insufficient evidence", "Evidência insuficiente"], local_track: ["Local trajectory", "Trajetória local"], source_or_geometry_changed: ["Source or geometry changed", "Fonte ou geometria alterada"], ambiguous_competitors: ["Competing candidates", "Candidatos concorrentes"], human_correction: ["Human correction", "Correção humana"], gap: ["Observation gap", "Lacuna de observação"],
  title: ["Multicamera continuity", "Continuidade multicâmera"], scope: ["Association scope", "Escopo da associação"],
  help: ["Declare clock and mapping uncertainty only from verified evidence. Missing values keep cross-camera matches uncertain. Scores are geometric residuals, not probabilities.", "Declare incertezas de relógio e mapeamento somente com evidência verificada. Valores ausentes mantêm associações entre câmeras incertas. Pontuações são resíduos geométricos, não probabilidades."],
  sources: ["Sources", "Fontes"], camera: ["Camera identifier", "Identificador da câmera"], source: ["Source identifier", "Identificador da fonte"],
  policy: ["Identity policy", "Política de identidade"], no_identification: ["No identification", "Sem identificação"], anonymous_continuity: ["Anonymous continuity only", "Somente continuidade anônima"], keep_confirmed_identity: ["Keep confirmed identity", "Manter identidade confirmada"], recognize_here: ["Recognize here", "Reconhecer aqui"],
  clock: ["Verified clock domain", "Domínio de relógio verificado"], uncertainty: ["Clock uncertainty (seconds)", "Incerteza do relógio (segundos)"], coordinates: ["Coordinate reference", "Referência de coordenadas"], error: ["Position error bound (metres)", "Limite de erro de posição (metros)"],
  add: ["Add", "Adicionar"], remove: ["Remove", "Remover"], transitions: ["Prepared transitions", "Transições preparadas"], from: ["From camera", "Da câmera"], to: ["To camera", "Para a câmera"], mode: ["Mode", "Modo"], transition: ["Transition", "Transição"], overlap: ["Overlap", "Sobreposição"], duration: ["Association window (seconds)", "Janela de associação (segundos)"], residual: ["Maximum geometric residual (metres)", "Resíduo geométrico máximo (metros)"], horizon: ["Anonymous retention (seconds)", "Retenção anônima (segundos)"], group: ["Group distance (metres, optional)", "Distância de grupo (metros, opcional)"],
  refresh: ["Review saved observations", "Revisar observações salvas"], failed: ["Review unavailable, changed or access denied. Refresh before retrying.", "Revisão indisponível, alterada ou sem acesso. Atualize antes de tentar novamente."], restart: ["A restart gap is recorded; continuity was not observed during downtime.", "Há uma lacuna de reinício; a continuidade não foi observada durante a parada."],
  observation: ["Observation", "Observação"], entity: ["Entity", "Entidade"], status: ["Status", "Estado"], reason: ["Reason", "Motivo"], target: ["Correct association", "Corrigir associação"], separate: ["Separate into a new entity", "Separar em nova entidade"], apply: ["Apply human correction", "Aplicar correção humana"], empty: ["No observations in the retention window. Save and activate this activity or flow first.", "Sem observações na janela de retenção. Salve e ative esta atividade ou fluxo primeiro."], episodes: ["Group episodes", "Episódios de grupo"], correctionHelp: ["The correction applies to the selected local trajectory and takes precedence over inference. It does not train the gallery.", "A correção vale para a trajetória local selecionada e prevalece sobre a inferência. Ela não treina a galeria."],
} satisfies Record<string, [string, string]>;
export const associationTranslations = Object.fromEntries(["en", "pt-BR"].map((locale, index) => [locale, Object.fromEntries(Object.entries(words).map(([key, values]) => [`ext.vision.association.${key}`, values[index]]))]));

export function AssociationSummary({ i18n, config }: { i18n: HostI18n; config: Record<string, unknown> }) {
  const { t } = i18n.useI18n();
  const text = (key: string) => t(`ext.vision.association.${key}`);
  return <div><p>{text("horizon")}: {String(config.horizon_seconds ?? 120)}</p>{((config.sources || []) as Source[]).map(source => <p key={`${source.camera_id}/${source.source_id}`}>{source.camera_id} · {source.source_id}: {text(source.identity_policy)}</p>)}</div>;
}

export function Association({ i18n, config, updateConfig, configurationOnly = false, recognitionAllowed = true }: { configurationOnly?: boolean; recognitionAllowed?: boolean; i18n: HostI18n; config: Record<string, unknown>; updateConfig: (patch: Record<string, unknown>) => void }) {
  const { t: translate } = i18n.useI18n(); const t = (key: keyof typeof words) => translate(`ext.vision.association.${key}`);
  const sources = (config.sources ?? []) as Source[]; const transitions = (config.transitions ?? []) as Transition[];
  const scope = String(config.scope ?? ""); const [review, setReview] = useState<Review | null>(null); const [failure, setFailure] = useState(false); const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState(""); const [target, setTarget] = useState(""); const request = useRef<AbortController | null>(null);
  useEffect(() => { request.current?.abort(); setReview(null); setSelected(""); setTarget(""); setBusy(false); return () => request.current?.abort(); }, [scope, JSON.stringify(config)]);
  async function load(correction = false) {
    request.current?.abort(); const controller = new AbortController(); request.current = controller; setBusy(true); setFailure(false);
    try {
      const base = `/api/vision/associations/${encodeURIComponent(scope)}`;
      if (correction && review) await requestJson(`${base}/corrections`, { method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal, body: JSON.stringify({ observation_id: selected, entity_id: target || null, expected_revision: review.revision }) });
      const value = await requestJson<Review>(base, { signal: controller.signal });
      if (!controller.signal.aborted) { setReview(value); setSelected(""); setTarget(""); }
    } catch { if (!controller.signal.aborted) { setFailure(true); setReview(null); } }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const translated = (value: string) => value in words ? t(value as keyof typeof words) : value;
  const entityLabel = (id: string) => {
    const entity = review?.entities[id]; const names = Object.values(entity?.identities ?? {}).map(identity => identity.name).filter(Boolean);
    const index = Object.keys(review?.entities ?? {}).indexOf(id);
    return index < 0 ? `${t("historical")} ${id.slice(0, 8)}` : `${t("entity")} ${index + 1}${names.length ? ` · ${names.join(", ")}` : ""}`;
  };
  const sourceChange = (index: number, patch: Partial<Source>) => updateConfig({ sources: sources.map((source, at) => at === index ? { ...source, ...patch } : source) });
  return <div className="formGrid">
    <h3>{t("title")}</h3>{!configurationOnly && <ManualAssociationResult key={scope} i18n={i18n} />}<p>{t("help")}</p>
    {!configurationOnly && <label>{t("scope")}<input value={scope} onChange={event => updateConfig({ scope: event.target.value })} pattern="(?:[a-zA-Z0-9_]|-)+" /></label>}
    <label>{t("horizon")}<input type="number" min="1" max="3600" value={Number(config.horizon_seconds ?? 120)} onChange={event => updateConfig({ horizon_seconds: event.target.valueAsNumber })} /></label>
    <label>{t("group")}<input type="number" min="0.01" max="20" step="any" value={config.group_distance_metres == null ? "" : Number(config.group_distance_metres)} onChange={event => updateConfig({ group_distance_metres: event.target.value === "" ? null : event.target.valueAsNumber })} /></label>
    <h4>{t("sources")}</h4>
    {sources.map((source, index) => <fieldset key={index}><legend>{t("source")} {index + 1}</legend>
      {([['camera_id','camera'],['source_id','source'],['clock_domain','clock'],['coordinate_reference','coordinates']] as const).map(([key,label]) => <label key={key}>{t(label)}<input readOnly={configurationOnly && key !== 'clock_domain'} value={source[key]} onChange={event => sourceChange(index, { [key]: event.target.value })} /></label>)}
      <label>{t("policy")}<select value={source.identity_policy} onChange={event => sourceChange(index, { identity_policy: event.target.value })}>{(["no_identification", "anonymous_continuity", "keep_confirmed_identity", "recognize_here"] as const).map(value => <option key={value} value={value} disabled={!recognitionAllowed && ["keep_confirmed_identity", "recognize_here"].includes(value)}>{t(value)}</option>)}</select></label>
      {([['clock_uncertainty_seconds','uncertainty'],['position_error_metres','error']] as const).map(([key,label]) => <label key={key}>{t(label)}<input type="number" min="0" step="0.01" value={source[key] ?? ""} onChange={event => sourceChange(index, { [key]: event.target.value === "" ? null : event.target.valueAsNumber })} /></label>)}
      {!configurationOnly && <button type="button" onClick={() => updateConfig({ sources: sources.filter((_, at) => at !== index) })}>{t("remove")}</button>}
    </fieldset>)}
    {!configurationOnly && <button type="button" disabled={sources.length >= 16} onClick={() => updateConfig({ sources: [...sources, { camera_id: "", source_id: "", identity_policy: "no_identification", clock_domain: "", clock_uncertainty_seconds: null, coordinate_reference: "", position_error_metres: null }] })}>{t("add")} · {t("source")}</button>}
    <h4>{t("transitions")}</h4>
    {transitions.map((transition, index) => { const change = (patch: Partial<Transition>) => updateConfig({ transitions: transitions.map((item, at) => at === index ? { ...item, ...patch } : item) }); return <fieldset key={index}><legend>{t("transition")} {index + 1}</legend>
      {([['source_camera','from'],['target_camera','to']] as const).map(([key,label]) => <label key={key}>{t(label)}<select value={transition[key]} onChange={event => change({ [key]: event.target.value })}><option value="">—</option>{[...new Set(sources.map(source => source.camera_id))].filter(Boolean).map(camera => <option key={camera} value={camera}>{camera}</option>)}</select></label>)}
      <label>{t("mode")}<select value={transition.mode} onChange={event => change({ mode: event.target.value })}><option value="transition">{t("transition")}</option><option value="overlap">{t("overlap")}</option></select></label>
      {([['max_seconds','duration'],['residual_metres','residual']] as const).map(([key,label]) => <label key={key}>{t(label)}<input type="number" min="0.01" step="any" value={transition[key]} onChange={event => change({ [key]: event.target.valueAsNumber })} /></label>)}
      <button type="button" onClick={() => updateConfig({ transitions: transitions.filter((_, at) => at !== index) })}>{t("remove")}</button>
    </fieldset>; })}
    <button type="button" disabled={transitions.length >= 128} onClick={() => updateConfig({ transitions: [...transitions, { source_camera: "", target_camera: "", mode: "transition", max_seconds: 3, residual_metres: 0.5 }] })}>{t("add")} · {t("transition")}</button>
    <button type="button" disabled={!scope || busy} onClick={() => void load()}>{t("refresh")}</button>
    {failure && <p role="alert">{t("failed")}</p>}
    {review && <><p>{t("status")}: {review.revision}</p>{review.restart_gap && <p>{t("restart")}</p>}
      {!Object.keys(review.observations).length ? <p>{t("empty")}</p> : <><div style={{ overflowX: "auto" }}><table><thead><tr><th>{t("observation")}</th><th>{t("entity")}</th><th>{t("status")}</th><th>{t("reason")}</th></tr></thead><tbody>{Object.entries(review.observations).map(([id, record]) => <tr key={id}><td>{record.observation.camera} · {record.observation.track} · {record.observation.observed_at}</td><td>{entityLabel(record.entity)}</td><td>{translated(review.entities[record.entity]?.recognition_status ?? "unknown")}</td><td>{translated(record.reason)}{record.gap ? ` · ${t("gap")}` : ""}</td></tr>)}</tbody></table></div>
      {review.editable && <><p>{t("correctionHelp")}</p><label>{t("observation")}<select value={selected} onChange={event => setSelected(event.target.value)}><option value="">—</option>{Object.entries(review.observations).map(([id, record]) => <option key={id} value={id}>{record.observation.camera} · {record.observation.track} · {record.observation.observed_at}</option>)}</select></label><label>{t("target")}<select value={target} onChange={event => setTarget(event.target.value)}><option value="">{t("separate")}</option>{Object.keys(review.entities).map(id => <option key={id} value={id}>{entityLabel(id)}</option>)}</select></label><button type="button" disabled={busy || !selected} onClick={() => void load(true)}>{t("apply")}</button></>}
      <h4>{t("episodes")}</h4><ul>{review.groups.map(group => <li key={group.id}>{group.members.map(entityLabel).join(", ")} · {group.start} — {group.end ?? "…"}</li>)}</ul></>}
    </>}
  </div>;
}
