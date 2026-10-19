import React, { useEffect, useRef, useState } from "react";
import { requestJson, type HostI18n } from "@toposync/plugin-api";

type Choice = { species: string; embedding_space: string; model_name: string };
type Policy = Record<string, unknown> & { species?: string; embedding_space?: string; calibration_revision?: string; automatic_enabled?: boolean };
const numericFields = [
  ["acceptance_similarity", -1, 1, "Acceptance similarity", "Similaridade para aceitar"],
  ["suggestion_similarity", -1, 1, "Suggestion similarity", "Similaridade para sugerir"],
  ["competitor_margin", 0, 2, "Margin over other identities", "Margem sobre outras identidades"],
  ["clustering_similarity", -1, 1, "Clustering similarity", "Similaridade para agrupar"],
  ["continuity_seconds", 0, 60, "Identity continuity (seconds)", "Continuidade da identidade (segundos)"],
] as const;

export function recognitionPoliciesValid(policies: Policy[]): boolean {
  return policies.length <= 16 && policies.every(policy => Boolean(policy.species && policy.embedding_space && policy.calibration_revision?.trim())
    && numericFields.every(([key, minimum, maximum]) => typeof policy[key] === "number" && Number.isFinite(policy[key]) && (policy[key] as number) >= minimum && (policy[key] as number) <= maximum)
    && Number(policy.suggestion_similarity) <= Number(policy.acceptance_similarity))
    && new Set(policies.map(policy => `${policy.species}/${policy.embedding_space}`)).size === policies.length;
}

export function RecognitionPolicySummary({ i18n, config }: { i18n: HostI18n; config: Record<string, unknown> }) {
  const { locale } = i18n.useI18n();
  const pt = locale === "pt-BR";
  const policies = (Array.isArray(config.policies) ? config.policies : []) as Policy[];
  return <div>{!policies.length ? <p>{pt ? "Sem política calibrada: identidade não confirmada." : "No calibrated policy: identity unconfirmed."}</p> : policies.map((policy, index) => <details key={index}>
    <summary>{i18n.t(`ext.vision.identity.${policy.species}`)} · {policy.calibration_revision} · {policy.automatic_enabled ? (pt ? "Confirmação automática" : "Automatic confirmation") : (pt ? "Somente sugestões" : "Suggestions only")}</summary>
    <p style={{ overflowWrap: "anywhere" }}>{pt ? "Identidade do modelo" : "Model identity"}: {policy.embedding_space}</p>
    <dl>{numericFields.map(([key, , , en, br]) => <React.Fragment key={key}><dt>{pt ? br : en}</dt><dd>{String(policy[key] ?? "—")}</dd></React.Fragment>)}</dl>
  </details>)}</div>;
}

export function RecognitionPolicyPanel({ i18n, config, updateConfig, onValidityChange }: {
  i18n: HostI18n; config: Record<string, unknown>; updateConfig: (patch: Record<string, unknown>) => void;
  onValidityChange?: (valid: boolean) => void;
}) {
  const { locale } = i18n.useI18n();
  const pt = locale === "pt-BR";
  const text = (en: string, br: string) => pt ? br : en;
  const policies = (Array.isArray(config.policies) ? config.policies : []) as Policy[];
  const [choices, setChoices] = useState<Choice[]>([]);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const valid = recognitionPoliciesValid(policies);
  const validityCallback = useRef(onValidityChange);
  validityCallback.current = onValidityChange;
  useEffect(() => { validityCallback.current?.(valid); }, [valid]);
  useEffect(() => () => validityCallback.current?.(true), []);
  useEffect(() => {
    const controller = new AbortController(); setError(false);
    void requestJson<{ choices: Choice[] }>("/api/vision/identities/recognition-options", { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setChoices(result.choices); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [reload]);
  const change = (index: number, policy: Policy) => updateConfig({ policies: policies.map((item, current) => current === index ? policy : item) });
  const available = choices.filter(choice => !policies.some(policy => policy.species === choice.species && policy.embedding_space === choice.embedding_space));
  return <div className="formGrid">
    <p>{text("These values describe your calibration protocol, not probabilities. Changing a model requires a new calibration. No model is installed or run here.", "Estes valores descrevem seu protocolo de calibração, não probabilidades. Trocar o modelo exige nova calibração. Nenhum modelo é instalado ou executado aqui.")}</p>
    {error && <p role="alert">{text("Could not load model options.", "Não foi possível carregar as opções de modelo.")} <button type="button" className="chipButton" onClick={() => setReload(value => value + 1)}>{text("Try again", "Tentar novamente")}</button></p>}
    {!policies.length && <RecognitionPolicySummary i18n={i18n} config={config} />}
    {policies.map((policy, index) => <fieldset key={index} style={{ minWidth: 0 }}>
      <legend>{text("Recognition calibration", "Calibração de reconhecimento")} {index + 1}</legend>
      <label className="field"><span>{text("Species and model", "Espécie e modelo")}</span><select className="input" value={`${policy.species}/${policy.embedding_space}`} onChange={event => {
        const choice = choices.find(item => `${item.species}/${item.embedding_space}` === event.target.value);
        if (choice) change(index, { species: choice.species, embedding_space: choice.embedding_space, calibration_revision: "", automatic_enabled: false });
      }}>
        {!choices.some(choice => choice.species === policy.species && choice.embedding_space === policy.embedding_space) && <option value={`${policy.species}/${policy.embedding_space}`}>{policy.species} · {text("Saved model", "Modelo salvo")}</option>}
        {choices.map(choice => <option key={`${choice.species}/${choice.embedding_space}`} value={`${choice.species}/${choice.embedding_space}`} disabled={policies.some((other, current) => current !== index && other.species === choice.species && other.embedding_space === choice.embedding_space)}>{i18n.t(`ext.vision.identity.${choice.species}`)} · {choice.model_name}</option>)}
      </select></label>
      <label className="field"><span>{text("Calibration reference / revision", "Referência / revisão da calibração")}</span><input className="input" required maxLength={512} value={policy.calibration_revision || ""} onChange={event => change(index, { ...policy, calibration_revision: event.target.value })} /></label>
      {numericFields.map(([key, minimum, maximum, en, br]) => <label className="field" key={key}><span>{pt ? br : en}</span><input className="input" type="number" required min={minimum} max={maximum} step="any" value={typeof policy[key] === "number" && Number.isFinite(policy[key]) ? policy[key] as number : ""} onChange={event => change(index, { ...policy, [key]: event.target.value === "" ? undefined : event.target.valueAsNumber })} /></label>)}
      <label><input type="checkbox" checked={policy.automatic_enabled === true} onChange={event => change(index, { ...policy, automatic_enabled: event.target.checked })} /> {text("Allow automatic confirmation using this calibration", "Permitir confirmação automática com esta calibração")}</label>
      <button type="button" className="chipButton" onClick={() => updateConfig({ policies: policies.filter((_, current) => current !== index) })}>{text("Remove policy", "Remover política")}</button>
    </fieldset>)}
    {!valid && <p role="alert">{text("Complete each calibration. The suggestion threshold cannot exceed acceptance; each species/model pair must be unique.", "Complete cada calibração. O limite de sugestão não pode superar o de aceitação; cada par de espécie/modelo deve ser único.")}</p>}
    <button type="button" className="chipButton" disabled={!available.length || policies.length >= 16} onClick={() => { const choice = available[0]; if (choice) updateConfig({ policies: [...policies, { species: choice.species, embedding_space: choice.embedding_space, calibration_revision: "", automatic_enabled: false }] }); }}>{text("Add calibrated policy", "Adicionar política calibrada")}</button>
  </div>;
}
