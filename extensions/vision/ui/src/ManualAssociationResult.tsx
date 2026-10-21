import React, { useEffect, useRef, useState } from "react";
import { requestJson, requestVoid, type HostI18n } from "@toposync/plugin-api";

type Output = { key: string; name?: string; contract: { type: string; version: number; payload_keys: string[]; artifacts: string[] }; production_revision: string };
type Demand = { id: string; received: number; result?: { payload: { association?: { revision: number; reason: string } } } | null };
const labels = {
  title: ["Manual result consumer", "Consumidor manual de resultado"], catalog: ["List available outputs", "Listar saídas disponíveis"], output: ["Named output", "Saída nomeada"], open: ["Use for 30 seconds", "Usar por 30 segundos"], renew: ["Renew for 30 seconds", "Renovar por 30 segundos"], refresh: ["Read latest result", "Ler resultado recente"], close: ["Release consumer", "Liberar consumidor"], waiting: ["No recent result. The trajectory review keeps retained observations.", "Sem resultado recente. A revisão de trajetória mantém as observações retidas."], failed: ["Output unavailable or access revoked. Refresh the catalog before retrying.", "Saída indisponível ou acesso revogado. Atualize o catálogo antes de tentar novamente."], received: ["Results received", "Resultados recebidos"], help: ["This temporary consumer uses the shared producer. Closing it releases only this use; other consumers may keep the producer running.", "Este consumidor temporário usa o produtor compartilhado. Fechá-lo libera somente este uso; outros consumidores podem manter o produtor em execução."],
} satisfies Record<string, [string, string]>;
export const manualResultTranslations = Object.fromEntries(["en", "pt-BR"].map((locale, index) => [locale, Object.fromEntries(Object.entries(labels).map(([key, values]) => [`ext.vision.manualResult.${key}`, values[index]]))]));

export function ManualAssociationResult({ i18n }: { i18n: HostI18n }) {
  const { t: translate } = i18n.useI18n(); const t = (key: keyof typeof labels) => translate(`ext.vision.manualResult.${key}`);
  const [outputs, setOutputs] = useState<Output[]>([]); const [selected, setSelected] = useState(""); const [demand, setDemand] = useState<Demand | null>(null); const [busy, setBusy] = useState(false); const [failed, setFailed] = useState(false);
  const active = useRef<string | null>(null); const lifetime = useRef<AbortController | null>(null); const expiry = useRef<ReturnType<typeof setTimeout> | null>(null);
  function expire() { if (expiry.current) clearTimeout(expiry.current); expiry.current = setTimeout(() => { active.current = null; setDemand(null); }, 30000); }
  useEffect(() => { lifetime.current = new AbortController(); return () => { lifetime.current?.abort(); if (expiry.current) clearTimeout(expiry.current); const id = active.current; active.current = null; if (id) void requestVoid(`/api/result-demands/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => undefined); }; }, []);
  async function action(kind: "catalog" | "open" | "renew" | "refresh" | "close") {
    const signal = lifetime.current?.signal; setBusy(true); setFailed(false);
    try {
      if (kind === "catalog") {
        const result = await requestJson<{ results: Output[] }>("/api/pipeline-results", { signal });
        if (!signal?.aborted) setOutputs(result.results.filter(output => output.contract.payload_keys.includes("association")));
      } else if (kind === "open") {
        const output = outputs.find(item => item.key === selected); if (!output) return;
        const value = await requestJson<Demand>("/api/result-demands", { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ consumer_id: `manual-association-${crypto.randomUUID()}`, purpose: "manual", result_key: output.key, contract: output.contract, production_revision: output.production_revision, lease_seconds: 30, payload_keys: ["association"] }) });
        if (signal?.aborted) { void requestVoid(`/api/result-demands/${encodeURIComponent(value.id)}`, { method: "DELETE" }).catch(() => undefined); return; }
        active.current = value.id; setDemand(value); expire();
      } else if (active.current) {
        const id = active.current; const path = `/api/result-demands/${encodeURIComponent(id)}`;
        if (kind === "close") { await requestVoid(path, { method: "DELETE", signal }); active.current = null; if (expiry.current) clearTimeout(expiry.current); if (!signal?.aborted) setDemand(null); }
        else if (kind === "renew") { await requestVoid(`${path}/renew`, { method: "POST", signal }); if (!signal?.aborted && active.current === id) expire(); else void requestVoid(path, { method: "DELETE" }).catch(() => undefined); }
        else { const result = await requestJson<Demand>(path, { signal }); if (!signal?.aborted && active.current === id) setDemand(result); }
      }
    } catch { if (!signal?.aborted) { setFailed(true); const id = active.current; active.current = null; setDemand(null); if (id) void requestVoid(`/api/result-demands/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => undefined); } }
    finally { if (!signal?.aborted) setBusy(false); }
  }
  return <fieldset className="formSection"><legend>{t("title")}</legend><p>{t("help")}</p><button className="chipButton" type="button" disabled={busy || !!demand} onClick={() => void action("catalog")}>{t("catalog")}</button><label className="field">{t("output")}<select className="input" value={selected} disabled={!!demand} onChange={event => setSelected(event.target.value)}><option value="">—</option>{outputs.map(output => <option key={output.key} value={output.key}>{output.name || output.key}</option>)}</select></label>
    {!demand ? <button className="chipButton" type="button" disabled={busy || !outputs.some(output => output.key === selected)} onClick={() => void action("open")}>{t("open")}</button> : <><p>{t("received")}: {demand.received}</p><p>{demand.result?.payload.association ? `${demand.result.payload.association.revision} · ${demand.result.payload.association.reason}` : t("waiting")}</p>{(["refresh", "renew", "close"] as const).map(kind => <button className="chipButton" key={kind} type="button" disabled={busy} onClick={() => void action(kind)}>{t(kind)}</button>)}</>}
    {failed && <p role="alert">{t("failed")}</p>}
  </fieldset>;
}
