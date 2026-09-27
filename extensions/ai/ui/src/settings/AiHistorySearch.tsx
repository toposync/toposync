import React, { useEffect, useRef, useState } from "react";
import { requestJson, resolveToposyncUrl, type Notification } from "@toposync/plugin-api";

type SearchResult = {
  notifications: Notification[];
  answer?: { status: string; message?: string; citations?: { notification_id: string; field: string; excerpt: string }[] };
  search: { mode: string; status: string; pending?: number; unavailable_images?: number; without_images?: number; scope_limit?: number; reason?: string; results_truncated?: boolean; corpus_truncated?: boolean;
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
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [identity, setIdentity] = useState("");
  const [association, setAssociation] = useState("human");
  const [camera, setCamera] = useState("");
  const [area, setArea] = useState("");
  const [mode, setMode] = useState("text");
  const [options, setOptions] = useState<Array<Array<{ value: string; label: string | null }> | null>>([null, null, null]);
  const [optionErrors, setOptionErrors] = useState([false, false, false]);
  const [optionsRevision, setOptionsRevision] = useState(0);
  const operation = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!filtersOpen) return;
    const controller = new AbortController();
    setOptions([null, null, null]); setOptionErrors([false, false, false]);
    const locations = (field: string, label: string) => requestJson<{ options: Array<{ value: string; label: string | null }> }>(`/api/notifications/data-options?field=${field}&label_field=${label}`, { signal: controller.signal }).then(result => result.options);
    void Promise.allSettled([
      requestJson<{ identities: Array<{ id: string; name: string; species: string }> }>("/api/vision/identities", { signal: controller.signal }).then(result => result.identities.map(item => ({ value: item.id, label: `${item.name} · ${{person:"Pessoa",cat:"Gato",dog:"Cão"}[item.species] ?? "Identidade"}` }))),
      locations("camera_id", "camera_name"), locations("area_id", "area_label"),
    ]).then(results => {
      if (controller.signal.aborted) return;
      setOptions(results.map(result => result.status === "fulfilled" ? result.value : null));
      setOptionErrors(results.map(result => result.status === "rejected"));
    });
    return () => controller.abort();
  }, [filtersOpen, optionsRevision]);
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    operation.current?.abort(); operation.current = null;
    setResult(null); setError(""); setBusy(false);
  }, [query, profile, identity, association, camera, area, mode]);
  useEffect(() => {
    const invalidate = () => {
      operation.current?.abort(); operation.current = null;
      setResult(null); setError(""); setBusy(false); setOptionsRevision(value => value + 1);
    };
    window.addEventListener("focus", invalidate);
    window.addEventListener("toposync:identity-gallery-changed", invalidate);
    return () => { operation.current?.abort(); window.removeEventListener("focus", invalidate); window.removeEventListener("toposync:identity-gallery-changed", invalidate); };
  }, []);
  const range = result?.search.time_range;
  const dateFormat = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short", ...(range ? { timeZone: range.timezone } : {}) });
  async function search(event: React.FormEvent) {
    event.preventDefault();
    if (operation.current) return;
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setError(""); setResult(null);
    try {
      const data = await requestJson<SearchResult>("/api/ai/history/answer", {
        method: "POST", headers: { "content-type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ question: query.trim(), profile_id: profile || null,
          identity_id: identity || null, association, mode,
          data_filter: { ...(camera ? { camera_id: camera } : {}), ...(area ? { area_id: area } : {}) } }),
      });
      if (!controller.signal.aborted) setResult(data);
    } catch (cause) {
      if (controller.signal.aborted) return;
      const status = (cause as { status?: number })?.status;
      setError(status === 403 || status === 401 ? "Acesso à consulta ou à seleção não autorizado. Revise os filtros."
        : status === 409 ? "As associações mudaram durante a consulta. Confira a seleção e consulte novamente."
        : status === 503 ? "A seleção está indisponível. Ela foi preservada; revise os filtros antes de consultar novamente."
        : "Busca indisponível. A seleção foi preservada. Consulte o histórico para pesquisa manual.");
    } finally {
      if (operation.current === controller) { operation.current = null; setBusy(false); }
    }
  }

  return <section aria-label="Consultar acontecimentos" style={{ display: "grid", gap: 16 }}>
    <form onSubmit={(event) => void search(event)} style={{ display: "grid", gap: 12 }}>
      {profiles.length > 1 && <details><summary>Modelo local</summary><label className="field">Perfil<select className="input" value={profile} disabled={busy} onChange={(event) => setProfile(event.target.value)}>
        {profiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label></details>}
      <label className="field">O que deseja saber?<input className="input" value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)} placeholder="Como os gatos passaram ontem à tarde?" /></label>
      <details onToggle={event => setFiltersOpen(event.currentTarget.open)}>
        <summary>{identity || camera || area ? "Filtros selecionados" : "Restringir busca e escolher modo"}</summary>
        <div style={{ display: "grid", gap: 12, minWidth: 0, marginTop: 12 }}>
          {[{ label: "Pessoa ou pet", value: identity, change: (value: string) => { setIdentity(value); setAssociation("human"); } },
            { label: "Câmera", value: camera, change: setCamera }, { label: "Área", value: area, change: setArea }].map((field, index) => <label className="field" key={field.label}>{field.label}
            <select className="input" style={{ width: "100%", minWidth: 0 }} value={field.value} onChange={event => field.change(event.target.value)}>
              <option value="">{index === 0 ? "Todas as pessoas e pets" : "Todas"}</option>
              {field.value && !options[index]?.some(item => item.value === field.value) ? <option value={field.value}>Seleção indisponível</option> : null}
              {options[index]?.map((item, position) => <option key={item.value} value={item.value}>{item.label && item.label !== item.value ? item.label : `Local sem nome ${position + 1}`}</option>)}
            </select>
            {optionErrors[index] ? <span role="status">Opções indisponíveis ou sem acesso. A seleção foi mantida.</span> : !options[index] ? <span role="status">Carregando opções…</span> : null}
          </label>)}
          {identity && <label className="field">Associação<select className="input" style={{ width: "100%", minWidth: 0 }} value={association} onChange={event => setAssociation(event.target.value)}>
            <option value="human">Somente confirmação humana</option><option value="recognized">Humanas e automáticas</option>
          </select></label>}
          <label className="field">Recuperar fontes por<select className="input" value={mode} onChange={event => setMode(event.target.value)}>
            <option value="text">Significado do texto</option><option value="visual">Similaridade de imagens</option>
          </select></label>
          {optionErrors.some(Boolean) && <button type="button" className="chipButton" onClick={() => setOptionsRevision(value => value + 1)}>Atualizar opções</button>}
        </div>
      </details>
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
        : <p>{result.search.mode === "semantic_visual" ? "Fontes recuperadas por similaridade visual; a resposta cita apenas textos registrados, sem confirmar identidade." : result.search.mode === "semantic_text" ? "Resultados por semelhança de texto, não de imagens." : result.search.status === "degraded" ? "Busca literal: o modo de semelhança solicitado está indisponível." : "Resultados dos filtros selecionados."} A ordenação não indica certeza.</p>}
      {range && <p>Notificações criadas de {dateFormat.format(new Date(range.created_after * 1000))} até {dateFormat.format(new Date(range.created_before * 1000))}, sem incluir o término ({range.timezone}).</p>}
      <p>Cobertura limitada às notificações disponíveis{result.search.scope_limit ? `, até ${result.search.scope_limit} registros recentes` : ""}. Períodos sem registro permanecem desconhecidos. Hoje/ontem e períodos do dia usam o fuso da instalação.</p>
      {(result.search.results_truncated || result.search.corpus_truncated) && <p>Há registros fora desta seleção. Esta resposta não resume todo o histórico.</p>}
      {(result.search.pending ?? 0) > 0 && <p>Indexação parcial: {result.search.pending} registros pendentes.</p>}
      {result.search.mode === "semantic_visual" && <>
        {(result.search.unavailable_images ?? 0) > 0 && <p>{result.search.unavailable_images} imagens indisponíveis para busca visual. Esses registros não foram avaliados por imagem.</p>}
        {(result.search.without_images ?? 0) > 0 && <p>{result.search.without_images} registros sem imagem ficaram fora da busca visual. Consulte também por texto.</p>}
      </>}
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
