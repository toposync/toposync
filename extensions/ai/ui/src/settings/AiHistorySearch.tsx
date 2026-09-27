import React, { useState } from "react";
import { requestJson, resolveToposyncUrl, type Notification } from "@toposync/plugin-api";

type SearchResult = {
  notifications: Notification[];
  search: { mode: string; status: string; pending?: number; scope_limit?: number; reason?: string;
    time_range?: { timezone: string; created_after: number; created_before: number } };
};

export function AiHistorySearch(): React.ReactElement {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const range = result?.search.time_range;
  const dateFormat = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short", ...(range ? { timeZone: range.timezone } : {}) });
  async function search(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setResult(null);
    try {
      setResult(await requestJson<SearchResult>(`/api/ai/history/search?query=${encodeURIComponent(query.trim())}&limit=10`));
    } catch { setError("Busca indisponível. Consulte o histórico de notificações para pesquisa manual."); }
    finally { setBusy(false); }
  }
  return <section aria-label="Consultar acontecimentos" style={{ display: "grid", gap: 16 }}>
    <form onSubmit={(event) => void search(event)} style={{ display: "grid", gap: 12 }}>
      <label className="field">O que deseja encontrar?<input className="input" value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)} placeholder="Por exemplo: gato perto da água" /></label>
      <button className="primaryButton" disabled={busy || !query.trim()}>{busy ? "Buscando…" : "Buscar acontecimentos"}</button>
    </form>
    <p>A consulta recupera registros, sem executar instruções contidas neles. Observações esparsas não comprovam rotina contínua, identidade, intenção ou saúde.</p>
    {error && <p role="alert">{error}</p>}
    {result && <div aria-live="polite">
      {result.search.reason === "installation_timezone_required" ? <p>Defina o fuso da instalação em Configurações → Base para buscar datas relativas.</p>
        : <p>{result.search.mode === "semantic_text" ? "Resultados por semelhança de texto, não de imagens." : result.search.status === "degraded" ? "Busca literal: semelhança de texto indisponível." : "Resultados do filtro temporal."} A ordenação não indica certeza.</p>}
      {range && <p>Notificações criadas de {dateFormat.format(new Date(range.created_after * 1000))} até {dateFormat.format(new Date(range.created_before * 1000))}, sem incluir o término ({range.timezone}).</p>}
      <p>Cobertura limitada às notificações disponíveis{result.search.scope_limit ? `, até ${result.search.scope_limit} registros recentes` : ""}. Períodos sem registro permanecem desconhecidos. Hoje/ontem e períodos do dia usam o fuso da instalação.</p>
      {result.search.status === "partial" && <p>Indexação parcial: {result.search.pending ?? 0} registros pendentes.</p>}
      {!result.notifications.length && <p>Nenhum registro encontrado. Isso não comprova ausência de acontecimentos. Consulte também o histórico manual.</p>}
      <ol>{result.notifications.map((item) => <li key={item.id} style={{ marginBottom: 20, overflowWrap: "anywhere" }}>
        <strong>{item.title}</strong>
        {item.createdAt && <p><time dateTime={item.createdAt}>{dateFormat.format(new Date(item.createdAt))}</time></p>}
        {item.description && <p>{item.description}</p>}
        <a style={{ color: "var(--text)", textDecoration: "underline" }} href={resolveToposyncUrl(`/api/notifications/${encodeURIComponent(item.id)}`)} target="_blank" rel="noreferrer">Registro original</a>
        {item.imageUrl?.startsWith("/files/") && <> · <a style={{ color: "var(--text)", textDecoration: "underline" }} href={resolveToposyncUrl(item.imageUrl)} target="_blank" rel="noreferrer">Imagem registrada</a></>}
      </li>)}</ol>
    </div>}
  </section>;
}
