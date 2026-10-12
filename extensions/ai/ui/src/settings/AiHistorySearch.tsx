import React, { useEffect, useRef, useState } from "react";
import { requestJson, resolveToposyncUrl, type Notification, type NotificationHistoryAssistant, type HostI18n } from "@toposync/plugin-api";

type SearchResult = {
  notifications: Notification[];
  answer?: { status: string; message?: string; citations?: { notification_id: string; field: string; excerpt: string }[] };
  search: { mode: string; status: string; pending?: number; unavailable_images?: number; without_images?: number; scope_limit?: number; reason?: string; results_truncated?: boolean; corpus_truncated?: boolean;
    time_range?: { timezone: string; created_after: number; created_before: number } };
};

export function AiHistorySearch({ context, i18n }: { context: Parameters<NotificationHistoryAssistant["render"]>[0]; i18n: HostI18n }): React.ReactElement {
  const { locale, t } = i18n.useI18n();
  const text = (key: string, values?: Record<string, unknown>) => t(`ext.ai.history.${key}`, values);
  const selection = context.selection?.value.split(":") ?? [];
  const unsupported = Object.keys(context.dataFilter ?? {}).some(key => key !== "camera_id" && key !== "area_id")
    || Boolean(context.selection && (context.selection.id !== "com.toposync.vision.identities" || selection.length !== 2 || !["human", "recognized"].includes(selection[0]) || !/^[a-zA-Z0-9_-]{1,128}$/.test(selection[1])));
  const [openingSource, setOpeningSource] = useState(false);
  const [sourceError, setSourceError] = useState("");
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
  const [identity, setIdentity] = useState(selection[1] || "");
  const [association, setAssociation] = useState(selection[0] || "human");
  const [camera, setCamera] = useState(context.dataFilter?.camera_id || "");
  const [area, setArea] = useState(context.dataFilter?.area_id || "");
  const [mode, setMode] = useState(context.visual ? "visual" : "text");
  const [options, setOptions] = useState<Array<Array<{ value: string; label: string | null; species?: string }> | null>>([null, null, null]);
  const [optionErrors, setOptionErrors] = useState([false, false, false]);
  const [optionsRevision, setOptionsRevision] = useState(0);
  const operation = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!filtersOpen) return;
    const controller = new AbortController();
    setOptions([null, null, null]); setOptionErrors([false, false, false]);
    const locations = (field: string, label: string) => requestJson<{ options: Array<{ value: string; label: string | null }> }>(`/api/notifications/data-options?field=${field}&label_field=${label}`, { signal: controller.signal }).then(result => result.options);
    void Promise.allSettled([
      requestJson<{ identities: Array<{ id: string; name: string; species: string }> }>("/api/vision/identities", { signal: controller.signal }).then(result => result.identities.map(item => ({ value: item.id, label: item.name, species: item.species }))),
      locations("camera_id", "camera_name"), locations("area_id", "area_label"),
    ]).then(results => {
      if (controller.signal.aborted) return;
      setOptions(results.map(result => result.status === "fulfilled" ? result.value : null));
      setOptionErrors(results.map(result => result.status === "rejected"));
    });
    return () => controller.abort();
  }, [filtersOpen, optionsRevision]);
  const [query, setQuery] = useState(context.query);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    operation.current?.abort(); operation.current = null;
    setResult(null); setError(""); setBusy(false);
  }, [query, profile, identity, association, camera, area, mode, context.createdAfter, context.createdBefore]);
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
  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short", ...(range ? { timeZone: range.timezone } : {}) });
  async function search(event: React.FormEvent) {
    event.preventDefault();
    if (operation.current || unsupported) return;
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setError(""); setResult(null);
    try {
      const data = await requestJson<SearchResult>("/api/ai/history/answer", {
        method: "POST", headers: { "content-type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ question: query.trim(), profile_id: profile || null,
          identity_id: identity || null, association, mode, priorities: context.priorities, types: context.types,
          created_after: context.createdAfter, created_before: context.createdBefore,
          data_filter: { ...(camera ? { camera_id: camera } : {}), ...(area ? { area_id: area } : {}) } }),
      });
      if (!controller.signal.aborted) setResult(data);
    } catch (cause) {
      if (controller.signal.aborted) return;
      const status = (cause as { status?: number })?.status;
      setError(status === 403 || status === 401 ? "unauthorized"
        : status === 409 ? "associationsChanged"
        : status === 503 ? "selectionUnavailableHelp"
        : "searchUnavailable");
    } finally {
      if (operation.current === controller) { operation.current = null; setBusy(false); }
    }
  }

  return <section aria-label={text("title")} style={{ display: "grid", gap: 16 }}>
    <form onSubmit={(event) => void search(event)} style={{ display: "grid", gap: 12 }}>
      {profiles.length > 1 && <details><summary>{t("ext.ai.assistant.localModel")}</summary><label className="field">{t("ext.ai.assistant.profile")}<select className="input" value={profile} disabled={busy} onChange={(event) => setProfile(event.target.value)}>
        {profiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label></details>}
      <label className="field">{text("question")}<input className="input" value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)} placeholder={text("example")} /></label>
      <details onToggle={event => setFiltersOpen(event.currentTarget.open)}>
        <summary>{text(identity || camera || area ? "selectedFilters" : "chooseFilters")}</summary>
        <div style={{ display: "grid", gap: 12, minWidth: 0, marginTop: 12 }}>
          {[{ label: "identityFilter", value: identity, change: (value: string) => { setIdentity(value); setAssociation("human"); } },
            { label: "camera", value: camera, change: setCamera }, { label: "area", value: area, change: setArea }].map((field, index) => <label className="field" key={field.label}>{text(field.label)}
            <select className="input" style={{ width: "100%", minWidth: 0 }} value={field.value} onChange={event => field.change(event.target.value)}>
              <option value="">{text(index === 0 ? "allIdentities" : "allLocations")}</option>
              {field.value && !options[index]?.some(item => item.value === field.value) ? <option value={field.value}>{text("selectionUnavailable")}</option> : null}
              {options[index]?.map((item, position) => <option key={item.value} value={item.value}>{index === 0 ? `${item.label} · ${text(["person", "cat", "dog"].includes(item.species ?? "") ? item.species! : "identity")}` : item.label && item.label !== item.value ? item.label : text("unnamedLocation", { number: position + 1 })}</option>)}
            </select>
            {optionErrors[index] ? <span role="status">{text("optionsUnavailable")}</span> : !options[index] ? <span role="status">{text("loadingOptions")}</span> : null}
          </label>)}
          {identity && <label className="field">{text("association")}<select className="input" style={{ width: "100%", minWidth: 0 }} value={association} onChange={event => setAssociation(event.target.value)}>
            <option value="human">{text("humanOnly")}</option><option value="recognized">{text("humanAndAutomatic")}</option>
          </select></label>}
          <label className="field">{text("retrieval")}<select className="input" value={mode} onChange={event => setMode(event.target.value)}>
            <option value="text">{text("textMeaning")}</option><option value="visual">{text("imageSimilarity")}</option>
          </select></label>
          {optionErrors.some(Boolean) && <button type="button" className="chipButton" onClick={() => setOptionsRevision(value => value + 1)}>{text("refreshOptions")}</button>}
        </div>
      </details>
      <button className="primaryButton" disabled={busy || unsupported || !query.trim()}>{text(busy ? "busy" : "title")}</button>
    </form>
    <p>{text("limitations")}</p>
    {unsupported && <p role="alert">{text("unsupportedFilters")}</p>}
    {error && <p role="alert">{text(error)}</p>}
    {sourceError && <p role="alert">{text(sourceError)}</p>}
    {result && <div aria-live="polite">
      {!result.notifications.length && result.answer?.status !== "needs_date" && <p>{text("empty")}</p>}
      {result.answer?.status === "needs_date" && <p>{result.answer.message}</p>}
      {result.answer?.status === "extractive" && <section aria-label={text("answerWithSources")}>
        <p>{text("extractiveHelp")}</p>
        {result.answer.citations?.map((citation, index) => <blockquote key={index} style={{ margin: "12px 0", paddingLeft: 12, borderLeft: "3px solid var(--border)", overflowWrap: "anywhere" }}>
          <p>{citation.excerpt}</p>
          <a style={{ color: "var(--text)", textDecoration: "underline" }} href={`#history-source-${citation.notification_id}`}>{text("sourceNumber", { number: result.notifications.findIndex((item) => item.id === citation.notification_id) + 1 })}</a>
        </blockquote>)}
      </section>}
      {result.answer?.status === "unavailable" && <p>{text("answerUnavailable")}</p>}
      {result.answer?.status === "no_supported_answer" && <p>{text("unsupportedAnswer")}</p>}
      {result.answer?.status !== "needs_date" && <>
      {result.search.reason === "installation_timezone_required" ? <p>{text("timezoneRequired")}</p>
        : <p>{text(result.search.mode === "semantic_visual" ? "visualResults" : result.search.mode === "semantic_text" ? "textResults" : result.search.status === "degraded" ? "literalFallback" : "filteredResults")} {text("rankingLimit")}</p>}
      {range && <p>{text("timeRange", { after: dateFormat.format(new Date(range.created_after * 1000)), before: dateFormat.format(new Date(range.created_before * 1000)), timezone: range.timezone })}</p>}
      <details><summary>{text("coverageTitle")}</summary>
      <p>{text("coverage", { limit: result.search.scope_limit ? text("scopeLimit", { count: result.search.scope_limit }) : "" })}</p>
      {(result.search.results_truncated || result.search.corpus_truncated) && <p>{text("truncated")}</p>}
      {(result.search.pending ?? 0) > 0 && <p>{text("pending", { count: result.search.pending })}</p>}
      {result.search.mode === "semantic_visual" && <>
        {(result.search.unavailable_images ?? 0) > 0 && <p>{text("unavailableImages", { count: result.search.unavailable_images })}</p>}
        {(result.search.without_images ?? 0) > 0 && <p>{text("withoutImages", { count: result.search.without_images })}</p>}
      </>}
      </details>
      <ol>{result.notifications.map((item) => <li id={`history-source-${item.id}`} key={item.id} style={{ marginBottom: 20, overflowWrap: "anywhere" }}>
        <strong>{item.title}</strong>
        {item.createdAt && <p><time dateTime={item.createdAt}>{dateFormat.format(new Date(item.createdAt))}</time></p>}
        {item.description && <p>{item.description}</p>}
        <button type="button" className="chipButton" disabled={openingSource} onClick={async () => {
          setOpeningSource(true); setSourceError("");
          try { await context.onOpenNotification(item.id); }
          catch { setSourceError("sourceUnavailable"); }
          finally { setOpeningSource(false); }
        }}>{text("originalRecord")}</button>
        {item.imageUrl?.startsWith("/files/") && <> · <a style={{ color: "var(--text)", textDecoration: "underline" }} href={resolveToposyncUrl(item.imageUrl)} target="_blank" rel="noreferrer">{text("recordedImage")}</a></>}
      </li>)}</ol></>}
    </div>}
  </section>;
}
