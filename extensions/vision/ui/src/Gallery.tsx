import React, { useEffect, useId, useRef, useState } from "react";
import type { HostI18n, Notification } from "@toposync/plugin-api";
import { changed, photoUrl, read, write, type GalleryData, type Identity, type Observation, type Occurrence, type Operation, type Species } from "./api";
import { styles } from "./styles";

type Translate = (key: string, params?: Record<string, unknown>) => string;
function useText(i18n: HostI18n): Translate {
  const { t } = i18n.useI18n();
  return (key, params) => t(`ext.vision.identity.${params?.count === 1 && ["selection", "references", "visits"].includes(key) ? `${key}One` : key}`, params);
}
function useRevision() {
  const [revision, setRevision] = useState(0);
  useEffect(() => { const update = () => setRevision((value) => value + 1); window.addEventListener("toposync:identity-gallery-changed", update); return () => window.removeEventListener("toposync:identity-gallery-changed", update); }, []);
  return revision;
}
function useLoad<T>(path: string | null, refreshKey = "", occurrenceDeadline = 0) {
  const revision = useRevision();
  const [state, setState] = useState<{ data?: T; path?: string; loading: boolean; waiting?: boolean; error: boolean }>({ loading: true, error: false });
  useEffect(() => {
    if (path === null) { setState({ loading: false, error: false }); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState((previous) => ({ data: previous.path === path ? previous.data : undefined, path, loading: true, error: false }));
    const load = async () => {
      try {
        const data = await read<T>(path, controller.signal);
        if (!controller.signal.aborted) {
          setState({ data, path, loading: false, error: false });
          if ((data as { closed?: boolean }).closed === false && Date.now() < occurrenceDeadline && document.visibilityState === "visible") {
            timer = setTimeout(() => { void load(); }, Math.min(2000, occurrenceDeadline - Date.now()));
          }
        }
      } catch (cause) {
        if (controller.signal.aborted) return;
        const status = (cause as { status?: number })?.status;
        // A recent occurrence can reach notifications before the recognition branch.
        // Access errors stop immediately and clear previously visible identity data.
        if (status === 404 && Date.now() < occurrenceDeadline && document.visibilityState === "visible") {
          setState({ path, loading: false, waiting: true, error: false });
          timer = setTimeout(() => { void load(); }, Math.min(2000, occurrenceDeadline - Date.now()));
        } else setState({ path, loading: false, error: true });
      }
    };
    void load();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [path, revision, refreshKey, occurrenceDeadline]);
  return state;
}
function occurrenceDeadline(notification: Notification): number {
  const timestamp = Date.parse(notification.updatedAt ?? notification.createdAt ?? "");
  return Number.isFinite(timestamp) ? timestamp + 60_000 : 0;
}
type PhotoPage = { observations: Observation[]; revision: number; next_cursor: string | null };
function usePhotos(path: string | null) {
  const revision = useRevision();
  const [state, setState] = useState<{ data?: PhotoPage; path?: string; loading: boolean; error: boolean }>({ loading: false, error: false });
  const operation = useRef<AbortController>();
  const locked = useRef(false);
  useEffect(() => {
    operation.current?.abort(); locked.current = false;
    if (path === null) { setState({ loading: false, error: false }); return; }
    const controller = new AbortController(); operation.current = controller;
    setState((previous) => ({ data: previous.path === path ? previous.data : undefined, path, loading: true, error: false }));
    void read<PhotoPage>(path, controller.signal).then((data) => {
      if (!controller.signal.aborted) setState({ data, path, loading: false, error: false });
    }).catch(() => { if (!controller.signal.aborted) setState({ path, loading: false, error: true }); });
    return () => operation.current?.abort();
  }, [path, revision]);
  const more = async () => {
    const cursor = state.data?.next_cursor;
    if (!path || !cursor || state.loading || locked.current) return;
    locked.current = true;
    const controller = new AbortController(); operation.current = controller;
    setState((previous) => ({ ...previous, loading: true, error: false }));
    try {
      const data = await read<PhotoPage>(`${path}&cursor=${encodeURIComponent(cursor)}`, controller.signal);
      if (!controller.signal.aborted) setState((previous) => ({ path, loading: false, error: false, data: { ...data, observations: [...(previous.data?.observations || []), ...data.observations] } }));
    } catch (cause) {
      if (!controller.signal.aborted) {
        const status = (cause as { status?: number })?.status;
        // Revoked access must clear cached photos/cursors before a later retry.
        setState((previous) => status === 401 || status === 403
          ? { path, loading: false, error: true }
          : { ...previous, loading: false, error: true });
      }
    }
    finally { if (operation.current === controller) locked.current = false; }
  };
  return { ...state, more };
}
function useMutation() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const locked = useRef(false);
  const retryKey = useRef<{ signature: string; key: string }>();
  const run = async (path: string, body: Record<string, unknown>, method = "POST"): Promise<boolean> => {
    if (locked.current) return false;
    locked.current = true; setBusy(true); setError("");
    const signature = JSON.stringify([path, body, method]);
    if (retryKey.current?.signature !== signature) retryKey.current = { signature, key: crypto.randomUUID() };
    try {
      await write(path, path === "/curation" ? { ...body, request_key: retryKey.current.key } : body, method);
      retryKey.current = undefined; changed(); return true;
    } catch (cause) {
      const text = String(cause);
      setError(text.includes("409") || text.includes("revision") || text.includes("changed") ? "conflict" : "error"); return false;
    } finally { locked.current = false; setBusy(false); }
  };
  return { busy, error, run };
}
function ErrorMessage({ t, message = "error", onRetry = changed }: { t: Translate; message?: string; onRetry?: () => void }) {
  return <div className="identityError" role="alert"><p>{t(message)}</p><button type="button" className="chipButton" onClick={onRetry}>{t("retry")}</button></div>;
}
type RetentionPreview = { enabled: boolean; revision: number; eligible_observations: number; eligible_references: number; eligible_history: number; eligible_occurrences: number; maintenance_error: string | null };
function Retention({ t }: { t: Translate }) {
  const heading = useRef<HTMLElement>(null);
  const [open, setOpen] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const preview = useLoad<RetentionPreview>(open ? "/retention-preview" : null);
  const { busy, error, run } = useMutation();
  const data = preview.data;
  useEffect(() => { setAcknowledged(false); }, [data?.revision, open]);
  return <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary ref={heading}>{t("retention")}</summary>
    {open && (preview.error ? <ErrorMessage t={t} /> : !data ? <p role="status">{t("loading")}</p> : <div className="identityForm">
      <p role="status">{t(data.enabled ? "retentionActive" : "retentionInactive")}</p>
      <p>{t("retentionPeriods")}</p><p className="identityMuted">{t("retentionEffect")}</p>
      <p>{t("retentionImpact", { photos: data.eligible_observations, references: data.eligible_references, history: data.eligible_history, visits: data.eligible_occurrences })}</p>
      {data.maintenance_error && <p role="alert">{t("retentionFailure")}</p>}
      {!data.enabled && <label><input type="checkbox" checked={acknowledged} disabled={busy || preview.loading} onChange={(event) => setAcknowledged(event.target.checked)} />{t("retentionConsent")}</label>}
      {error && <ErrorMessage t={t} message={error} />}
      <div className="identityActions"><button type="button" className="chipButton" disabled={busy || preview.loading || (!data.enabled && !acknowledged)} onClick={() => { void run("/retention", { enabled: !data.enabled, expected_revision: data.revision }, "PUT").then((saved) => { if (saved) heading.current?.focus(); }); }}>{t(busy ? "saving" : data.enabled ? "retentionDisable" : "retentionEnable")}</button></div>
    </div>)}
  </details>;
}
function Photo({ observation, portrait = false, full = false, t }: { observation: Pick<Observation,"id">; portrait?: boolean; full?: boolean; t: Translate }) {
  const [failedId, setFailedId] = useState<string | null>(null);
  return failedId === observation.id ? <span className="identityMuted">{t("missing")}</span> : <img className={full ? "identityFullPhoto" : portrait ? "identityPortrait" : "identityPhoto"} src={photoUrl(observation.id)} alt={t("photo")} loading="lazy" referrerPolicy="no-referrer" onError={() => setFailedId(observation.id)} />;
}
function PhotoInspector({ observation, t, onClose }: { observation: Pick<Observation, "id">; t: Translate; onClose: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [observation.id]);
  return <section className="identityInspector" aria-label={t("photo")}>
    <div className="identityToolbar"><h3 tabIndex={-1} ref={heading}>{t("photo")}</h3><button type="button" className="chipButton" onClick={onClose}>{t("backToPhotos")}</button></div>
    <Photo observation={observation} full t={t} />
  </section>;
}
function Editor({ species, observations, identities, revision, t, onClose, initialIdentity = "" }: { species: Species; observations: Observation[]; identities: Identity[]; revision: number; t: Translate; onClose: () => void; initialIdentity?: string }) {
  const available = identities.filter((item) => item.species === species);
  const [mode, setMode] = useState(available.length ? "existing" : "new");
  const [identity, setIdentity] = useState(initialIdentity);
  const [query, setQuery] = useState("");
  const choiceName = useId();
  const matching = available.filter((item) => item.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const [name, setName] = useState("");
  const [reference, setReference] = useState(false);
  const { busy, error, run } = useMutation();
  const title = useRef<HTMLHeadingElement>(null);
  useEffect(() => { title.current?.focus(); }, []);
  return <form className="identityForm" onSubmit={(event) => {
    event.preventDefault();
    void run("/curation", { action: "identify", expected_revision: revision, values: { species, observation_ids: observations.map((item) => item.id), ...(mode === "new" ? { name: name.trim() } : { identity_id: identity }), use_as_reference: reference } }).then((saved) => { if (saved) onClose(); });
  }}>
    <h3 tabIndex={-1} ref={title}>{t("who")}</h3><p className="identityMuted">{t("selection", { count: observations.length })}</p>
    <fieldset disabled={busy}>
      {available.length > 0 && <label><input type="radio" checked={mode === "existing"} onChange={() => setMode("existing")} />{t("existing")}</label>}
      {mode === "existing" && <>
        <label className="identityField">{t("search")}<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} autoComplete="off" /></label>
        <div className="identityChoices" role="group" aria-label={t("choose")}>
          {matching.map((item) => <label className={`identityChoice ${identity === item.id ? "isSelected" : ""}`} key={item.id}>
            <input type="radio" name={choiceName} value={item.id} checked={identity === item.id} onChange={() => setIdentity(item.id)} />
            {item.representative_id && <Photo observation={{ id: item.representative_id }} portrait t={t} />}
            <span className="identityName">{item.name}</span>
          </label>)}
          {!matching.length && <p className="identityMuted">{t("noMatches")}</p>}
        </div>
      </>}
      <label><input type="radio" checked={mode === "new"} onChange={() => setMode("new")} />{t("create")}</label>
      {mode === "new" && <label className="identityField">{t("name")}<input maxLength={120} required value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" /></label>}
      <label><input type="checkbox" disabled={!observations.some((item) => item.eligible)} checked={reference} onChange={(event) => setReference(event.target.checked)} />{t("reference")}</label><p className="identityMuted">{t("referenceHelp")}</p>
      {error && <ErrorMessage t={t} message={error} />}
      <div className="identityActions"><button className="chipButton" type="button" onClick={onClose}>{t("cancel")}</button><button className="primaryButton" type="submit" disabled={mode === "new" ? !name.trim() : !identity}>{t(busy ? "saving" : "save")}</button></div>
    </fieldset>
  </form>;
}
export function occurrenceId(notification: Notification): string | null {
  const payload = notification.payload as { data?: { recognition?: { occurrence_id?: unknown } }; recognition?: { occurrence_id?: unknown } } | undefined;
  const value = payload?.data?.recognition?.occurrence_id ?? payload?.recognition?.occurrence_id;
  return typeof value === "string" && value.length > 0 ? value : null;
}
type GroupMember = { recognition_occurrence_id: string; category: Species };
function groupMembers(notification: Notification): GroupMember[] {
  const payload = notification.payload as { subject?: { type?: unknown; members?: unknown }; data?: { subject?: { type?: unknown; members?: unknown } } } | undefined;
  const subject = payload?.data?.subject ?? payload?.subject;
  if (subject?.type !== "group_event" || !Array.isArray(subject.members)) return [];
  const seen = new Set<string>();
  return subject.members.filter((member): member is GroupMember => {
    if (!member || typeof member !== "object" || typeof member.recognition_occurrence_id !== "string" || !member.recognition_occurrence_id || member.recognition_occurrence_id.length > 512 || !["person", "cat", "dog"].includes(member.category) || seen.has(member.recognition_occurrence_id)) return false;
    seen.add(member.recognition_occurrence_id); return true;
  });
}
export function supportsRecognition(notification: Notification): boolean {
  return occurrenceId(notification) !== null || groupMembers(notification).length > 0;
}
function MemberChoice({ member, selected, choose, t, index, refreshKey, deadline }: { deadline: number; refreshKey: string; member: GroupMember; selected: boolean; choose: () => void; t: Translate; index: number }) {
  const visit = useLoad<Occurrence>(`/occurrences/${encodeURIComponent(member.recognition_occurrence_id)}`, refreshKey, deadline);
  const name = visit.data?.identities.find((item) => item.id === visit.data?.decision.identity_id)?.name;
  return <button type="button" className={`identityTile ${selected ? "isSelected" : ""}`} aria-pressed={selected} onClick={choose}>
    {visit.data?.observations[0] && <Photo observation={visit.data.observations[0]} t={t} />}
    <span className="identityName">{name || `${t(member.category)} ${index + 1}`}</span>
    {visit.loading && !visit.data && <span>{t("loading")}</span>}
    {visit.waiting && <span>{t("pending")}</span>}
    {visit.error && <span>{t("memberUnavailable")}</span>}
  </button>;
}
export function NotificationIdentity({ notification, i18n, summary = false }: { notification: Notification; i18n: HostI18n; summary?: boolean }) {
  const t = useText(i18n);
  const members = groupMembers(notification);
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const individual = occurrenceId(notification);
  if (individual) return <IndividualIdentity notification={notification} i18n={i18n} summary={summary} />;
  if (!members.length || summary) return null;
  const selectedMember = members.find((member) => member.recognition_occurrence_id === selected);
  const start = Math.min(page, Math.floor((members.length - 1) / 12)) * 12;
  return <section className="identityGallery" aria-label={t("groupMembers")}><style>{styles}</style>
    <h3>{t("groupMembers")}</h3><p className="identityMuted">{t("groupHelp")}</p>
    <div className="identityGrid">{members.slice(start, start + 12).map((member, index) => <MemberChoice key={member.recognition_occurrence_id} member={member} refreshKey={notification.updatedAt ?? ""} deadline={occurrenceDeadline(notification)} selected={member.recognition_occurrence_id === selected} choose={() => setSelected(member.recognition_occurrence_id)} t={t} index={start + index} />)}</div>
    {members.length > 12 && <div className="identityActions"><button type="button" className="chipButton" disabled={start === 0} onClick={() => setPage(Math.max(0, page - 1))}>{t("previousMembers")}</button><button type="button" className="chipButton" disabled={start + 12 >= members.length} onClick={() => setPage(page + 1)}>{t("nextMembers")}</button></div>}
    {selectedMember && <IndividualIdentity key={selectedMember.recognition_occurrence_id} notification={notification} i18n={i18n} selectedOccurrence={selectedMember.recognition_occurrence_id} />}
  </section>;
}
function IndividualIdentity({ notification, i18n, summary = false, selectedOccurrence }: { notification: Notification; i18n: HostI18n; summary?: boolean; selectedOccurrence?: string }) {
  const t = useText(i18n);
  const key = selectedOccurrence ?? occurrenceId(notification);
  const visit = useLoad<Occurrence>(key ? `/occurrences/${encodeURIComponent(key)}` : null, notification.updatedAt ?? "", occurrenceDeadline(notification));
  const [editing, setEditing] = useState(false);
  const [inspecting, setInspecting] = useState<string | null>(null);
  const photoButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (inspecting && !visit.data?.observations.some((item) => item.id === inspecting)) setInspecting(null); }, [inspecting, visit.data]);
  const gallery = useLoad<GalleryData>(key && editing && !summary ? "" : null);
  const button = useRef<HTMLButtonElement>(null);
  const { busy, error, run } = useMutation();
  const close = () => { setEditing(false); requestAnimationFrame(() => button.current?.focus()); };
  const current = visit.data?.identities.find((item) => item.id === visit.data?.decision.identity_id);
  if (summary) return current ? <span className="notificationCardDesc">{current.name}</span> : null;
  if (visit.loading && !visit.data) return <p role="status">{t("loading")}</p>;
  if (visit.waiting) return <section className="identityGallery"><p role="status">{t("pending")}</p><button type="button" className="chipButton" onClick={changed}>{t("retry")}</button></section>;
  if (visit.error) return <section className="identityGallery"><ErrorMessage t={t} /></section>;
  if (!visit.data) return null;
  const detail = visit.data;
  const writable = detail.editable;
  const candidates = detail.identities.filter((item) => detail.decision.candidate_ids.includes(item.id));
  return <section className="identityGallery" aria-label={t("title")}><style>{styles}</style>
    <div className="identityToolbar">{detail.observations[0] && <button type="button" className="identityPhotoButton" aria-label={t("viewPhoto")} ref={photoButton} onClick={() => setInspecting(detail.observations[0].id)}><Photo observation={detail.observations[0]} portrait t={t} /></button>}<div><h3>{current?.name || t(detail.decision.status)}</h3>{detail.decision.reason === "calibration_required" && <p className="identityMuted">{t("calibration_required")}</p>}</div></div>
    {inspecting && <PhotoInspector observation={{ id: inspecting }} t={t} onClose={() => { setInspecting(null); requestAnimationFrame(() => photoButton.current?.focus()); }} />}
    {error && <ErrorMessage t={t} message={error} />}
    {!writable && detail.observations.length > 0 && <p className="identityMuted">{t("readOnly")}</p>}
    {editing && writable ? gallery.error ? <ErrorMessage t={t} /> : !gallery.data ? <p role="status">{t("loading")}</p> : <Editor species={detail.species} observations={detail.observations} identities={gallery.data.identities} revision={detail.revision} t={t} onClose={close} initialIdentity={current?.id} /> : <>
      {candidates.length > 0 && <div><p className="identityMuted">{t("suggestions")}</p>{candidates.map((item) => <div className="identityToolbar" key={item.id}><span>{item.name}</span><button type="button" className="chipButton" disabled={busy || !writable} onClick={() => { void run("/curation", { action: "reject", expected_revision: detail.revision, values: { identity_id: item.id, observation_ids: detail.observations.map((sample) => sample.id) } }); }}>{t("reject")}</button></div>)}</div>}
      {detail.observations.length > 0 && <div className="identityActions"><button className="chipButton" type="button" ref={button} disabled={!writable || busy} onClick={() => setEditing(true)}>{t(current ? "correct" : "identify")}</button></div>}
      {!detail.observations.length && <p className="identityMuted">{t(detail.decision.reason === "identity_deleted" ? "deletedPhotos" : "missing")}</p>}
    </>}
  </section>;
}
export function Gallery({ i18n }: { i18n: HostI18n }) {
  const t = useText(i18n);
  const [search, setSearch] = useState("");
  const [species, setSpecies] = useState("");
  const [profile, setProfile] = useState<string | null>(null);
  const [review, setReview] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [action, setAction] = useState("");
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [inspecting, setInspecting] = useState<string | null>(null);
  const photoButton = useRef<HTMLButtonElement | null>(null);
  useEffect(() => { setInspecting(null); }, [profile, review, species]);
  const galleryChanges = useRevision();
  useEffect(() => { setSelected([]); setEditing(false); setAction(""); setInspecting(null); }, [galleryChanges]);
  const profileHeading = useRef<HTMLHeadingElement>(null);
  const pendingFocus = useRef(false);
  const actionHeading = useRef<HTMLHeadingElement>(null);
  const gallerySearch = useRef<HTMLInputElement>(null);
  const historyHeading = useRef<HTMLElement>(null);
  const gallery = useLoad<GalleryData>("");
  const photoPath = profile ? `/observations?identity_id=${encodeURIComponent(profile)}` : review ? `/observations?unassigned=true${species ? `&species=${species}` : ""}` : null;
  const photos = usePhotos(photoPath);
  const history = useLoad<{ operations: Operation[]; revision: number }>(showHistory ? "/history" : null);
  const { busy, error, run } = useMutation();
  const current = gallery.data?.identities.find((item) => item.id === profile);
  const writable = gallery.data?.permissions.curate === true;
  const observations = (photos.data?.observations || []).filter((item) => (!review || !item.identity_id) && (!species || item.species === species));
  const reviewGroups = new Map<string, Observation[]>();
  for (const item of observations) {
    const key = review ? item.cluster_id || item.occurrence_id || item.id : item.id;
    reviewGroups.set(key, [...(reviewGroups.get(key) || []), item]);
  }
  const tiles = Array.from(reviewGroups.values());
  const chosen = observations.filter((item) => selected.includes(item.id));
  useEffect(() => {
    const visible = new Set((photos.data?.observations || []).map((item) => item.id));
    setSelected((previous) => { const retained = previous.filter((id) => visible.has(id)); return retained.length === previous.length ? previous : retained; });
  }, [photos.data]);
  const selectedSpecies = chosen[0]?.species;
  const identities = (gallery.data?.identities || []).filter((item) => (!species || item.species === species) && item.name.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const reset = () => { setSelected([]); setEditing(false); setAction(""); setTarget(""); };
  const resetAfterSave = () => { pendingFocus.current = true; reset(); };
  useEffect(() => {
    // Loading temporarily unmounts these controls; focus only their settled render.
    if (!pendingFocus.current || gallery.loading || photos.loading || gallery.error || photos.error || (photoPath !== null && photos.path !== photoPath)) return;
    const destination = profile ? profileHeading.current : gallerySearch.current;
    if (destination) { destination.focus(); pendingFocus.current = false; }
  });
  useEffect(() => { if (action) actionHeading.current?.focus(); }, [action]);
  const saveAction = async () => {
    const revision = gallery.data?.revision;
    const saved = action === "delete" ? await run(`/${encodeURIComponent(profile!)}`, { expected_revision: revision }, "DELETE") : await run("/curation", { action, expected_revision: revision, values: action === "merge" ? { source_id: profile, target_id: target } : { identity_id: profile, name } });
    if (saved) { resetAfterSave(); if (action !== "rename") setProfile(null); }
  };
  return <section className="identityGallery" aria-label={t("title")}><style>{styles}</style>
    {error && <ErrorMessage t={t} message={error} />}
    <div className="identityActions"><button type="button" className="chipButton" disabled={gallery.loading || photos.loading || busy} onClick={changed}>{t("retry")}</button></div>
    {gallery.error || photos.error ? <ErrorMessage t={t} onRetry={() => { pendingFocus.current = true; changed(); }} /> : (gallery.loading && !gallery.data) || (photos.loading && !photos.data) ? <p role="status">{t("loading")}</p> : <>
      {current ? <div className="identityToolbar"><button className="chipButton" type="button" onClick={() => { setProfile(null); resetAfterSave(); }}>{t("back")}</button><h3 ref={profileHeading} tabIndex={-1} className="identityName">{current.name}</h3><span className="identityMuted">{t("references", { count: current.references })}</span></div> : <div className="identityToolbar"><input ref={gallerySearch} aria-label={t("search")} placeholder={t("search")} value={search} onChange={(event) => setSearch(event.target.value)} /><label><select aria-label={t("all")} value={species} onChange={(event) => { setSpecies(event.target.value); reset(); }}><option value="">{t("all")}</option>{(["person", "cat", "dog"] as Species[]).map((item) => <option key={item} value={item}>{t(item)}</option>)}</select></label><label><input type="checkbox" checked={review} onChange={(event) => { setReview(event.target.checked); reset(); }} />{t("review")}</label></div>}
      {!profile && !review && <div className="identityGrid">{identities.map((item) => <button className="identityTile" type="button" key={item.id} onClick={() => { setProfile(item.id); setReview(false); resetAfterSave(); }}>{item.representative_id && <Photo observation={{id:item.representative_id}} t={t} />}<span className="identityName">{item.name}</span><span className="identityMuted">{t(item.species)} · {t("references", { count: item.references })}</span></button>)}</div>}
      {!profile && !review && !identities.length && <p className="identityMuted">{t(gallery.data?.identities.length ? "noSearchResults" : "empty")}</p>}
      {!writable && <p className="identityMuted">{t("readOnly")}</p>}
      {current && writable && <div className="identityActions">{["rename", "merge", "delete"].map((item) => <button className="chipButton" type="button" key={item} onClick={() => { setAction(item); setName(current.name); setEditing(false); }}>{t(item)}</button>)}</div>}
      {action && current && writable && <form className="identityForm" onSubmit={(event) => { event.preventDefault(); void saveAction(); }}><h3 ref={actionHeading} tabIndex={-1}>{t(action)}</h3><fieldset disabled={busy}>{action === "rename" ? <label className="identityField">{t("name")}<input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label> : <><p>{t(action === "merge" ? "mergeHelp" : "deleteHelp")}</p><div className="identityToolbar">{[current, ...(action === "merge" ? gallery.data!.identities.filter((item) => item.id === target) : [])].map((item) => <div className="identityTile" key={item.id}>{item.representative_id && <Photo observation={{id:item.representative_id}} portrait t={t} />}<span className="identityName">{item.name}</span><span>{t("visits",{count:item.occurrences})} · {t("references",{count:item.references})}</span></div>)}</div>{action === "merge" && <label className="identityField">{t("existing")}<select required value={target} onChange={(event) => setTarget(event.target.value)}><option value="">{t("choose")}</option>{gallery.data?.identities.filter((item) => item.species === current.species && item.id !== profile).map((item) => <option key={item.id} value={item.id}>{item.name} · {t("references", { count: item.references })}</option>)}</select></label>}</>}<div className="identityActions"><button className="chipButton" type="button" onClick={resetAfterSave}>{t("cancel")}</button><button className="chipButton" type="submit" disabled={action === "merge" && !target}>{t(busy ? "saving" : action === "delete" ? "confirmDelete" : action === "merge" ? "confirmMerge" : "save")}</button></div></fieldset></form>}
      {inspecting && <PhotoInspector observation={{ id: inspecting }} t={t} onClose={() => { setInspecting(null); requestAnimationFrame(() => photoButton.current?.isConnected ? photoButton.current.focus() : gallerySearch.current?.focus()); }} />}
      {(profile || review) && <><p className="identityMuted">{t("selection", { count: chosen.length })}</p><div className="identityGrid">{tiles.map((group, index) => { const item=group[0]; const identifiers=group.map((sample)=>sample.id); return <div key={item.id} className={`identityTile ${identifiers.every((id)=>selected.includes(id)) ? "isSelected" : ""}`}>
        <button type="button" className="identityPhotoButton" aria-label={t("viewPhotoNumber", { index: index + 1 })} onClick={(event) => { photoButton.current = event.currentTarget; setInspecting(item.id); }}><Photo observation={item} t={t} /></button>
        <label><input type="checkbox" disabled={!writable || group.some((sample) => !sample.editable)} aria-label={t("selectPhoto", { index: index + 1 })} checked={identifiers.every((id)=>selected.includes(id))} onChange={(event) => setSelected((previous) => event.target.checked ? Array.from(new Set([...previous,...identifiers])) : previous.filter((id) => !identifiers.includes(id)))} /> {review ? t("visits",{count:new Set(group.map((sample)=>sample.occurrence_id)).size}) : item.reference ? t("referenceBadge") : item.confirmed ? t("identifiedBadge") : t("review")}</label></div>; })}</div>{!observations.length && <p>{t("emptyPhotos")}</p>}
      {photos.data?.next_cursor && <button type="button" className="chipButton" disabled={photos.loading} onClick={() => { void photos.more(); }}>{t(photos.loading ? "loading" : "morePhotos")}</button>}
      {current && writable && chosen.length > 0 && !editing && <div className="identityActions">
        <button type="button" className="chipButton" disabled={busy || chosen.some((item) => !item.eligible || !item.confirmed) || chosen.every((item) => item.reference)} onClick={() => { void run("/curation", { action: "reference", expected_revision: gallery.data!.revision, values: { observation_ids: chosen.map((item) => item.id), enabled: true } }).then((saved) => { if (saved) resetAfterSave(); }); }}>{t("addReferences")}</button>
        <button type="button" className="chipButton" disabled={busy || !chosen.some((item) => item.reference)} onClick={() => { void run("/curation", { action: "reference", expected_revision: gallery.data!.revision, values: { observation_ids: chosen.filter((item) => item.reference).map((item) => item.id), enabled: false } }).then((saved) => { if (saved) resetAfterSave(); }); }}>{t("removeReferences")}</button>
      </div>}
      {writable && chosen.every((item) => item.editable) && chosen.length > 0 && !editing && <div className="identityActions"><button type="button" className="chipButton" disabled={busy} onClick={() => { void run("/curation", { action: "unassign", expected_revision: gallery.data!.revision, values: { observation_ids: chosen.map((item) => item.id) } }).then((saved) => { if (saved) resetAfterSave(); }); }}>{t("removeAssociation")}</button><button type="button" className="chipButton" disabled={!selectedSpecies || chosen.some((item) => item.species !== selectedSpecies)} onClick={() => { setEditing(true); setAction(""); }}>{t("move")}</button></div>}
      {editing && writable && chosen.every((item) => item.editable) && selectedSpecies && <Editor species={selectedSpecies} observations={chosen} identities={gallery.data!.identities} revision={gallery.data!.revision} t={t} onClose={resetAfterSave} />}</>}
    </>}
    {gallery.data?.permissions.history && <Retention t={t} />}
    {gallery.data?.permissions.history && <details open={showHistory} onToggle={(event) => setShowHistory(event.currentTarget.open)}><summary ref={historyHeading}>{t("history")}</summary>{showHistory && (history.error ? <ErrorMessage t={t} /> : history.loading ? <p>{t("loading")}</p> : history.data?.operations.length ? history.data.operations.map((item) => <div className="identityHistory" key={item.id}><span>{t(({ assign: "identify", unassign: "removeAssociation", reference: "referenceBadge", reject: "reject" } as Record<string, string>)[item.action] || item.action)} · {new Date(item.created_at * 1000).toLocaleString(i18n.getLocale())}</span><button className="chipButton" type="button" disabled={busy || Boolean(item.undone) || item.id !== history.data?.operations.find((operation) => !operation.undone)?.id} onClick={() => { void run(`/history/${item.id}/undo`, { expected_revision: history.data!.revision }).then((saved) => { if (saved) historyHeading.current?.focus(); }); }}>{t(item.undone ? "undone" : "undo")}</button></div>) : <p>{t("noHistory")}</p>)}</details>}
  </section>;
}
