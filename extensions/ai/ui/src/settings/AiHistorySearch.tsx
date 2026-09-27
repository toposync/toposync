import React, { useEffect, useState } from "react";
import { requestJson, resolveToposyncUrl, type Notification } from "@toposync/plugin-api";

type SearchResult = {
  notifications: Notification[];
  answer?: { status: string; message?: string; citations?: { notification_id: string; field: string; excerpt: string }[] };
  search: { mode: string; status: string; pending?: number; scope_limit?: number; reason?: string; results_truncated?: boolean; corpus_truncated?: boolean;
    time_range?: { timezone: string; created_after: number; created_before: number } };
};

export function AiHistorySearch(): React.ReactElement {
  const [profiles, setProfiles] = useState<{ id: string; name: string }[]>([]);
  const [profile, setProfile] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    requestJson<{ profiles: { id: string; name: string }[] }>("/api/ai/history/profiles", { signal: controller.signal })
      .then((value) => { setProfiles(value.profiles); setProfile(value.profiles[0]?.id || ""); })
      .catch(() => {});
    return () => controller.abort();
  }, []);
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const range = result?.search.time_range;
  const dateFormat = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short", ...(range ? { timeZone: range.timezone } : {}) });
  async function search(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setResult(null);
    try {
      setResult(await requestJson<SearchResult>("/api/ai/history/answer", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: query.trim(), profile_id: profile || null }),
      }));
    } catch { setError("Busca indisponível. Consulte o histórico de notificações para pesquisa manual."); }
    finally { setBusy(false); }
  }
  return <section aria-label="Consultar acontecimentos" style={{ display: "grid", gap: 16 }}>
    <form onSubmit={(event) => void search(event)} style={{ display: "grid", gap: 12 }}>
      {profiles.length > 1 && <details><summary>Modelo local</summary><label className="field">Perfil<select className="input" value={profile} disabled={busy} onChange={(event) => setProfile(event.target.value)}>
        {profiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label></details>}
      <label className="field">O que deseja saber?<input className="input" value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)} placeholder="Como os gatos passaram ontem à tarde?" /></label>
      <button className="primaryButton" disabled={busy || !query.trim()}>{busy ? "Consultando…" : "Consultar acontecimentos"}</button>
    </form>
    <p>A consulta recupera registros, sem executar instruções contidas neles. Observações esparsas não comprovam rotina contínua, identidade, intenção ou saúde.</p>
    {error && <p role="alert">{error}</p>}
    {result && <div aria-live="polite">
      {result.answer?.status === "needs_date" && <p>{result.answer.message}</p>}
      {result.answer?.status === "extractive" && <section aria-label="Resposta com fontes">
        <p>Os registros selecionados relatam o seguinte. São trechos das fontes, não confirmação independente dos fatos:</p>
        {result.answer.citations?.map((citation, index) => <blockquote key={index} style={{ margin: "12px 0", paddingLeft: 12, borderLeft: "3px solid var(--border)", overflowWrap: "anywhere" }}>
          <p>{citation.excerpt}</p>
          <a style={{ color: "var(--text)", textDecoration: "underline" }} href={`#history-source-${citation.notification_id}`}>Fonte {result.notifications.findIndex((item) => item.id === citation.notification_id) + 1}</a>
        </blockquote>)}
      </section>}
      {result.answer?.status === "unavailable" && <p>Não foi possível elaborar uma resposta verificável com o modelo local. Consulte os registros abaixo.</p>}
      {result.answer?.status === "no_supported_answer" && <p>Os registros recuperados não sustentaram uma resposta. Confira as fontes e refine a consulta.</p>}
      {result.answer?.status !== "needs_date" && <>
      {result.search.reason === "installation_timezone_required" ? <p>Defina o fuso da instalação em Configurações → Base para buscar datas relativas.</p>
        : <p>{result.search.mode === "semantic_text" ? "Resultados por semelhança de texto, não de imagens." : result.search.status === "degraded" ? "Busca literal: semelhança de texto indisponível." : "Resultados do filtro temporal."} A ordenação não indica certeza.</p>}
      {range && <p>Notificações criadas de {dateFormat.format(new Date(range.created_after * 1000))} até {dateFormat.format(new Date(range.created_before * 1000))}, sem incluir o término ({range.timezone}).</p>}
      <p>Cobertura limitada às notificações disponíveis{result.search.scope_limit ? `, até ${result.search.scope_limit} registros recentes` : ""}. Períodos sem registro permanecem desconhecidos. Hoje/ontem e períodos do dia usam o fuso da instalação.</p>
      {(result.search.results_truncated || result.search.corpus_truncated) && <p>Há registros fora desta seleção. Esta resposta não resume todo o histórico.</p>}
      {result.search.status === "partial" && <p>Indexação parcial: {result.search.pending ?? 0} registros pendentes.</p>}
      {!result.notifications.length && <p>Nenhum registro encontrado. Isso não comprova ausência de acontecimentos. Consulte também o histórico manual.</p>}
      <ol>{result.notifications.map((item) => <li id={`history-source-${item.id}`} key={item.id} style={{ marginBottom: 20, overflowWrap: "anywhere" }}>
        <strong>{item.title}</strong>
        {item.createdAt && <p><time dateTime={item.createdAt}>{dateFormat.format(new Date(item.createdAt))}</time></p>}
        {item.description && <p>{item.description}</p>}
        <a style={{ color: "var(--text)", textDecoration: "underline" }} href={resolveToposyncUrl(`/api/notifications/${encodeURIComponent(item.id)}`)} target="_blank" rel="noreferrer">Registro original</a>
        {item.imageUrl?.startsWith("/files/") && <> · <a style={{ color: "var(--text)", textDecoration: "underline" }} href={resolveToposyncUrl(item.imageUrl)} target="_blank" rel="noreferrer">Imagem registrada</a></>}
      </li>)}</ol></>}
    </div>}
  </section>;
}
