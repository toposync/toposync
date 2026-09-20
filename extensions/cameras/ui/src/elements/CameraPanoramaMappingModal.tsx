import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isActiveModalFocus, resolveToposyncUrl } from "@toposync/plugin-api";
import type { CompositionElement, EditorToolSession, HostI18n, ToposyncHost, Viewport2DContext, NavigableViewportController } from "@toposync/plugin-api";

import { CameraPanoramaRequestError, aimCameraPanorama, createCameraPanorama, deleteCameraPanorama, fetchCameraPanoramaContext, fetchCameraPanoramaJob, restoreCameraPanorama, updateCameraPanorama } from "../api/camerasApi";
import { createUniqueId } from "../parsing";
import type { CameraPanoramaContext, CameraPanoramaJob, CameraPanoramaPoint } from "../types";
import { CameraSourcePanoramaSection } from "../settings/CameraSourcePanoramaSection";
import { SubModal } from "../ui/SubModal";
import { cameraPanoramaStyles } from "./cameraPanoramaStyles";
import { createPanoramaProjector } from "./panoramaProjection";
import type { PanoramaCoverageMask } from "./panoramaProjection";

type ImagePoint = { x: number; y: number };
type WorldPoint = { x: number; z: number };
type PendingPoint = { id: string; role: "fit" | "check"; panorama: ImagePoint | null; world: WorldPoint | null };
type PreviewCursor = { from: "panorama"; point: ImagePoint } | { from: "plan"; point: WorldPoint };
type PreviewDestination = { panel: "panorama"; point: ImagePoint } | { panel: "plan"; point: WorldPoint };
const MINIMUM_FIT_POINTS = 6;
const MINIMUM_CHECK_POINTS = 2;
function imagePoint(event: React.MouseEvent<HTMLElement>, image: HTMLImageElement | null): ImagePoint | null {
  if (!image?.complete || image.naturalWidth === 0) return null;
  const rectangle = image.getBoundingClientRect();
  const point = { x: (event.clientX - rectangle.left) / rectangle.width, y: (event.clientY - rectangle.top) / rectangle.height };
  return point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1 ? point : null;
}

function initialStep(job: CameraPanoramaJob | null): number {
  if (job?.active) return 3;
  if (!job?.panorama_url || job.state !== "ready") return 0;
  return job.permissions?.can_activate ? 3 : job.solution?.preview?.eligible && job.points.filter((point) => point.role === "fit").length >= MINIMUM_FIT_POINTS ? 2 : 1;
}

function samePoint(left: PendingPoint, right: PendingPoint | undefined): boolean {
  return Boolean(right && left.id === right.id && left.role === right.role && left.panorama?.x === right.panorama?.x && left.panorama?.y === right.panorama?.y && left.world?.x === right.world?.x && left.world?.z === right.world?.z);
}

function isDraftPoint(value: unknown, incomplete: boolean): value is PendingPoint {
  if (!value || typeof value !== "object") return false;
  const point = value as PendingPoint;
  const image = point.panorama;
  const world = point.world;
  return typeof point.id === "string" && point.id.length > 0 && point.id.length <= 100
    && (point.role === "fit" || point.role === "check")
    && (incomplete && image === null || Boolean(image && Number.isFinite(image.x) && Number.isFinite(image.y) && image.x >= 0 && image.x <= 1 && image.y >= 0 && image.y <= 1))
    && (incomplete && world === null || Boolean(world && Number.isFinite(world.x) && Number.isFinite(world.z)));
}

export function CameraPanoramaMappingModal({ open, onClose, onOpenSettings, onActivate, host, i18n, element, cameraId }: {
  open: boolean; onClose: () => void; host: ToposyncHost; i18n: HostI18n; element: CompositionElement; cameraId: string;
  onActivate: (job: CameraPanoramaJob) => void;
  onOpenSettings: (sourceId: string) => void;
}): React.ReactElement | null {
  const { t, locale } = i18n.useI18n();
  const text = useCallback((key: string, parameters?: Record<string, unknown>) => t(`ext.cameras.panorama.${key}`, parameters), [t]);
  const [context, setContext] = useState<CameraPanoramaContext | null>(null);
  const [job, setJob] = useState<CameraPanoramaJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [verifyingAim, setVerifyingAim] = useState(false);
  const [preparationRevision, setPreparationRevision] = useState(0);
  const [step, setStep] = useState(0);
  const [sourceId, setSourceId] = useState("");
  const [drafts, setDrafts] = useState<PendingPoint[]>([]);
  const [selectedPointId, setSelectedPointId] = useState<string | null>(null);
  const [unsavedPoints, setUnsavedPoints] = useState<CameraPanoramaPoint[] | null>(null);
  const [undo, setUndo] = useState<CameraPanoramaPoint[][]>([]);
  const [selectedCheck, setSelectedCheck] = useState<string | null>(null);
  const [checkImageReadyId, setCheckImageReadyId] = useState<string | null>(null);
  const [imageZoom, setImageZoom] = useState(1);
  const [planRotation, setPlanRotation] = useState<0 | 90 | 180 | 270>(0);
  const [planFit, setPlanFit] = useState<"camera" | "content">("content");
  const [keyboardImage, setKeyboardImage] = useState<ImagePoint | null>(null);
  const [keyboardWorld, setKeyboardWorld] = useState<WorldPoint | null>(null);
  const [aimTarget, setAimTarget] = useState<WorldPoint | null>(null);
  const [previewCursor, setPreviewCursor] = useState<PreviewCursor | null>(null);
  const [previewDestination, setPreviewDestination] = useState<PreviewDestination | null>(null);
  const [planPreviewCenter, setPlanPreviewCenter] = useState<WorldPoint | null>(null);
  const [coverage, setCoverage] = useState<{ url: string; mask: PanoramaCoverageMask } | null>(null);
  const [expandedView, setExpandedView] = useState<"panorama" | "plan" | null>(null);
  const [recoveryDraft, setRecoveryDraft] = useState<string | null>(null);
  const [browserDraftSaved, setBrowserDraftSaved] = useState(false);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const panoramaViewportRef = useRef<HTMLDivElement | null>(null);
  const panoramaNavigation = useRef<NavigableViewportController | null>(null);
  const [imageSize, setImageSize] = useState({ width: 1000, height: 500 });
  const panoramaCenterRef = useRef<ImagePoint>({ x: 0.5, y: 0.5 });
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const viewportRef = useRef<Viewport2DContext | null>(null);
  const jobRef = useRef(job);
  const operationRef = useRef(false);
  const cursorFrame = useRef(0);
  const cursorCandidate = useRef<PreviewCursor | null>(null);
  const draftKey = `toposync.panorama-point.${cameraId}.${element.id}`;
  const points = unsavedPoints ?? job?.points ?? [];
  const navigationPoints = [...points.map((point) => drafts.find((draft) => draft.id === point.id) ?? point), ...drafts.filter((draft) => !points.some((point) => point.id === draft.id))];
  const pending = step > 0 && step < 3 ? navigationPoints.find((point) => point.id === selectedPointId) ?? null : null;
  const changedDrafts = drafts.filter((draft) => !samePoint(draft, job?.points.find((point) => point.id === draft.id)));
  const selectedChanged = Boolean(pending && !samePoint(pending, job?.points.find((point) => point.id === pending.id)));
  const editingSupport = changedDrafts.some((draft) => job?.points.some((point) => point.id === draft.id));
  const fitCount = points.filter((point) => point.role === "fit").length;
  const checkPoints = points.filter((point) => point.role === "check");
  const checks = job?.checks.filter((check) => check.revision === job.revision) ?? [];
  const selectedCheckId = pending?.role === "check" && !selectedChanged ? pending.id : checkPoints.some((point) => point.id === selectedCheck) ? selectedCheck : null;
  const currentCheck = checks.find((check) => check.point_id === selectedCheckId);
  const successfulChecks = new Set(checks.filter((check) => check.result === "correct").map((check) => check.point_id));
  const captureRunning = job?.state === "capturing" || job?.state === "processing";
  const editingLocked = Boolean(loading || busy || captureRunning || job?.active || recoveryDraft);
  const canActivate = job?.permissions?.can_activate ?? (job?.state === "ready" && !job.physical_blockers?.length && job.solution?.quality.status === "ready" && fitCount >= MINIMUM_FIT_POINTS && checkPoints.length >= MINIMUM_CHECK_POINTS && checkPoints.every((point) => successfulChecks.has(point.id)));
  const selectedIndex = pending ? points.findIndex((point) => point.id === pending.id) : -1;
  const navigationIndex = navigationPoints.findIndex((point) => point.id === selectedPointId);
  const pointNumber = navigationIndex + 1;
  const availableSource = context?.sources.find((source) => source.id === sourceId);
  const previewContext = job?.solution?.preview?.context;
  const previewCurrent = Boolean(previewContext && previewContext.job_id === job?.id && previewContext.revision === job?.revision && previewContext.source_id === job?.source_id
    && previewContext.source_artifact_id === (job?.source_panorama?.id ?? null)
    && previewContext.source_artifact_revision === (job?.source_panorama?.compatible ? job.source_panorama.revision : null));
  const previewAllowed = previewCurrent && !loading && !busy && !unsavedPoints && !editingSupport && pending?.role !== "check" && !recoveryDraft;
  const projector = useMemo(() => previewAllowed && coverage?.url === job?.coverage_url ? createPanoramaProjector(job?.solution ?? null, coverage?.mask ?? null) : null, [previewAllowed, coverage, job?.solution, job?.coverage_url]);
  const ghostWorld = previewCursor?.from === "panorama" ? projector?.panoramaToWorld(previewCursor.point) ?? null : null;
  const ghostImage = previewCursor?.from === "plan" ? projector?.worldToPanorama(previewCursor.point) ?? null : null;

  function setPending(value: React.SetStateAction<PendingPoint | null>): void {
    const next = typeof value === "function" ? value(pending) : value;
    setDrafts((current) => next ? current.some((point) => point.id === next.id) ? current.map((point) => point.id === next.id ? next : point) : [...current, next] : current.filter((point) => point.id !== selectedPointId));
    setSelectedPointId(next?.id ?? null);
  }

  function previewAt(next: PreviewCursor | null): void {
    cursorCandidate.current = next;
    if (cursorFrame.current) return;
    cursorFrame.current = window.requestAnimationFrame(() => { cursorFrame.current = 0; setPreviewCursor(cursorCandidate.current); });
  }

  useEffect(() => {
    cursorCandidate.current = null; setPreviewCursor(null);
    setPreviewDestination(null);
  }, [job?.id, job?.revision, selectedPointId, previewAllowed]);

  useEffect(() => {
    if (ghostWorld) setPreviewDestination({ panel: "plan", point: ghostWorld });
    else if (ghostImage) setPreviewDestination({ panel: "panorama", point: ghostImage });
    else if (previewCursor || !projector) setPreviewDestination(null);
  }, [ghostWorld?.x, ghostWorld?.z, ghostImage?.x, ghostImage?.y, Boolean(previewCursor), projector]);

  useEffect(() => {
    if (!open || !job?.coverage_url) { setCoverage(null); return; }
    let cancelled = false;
    const url = job.coverage_url;
    const image = new Image();
    image.onload = () => {
      if (cancelled) return;
      try {
        if (!image.naturalWidth || !image.naturalHeight || image.naturalWidth * image.naturalHeight > 64_000_000) return;
        const canvas = document.createElement("canvas"); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context) return;
        context.drawImage(image, 0, 0);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        const data = new Uint8Array(canvas.width * canvas.height);
        for (let index = 0; index < data.length; index++) data[index] = pixels[index * 4 + 3] ? pixels[index * 4] : 0;
        setCoverage({ url, mask: { width: canvas.width, height: canvas.height, data } });
      } catch { setCoverage(null); }
    };
    image.onerror = () => { if (!cancelled) setCoverage(null); };
    image.src = resolveToposyncUrl(url);
    return () => { cancelled = true; };
  }, [open, job?.coverage_url]);

  useEffect(() => () => window.cancelAnimationFrame(cursorFrame.current), []);

  useEffect(() => { jobRef.current = job; }, [job]);

  const acceptJob = useCallback((next: CameraPanoramaJob | null) => {
    if (next && jobRef.current?.id === next.id) {
      if (jobRef.current.revision > next.revision) return;
      if (jobRef.current.revision === next.revision && Number(jobRef.current.updated_at) > Number(next.updated_at)) return;
    }
    jobRef.current = next;
    setJob(next);
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError(null);
    try {
      let response = await fetchCameraPanoramaContext(cameraId, element.id, signal);
      if (signal?.aborted) return;
      const invalidJob = response.blockers.some((blocker) => !["absolute_position_control_required", "optical_and_axis_profile_required"].includes(blocker));
      const source = response.sources.find((item) => item.id === response.job?.source_id && item.panorama?.compatible) ?? response.sources.find((item) => item.panorama?.compatible);
      // Resume an existing calibration even if another panorama has since been published.
      if ((!response.job || invalidJob || response.job.state !== "ready") && source?.panorama?.compatible) {
        const next = await createCameraPanorama(cameraId, { element_id: element.id, source_id: source.id, source_artifact_id: source.panorama.id, source_artifact_revision: source.panorama.revision, reuse_draft: true });
        if (signal?.aborted) return;
        response = { ...response, job: next, blockers: [] };
      }
      setVerifyingAim(false);
      setContext(response); acceptJob(response.job);
      setStep(response.blockers.some((blocker) => !["absolute_position_control_required", "optical_and_axis_profile_required"].includes(blocker)) ? 0 : initialStep(response.job));
      const selectedSource = response.sources.find((source) => source.id === response.job?.source_id) ?? response.sources.find((source) => source.panorama?.compatible) ?? response.sources[0];
      setSourceId(response.job?.source_id ?? selectedSource?.id ?? "");
      setSelectedCheck(response.job?.points.find((point) => point.role === "check")?.id ?? null);
      setDrafts([]); setSelectedPointId(null); setUnsavedPoints(null); setUndo([]); setRecoveryDraft(null);
      try {
        const stored = localStorage.getItem(draftKey);
        const cached = JSON.parse(stored ?? "null");
        if (response.job && cached?.jobId === response.job.id && cached?.revision === response.job.revision) {
          const candidate = isDraftPoint(cached.pending, true) ? cached.pending : null;
          const restored = Array.isArray(cached.drafts) && cached.drafts.length <= 64 && cached.drafts.every((point: unknown) => isDraftPoint(point, true)) ? cached.drafts as PendingPoint[] : candidate ? [candidate] : [];
          const changed = Array.isArray(cached.unsavedPoints) && cached.unsavedPoints.length <= 64 && cached.unsavedPoints.every((point: unknown) => isDraftPoint(point, false)) ? cached.unsavedPoints as CameraPanoramaPoint[] : null;
          const selectable = [...(changed ?? response.job?.points ?? []), ...restored];
          const selected = selectable.find((point) => point.id === cached.selectedPointId) ?? candidate ?? restored[0];
          setDrafts(restored); setSelectedPointId(selected?.id ?? null); setUnsavedPoints(changed);
          if (selected && initialStep(response.job) > 0 && (initialStep(response.job) < 3 || restored.length > 0 || changed) && !response.job?.active) setStep(selected.role === "check" ? 2 : 1);
          if (restored.length || changed) setNotice("restored");
        } else if (stored && (cached?.pending || cached?.drafts?.length || cached?.unsavedPoints)) setRecoveryDraft(stored);
      } catch { /* A damaged browser draft cannot replace the server record. */ }
    } catch (failure) {
      if (!signal?.aborted) setError(failure ?? "request_failed");
    } finally { if (!signal?.aborted) setLoading(false); }
  }, [acceptJob, cameraId, draftKey, element.id]);

  useEffect(() => {
    if (!open || !cameraId) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [open, cameraId, load, preparationRevision]);

  useEffect(() => {
    if (!open || !job || !captureRunning) return;
    const controller = new AbortController();
    let timer: number;
    const poll = async () => {
      try {
        const next = await fetchCameraPanoramaJob(cameraId, job.id, controller.signal);
        if (controller.signal.aborted) return;
        acceptJob(next); setError(null);
        if (next.state === "ready") { setStep(1); setNotice("panorama_ready"); }
      } catch (failure) {
        if (!controller.signal.aborted) setError("connection_lost");
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 1500);
    };
    timer = window.setTimeout(poll, 1000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [open, cameraId, job?.id, captureRunning, acceptJob]);

  useEffect(() => {
    if (!open || !job || loading || recoveryDraft) return;
    try {
      localStorage.setItem(draftKey, JSON.stringify({ jobId: job.id, revision: job.revision, selectedPointId, drafts, unsavedPoints }));
      setBrowserDraftSaved(true);
    } catch { setBrowserDraftSaved(false); if (drafts.length || unsavedPoints) setNotice("browser_draft_failed"); }
  }, [open, loading, recoveryDraft, draftKey, job?.id, job?.revision, selectedPointId, drafts, unsavedPoints]);

  useEffect(() => {
    if (!open || loading || recoveryDraft || step === 0 || step === 3 || !job || job.active || selectedPointId) return;
    const role = fitCount < MINIMUM_FIT_POINTS ? "fit" : "check";
    if (role === "check" && (checkPoints.length >= MINIMUM_CHECK_POINTS || !job.solution?.preview?.eligible)) {
      setSelectedPointId(job.points.find((point) => pointErrorForResume(point.id))?.id ?? job.points[0]?.id ?? null);
    } else setPending({ id: createUniqueId(), role, panorama: null, world: null });
    function pointErrorForResume(id: string): boolean { return Array.isArray(job?.solution?.quality.point_errors) && job.solution.quality.point_errors.some((point: { id?: string; inlier?: boolean }) => point.id === id && point.inlier === false); }
  }, [open, loading, recoveryDraft, step, job?.id, selectedPointId, fitCount, checkPoints.length]);

  const close = useCallback(() => {
    if (operationRef.current) { setNotice("wait_operation"); return; }
    onClose();
  }, [onClose]);
  const closeRef = useRef(close);
  closeRef.current = close;

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const panel = bodyRef.current?.closest<HTMLElement>("[role='dialog']");
    panel?.classList.add("cameraPanoramaPanel");
    const closeButton = panel?.querySelector<HTMLElement>("button");
    closeButton?.setAttribute("aria-label", t("core.actions.close"));
    closeButton?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (typeof isActiveModalFocus === "function" && !isActiveModalFocus(panel ?? null)) return;
      if (event.key === "Escape") {
        const target = event.target as HTMLElement;
        if (target.closest(".cameraPanoramaViewport")) return;
        event.preventDefault(); event.stopPropagation(); closeRef.current();
      }
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, [tabindex='0']")).filter((item) => item.getClientRects().length > 0);
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("keydown", onKey, true); panel?.classList.remove("cameraPanoramaPanel"); previous?.focus(); };
  }, [open]);

  async function operate(label: string, action: () => Promise<void>): Promise<void> {
    if (operationRef.current) return;
    operationRef.current = true; setBusy(label); setError(null); setNotice("");
    try { await action(); }
    catch (failure) {
      setError(failure ?? "request_failed");
      if (["checking", "aiming", "returning_camera"].includes(label) && jobRef.current) {
        try { acceptJob(await fetchCameraPanoramaJob(cameraId, jobRef.current.id)); } catch { /* Preserve the original operation error. */ }
      }
    }
    finally { operationRef.current = false; setBusy(null); }
  }

  async function mutate(action: Parameters<typeof updateCameraPanorama>[2], extra: Omit<Parameters<typeof updateCameraPanorama>[3], "revision"> = {}): Promise<CameraPanoramaJob> {
    const current = jobRef.current;
    if (!current) throw new CameraPanoramaRequestError("panorama_request_failed", "", 400);
    const next = await updateCameraPanorama(cameraId, current.id, action, { revision: current.revision, ...extra });
    acceptJob(next);
    return next;
  }

  async function savePoints(next: CameraPanoramaPoint[], remember = true): Promise<boolean> {
    if (operationRef.current) return false;
    const previous = jobRef.current?.points ?? [];
    let saved = false;
    setUnsavedPoints(next);
    await operate("saving", async () => {
      await mutate("points", { points: next });
      if (remember) setUndo((history) => [...history.slice(-19), previous]);
      setUnsavedPoints(null);
      setDrafts((current) => current.filter((draft) => !samePoint(draft, next.find((point) => point.id === draft.id))));
      setNotice("place_saved");
      saved = true;
    });
    return saved;
  }

  function startPoint(role: "fit" | "check", existing?: CameraPanoramaPoint): void {
    if (editingLocked || recoveryDraft || (!existing && navigationPoints.length >= 64)) return;
    setStep(role === "check" ? 2 : 1);
    if (existing) setSelectedPointId(existing.id);
    else setPending({ id: createUniqueId(), role, panorama: null, world: null });
    if (role === "check") setSelectedCheck(existing?.id ?? null);
    setKeyboardImage(null); setKeyboardWorld(null); setBrowserDraftSaved(false); setNotice(""); setError(null);
  }

  function selectPoint(point: PendingPoint): void {
    if (loading || busy || recoveryDraft) return;
    setSelectedPointId(point.id); setStep(point.role === "check" ? 2 : 1);
    setSelectedCheck(point.role === "check" ? point.id : null);
    setKeyboardImage(null); setKeyboardWorld(null); previewAt(null);
  }

  async function saveCurrentPoint(): Promise<void> {
    if (!pending?.panorama || !pending.world) return;
    const complete = pending as CameraPanoramaPoint;
    const next = selectedIndex >= 0 ? points.map((point) => point.id === complete.id ? complete : point) : [...points, complete];
    if (!await savePoints(next)) return;
    const following = navigationPoints[navigationIndex + 1];
    if (following) selectPoint(following);
    else if (next.filter((point) => point.role === complete.role).length < (complete.role === "fit" ? MINIMUM_FIT_POINTS : MINIMUM_CHECK_POINTS)) {
      setPending({ id: createUniqueId(), role: complete.role, panorama: null, world: null });
    } else if (complete.role === "fit" && jobRef.current?.solution?.preview?.eligible && next.filter((point) => point.role === "check").length < MINIMUM_CHECK_POINTS) {
      setStep(2); setPending({ id: createUniqueId(), role: "check", panorama: null, world: null });
    } else if (jobRef.current?.permissions?.can_activate) { setStep(3); setSelectedPointId(null); }
    else { setSelectedPointId(complete.id); setSelectedCheck(complete.role === "check" ? complete.id : null); }
  }

  async function reloadSavedRevision(): Promise<void> {
    if (job && (changedDrafts.length || unsavedPoints)) {
      try { localStorage.setItem(draftKey, JSON.stringify({ jobId: job.id, revision: job.revision, selectedPointId, drafts, unsavedPoints })); }
      catch { setError("browser_draft_failed"); return; }
    }
    await load();
  }

  function cancelCurrentChanges(): void {
    if (!pending) return;
    const saved = job?.points.find((point) => point.id === pending.id);
    const retained = points.flatMap((point) => point.id === pending.id ? saved ? [saved] : [] : [point]);
    setUnsavedPoints((current) => current && (retained.length !== job?.points.length || retained.some((point, index) => !samePoint(point, job?.points[index]))) ? retained : null);
    setDrafts((current) => current.filter((point) => point.id !== pending.id));
    setSelectedPointId(saved?.id ?? retained[0]?.id ?? drafts.find((point) => point.id !== pending.id)?.id ?? null);
    setKeyboardImage(null); setKeyboardWorld(null);
  }

  function placeWorld(world: WorldPoint): void {
    if (busy) return;
    if (step === 3 && job?.active) { setAimTarget(world); return; }
    if (!pending || editingLocked || recoveryDraft) return;
    setPending((value) => value ? { ...value, world } : null);
  }

  const overlayData = useRef({ points: navigationPoints, pending, placeWorld, selectPoint, previewAt, editingLocked, step, active: job?.active, keyboardWorld, aimTarget, ghostWorld, selectedChanged, polygon: job?.solution?.support_polygon ?? [] });
  overlayData.current = { points: navigationPoints, pending, placeWorld, selectPoint, previewAt, editingLocked, step, active: job?.active, keyboardWorld, aimTarget, ghostWorld, selectedChanged, polygon: job?.solution?.support_polygon ?? [] };
  const session = useMemo<EditorToolSession>(() => ({
    // The existing navigate mode forwards clicks after rejecting pan gestures.
    onPointerEvent: (event) => {
      const current = overlayData.current;
      if (event.kind === "move" && !event.buttons) current.previewAt({ from: "plan", point: event.world });
      if (event.kind === "cancel") current.previewAt(null);
      if (event.kind !== "down" || event.button !== 0) return;
      const viewport = viewportRef.current;
      const selected = current.points.find((point) => {
        if (!point.world || !viewport || point.id === current.pending?.id) return false;
        const screen = viewport.worldToScreen(point.world);
        return Math.hypot(screen.x - event.screen.x, screen.y - event.screen.y) <= 22;
      });
      if (selected && !current.active) { current.selectPoint(selected); return; }
      if (event.pointerType === "touch") { setKeyboardWorld(event.world); current.previewAt({ from: "plan", point: event.world }); }
      else current.placeWorld(event.world);
    },
    renderOverlay2D: ({ ctx, viewport }) => {
      viewportRef.current = viewport;
      const current = overlayData.current;
      if (current.ghostWorld) viewport.canvas.dataset.panoramaGhostWorld = JSON.stringify([current.ghostWorld.x, current.ghostWorld.z]);
      else delete viewport.canvas.dataset.panoramaGhostWorld;
      const tokens = getComputedStyle(viewport.canvas);
      const primaryColor = tokens.getPropertyValue("--color-text-primary").trim();
      const surfaceColor = tokens.getPropertyValue("--color-surface-solid").trim();
      if (current.polygon.length >= 3) {
        ctx.save(); ctx.fillStyle = tokens.getPropertyValue("--color-accent-background-soft").trim(); ctx.strokeStyle = tokens.getPropertyValue("--color-accent-teal").trim(); ctx.lineWidth = 2; ctx.setLineDash([5, 4]); ctx.beginPath();
        current.polygon.forEach((point, index) => { const screen = viewport.worldToScreen({ x: point[0], z: point[1] }); if (index) ctx.lineTo(screen.x, screen.y); else ctx.moveTo(screen.x, screen.y); });
        ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.restore();
      }
      const draw = (point: WorldPoint, label: string, check: boolean, pendingMarker = false, selected = false) => {
        const screen = viewport.worldToScreen(point);
        ctx.save(); ctx.fillStyle = primaryColor; ctx.strokeStyle = surfaceColor; ctx.lineWidth = 2;
        if (pendingMarker) ctx.setLineDash([3, 2]);
        ctx.beginPath(); if (check) ctx.roundRect(screen.x - 13, screen.y - 13, 26, 26, 4); else ctx.arc(screen.x, screen.y, 13, 0, Math.PI * 2);
        ctx.fill(); ctx.stroke(); ctx.fillStyle = surfaceColor; ctx.font = "bold 12px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(label, screen.x, screen.y); if (selected) { ctx.strokeStyle = tokens.getPropertyValue("--color-accent-teal").trim(); ctx.beginPath(); ctx.arc(screen.x, screen.y, 17, 0, Math.PI * 2); ctx.stroke(); } ctx.restore();
      };
      current.points.forEach((point, index) => { if (point.world && point.id !== current.pending?.id) draw(point.world, String(index + 1), point.role === "check"); });
      const pendingIndex = current.points.findIndex((point) => point.id === current.pending?.id);
      if (current.pending?.world) draw(current.pending.world, String(pendingIndex >= 0 ? pendingIndex + 1 : current.points.length + 1), current.pending.role === "check", current.selectedChanged, true);
      if (current.keyboardWorld) draw(current.keyboardWorld, "+", false, true);
      if (current.aimTarget) draw(current.aimTarget, "+", false);
      if (current.ghostWorld) {
        const screen = viewport.worldToScreen(current.ghostWorld);
        ctx.save(); ctx.strokeStyle = surfaceColor; ctx.lineWidth = 4; ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.arc(screen.x, screen.y, 12, 0, Math.PI * 2); ctx.stroke();
        ctx.strokeStyle = primaryColor; ctx.lineWidth = 2; ctx.stroke(); ctx.restore();
      }
    },
    getCursor: () => overlayData.current.pending || overlayData.current.step === 3 ? "crosshair" : "grab",
  }), [points, drafts, pending, editingLocked, step, job?.active, keyboardWorld, aimTarget, ghostWorld?.x, ghostWorld?.z, job?.solution]);

  function onPlanKey(event: React.KeyboardEvent<HTMLElement>): void {
    if (event.target !== event.currentTarget || busy) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setKeyboardWorld(null); previewAt(null); return; }
    const viewport = viewportRef.current;
    if (!viewport) return;
    const current = keyboardWorld ?? pending?.world ?? viewport.screenToWorld({ x: viewport.width / 2, y: viewport.height / 2 });
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); placeWorld(current); setKeyboardWorld(null); return; }
    const delta = event.shiftKey ? 2 : 10;
    const direction = { ArrowLeft: [-delta, 0], ArrowRight: [delta, 0], ArrowUp: [0, -delta], ArrowDown: [0, delta] }[event.key];
    if (direction) { event.preventDefault(); const screen = viewport.worldToScreen(current); const point = viewport.screenToWorld({ x: screen.x + direction[0], y: screen.y + direction[1] }); setKeyboardWorld(point); previewAt({ from: "plan", point }); }
  }

  function onImageKey(event: React.KeyboardEvent<HTMLElement>): void {
    if (event.target !== event.currentTarget || busy || !imageRef.current?.naturalWidth) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setKeyboardImage(null); previewAt(null); return; }
    const origin = keyboardImage ?? pending?.panorama ?? panoramaCenterRef.current;
    const current = { x: Math.max(0, Math.min(1, origin.x)), y: Math.max(0, Math.min(1, origin.y)) };
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); if (pending && !editingLocked) setPending({ ...pending, panorama: current }); setKeyboardImage(null); return; }
    const delta = event.shiftKey ? 0.001 : 0.01;
    const direction = { ArrowLeft: [-delta, 0], ArrowRight: [delta, 0], ArrowUp: [0, -delta], ArrowDown: [0, delta] }[event.key];
    if (direction) { event.preventDefault(); const point = { x: Math.max(0, Math.min(1, current.x + direction[0])), y: Math.max(0, Math.min(1, current.y + direction[1])) }; setKeyboardImage(point); previewAt({ from: "panorama", point }); }
  }

  function onImageClick(_point: ImagePoint, event: React.PointerEvent<HTMLDivElement>): void {
    const point = imagePoint(event, imageRef.current);
    if (!point) return;
    if (event.pointerType === "touch") { setKeyboardImage(point); previewAt({ from: "panorama", point }); }
    else if (pending && !editingLocked && !recoveryDraft) { setPending({ ...pending, panorama: point }); setKeyboardImage(null); }
  }

  async function beginMapping(review = false): Promise<void> {
    const artifact = review && job?.source_panorama?.compatible ? job.source_panorama : availableSource?.panorama;
    if (!artifact?.compatible) { if (review) setStep(0); return; }
    await operate("loading", async () => {
      const next = await createCameraPanorama(cameraId, { element_id: element.id, source_id: sourceId, source_artifact_id: artifact.id, source_artifact_revision: artifact.revision, reuse_draft: true, ...(review && job?.source_panorama ? { review_job_id: job.id, review_revision: job.revision } : {}) });
      acceptJob(next); setDrafts([]); setUnsavedPoints(null); setSelectedPointId(next.points[0]?.id ?? null); setStep(1);
    });
  }

  function centerPanoramaAt(point: ImagePoint): void {
    panoramaNavigation.current?.centerOn({ x: point.x * imageSize.width, y: point.y * imageSize.height });
  }

  function changeImageZoom(next: number): void {
    panoramaNavigation.current?.zoomBy(next / imageZoom);
  }

  function previewOutsideViewport(): boolean {
    if (!previewDestination) return false;
    if (previewDestination.panel === "plan") {
      const viewport = viewportRef.current;
      if (!viewport || expandedView === "panorama") return true;
      const point = viewport.worldToScreen(previewDestination.point);
      return point.x < 0 || point.y < 0 || point.x > viewport.width || point.y > viewport.height;
    }
    const image = imageRef.current?.getBoundingClientRect();
    const viewport = panoramaViewportRef.current?.getBoundingClientRect();
    if (!image || !viewport || expandedView === "plan") return true;
    const x = image.left + previewDestination.point.x * image.width;
    const y = image.top + previewDestination.point.y * image.height;
    return x < viewport.left || x > viewport.right || y < viewport.top || y > viewport.bottom;
  }

  function revealPreviewDestination(): void {
    if (!previewDestination) return;
    setExpandedView(null);
    if (previewDestination.panel === "panorama") centerPanoramaAt(previewDestination.point);
    else setPlanPreviewCenter(previewDestination.point);
  }

  const capturedBounds = job?.coverage_bounds;
  const initialImageBounds = capturedBounds ? {
    x: (capturedBounds.min_x - (capturedBounds.max_x - capturedBounds.min_x) * 0.05) * imageSize.width,
    y: (capturedBounds.min_y - (capturedBounds.max_y - capturedBounds.min_y) * 0.05) * imageSize.height,
    width: (capturedBounds.max_x - capturedBounds.min_x) * imageSize.width * 1.1,
    height: (capturedBounds.max_y - capturedBounds.min_y) * imageSize.height * 1.1,
  } : undefined;

  if (!open) return null;
  const blockers = context?.blockers ?? [];
  const outdated = blockers.some((blocker) => !["absolute_position_control_required", "optical_and_axis_profile_required"].includes(blocker));
  const instruction = step === 0 ? text(captureRunning ? "preparing_title" : "prepare_title")
    : step === 3 ? text(job?.active ? "active_title" : "finish_title")
    : pending ? text(pending.panorama && pending.world ? "confirm_pair" : pending.panorama ? "choose_plan" : pending.world ? "choose_panorama" : "choose_either", { number: pointNumber })
    : text("link_title");
  const dirty = Boolean(changedDrafts.length || unsavedPoints);
  const physicalBlocked = job?.permissions ? !job.permissions.can_verify_aim : Boolean(job?.physical_blockers?.length);
  const checkLens = currentCheck?.evidence?.lens ?? job?.profile?.lens;
  const physicalOperation = busy === "checking" || busy === "aiming" || busy === "returning_camera";
  const failure = error ?? job?.error ?? (!physicalOperation && job?.navigation?.phase === "failed" && job.navigation.error_code ? { code: job.navigation.error_code } : null);
  const failureKey = typeof failure === "string" ? failure : failure && typeof failure === "object" && "code" in failure && typeof failure.code === "string" ? `error_${failure.code}` : "request_failed";
  const failureDetails = failure && typeof failure === "object" && "message" in failure && typeof failure.message === "string" ? failure.message : "";
  const previewStatus = !projector ? "preview_unavailable" : previewCursor && !ghostImage && !ghostWorld ? "preview_outside" : job?.solution?.preview.status === "provisional" ? "preview_provisional" : "preview_available";
  const pointError = (id: string) => job?.solution?.quality.point_errors instanceof Array && job.solution.quality.point_errors.some((entry: { id?: string; inlier?: boolean; error_radians?: number }) => entry.id === id && (entry.inlier === false || Number(entry.error_radians) > Number(job.solution?.quality.angular_threshold_radians)));

  return <SubModal open={open} onClose={close} title={text("title", { camera: element.name || cameraId })} panelStyle={{ width: "min(1240px, calc(100vw - 24px))", maxHeight: "calc(100dvh - 24px)" }} bodyStyle={{ overflowY: "auto" }}>
    <style>{cameraPanoramaStyles}</style>
    <div className="cameraPanoramaMapping" ref={bodyRef} data-testid="camera-panorama-mapping" onKeyDown={(event) => event.stopPropagation()} onKeyUp={(event) => event.stopPropagation()}>
      {step > 0 && !job?.active ? <details><summary>{text("review_points")}</summary><nav aria-label={text("point_navigation")} className="cameraPanoramaPointNavigation">
        <ol className="cameraPanoramaPairs">{navigationPoints.map((point, index) => {
          const changed = !samePoint(point, job?.points.find((saved) => saved.id === point.id));
          return <li key={point.id} className={`cameraPanoramaPair ${point.id === selectedPointId && step < 3 ? "isSelected" : ""}`}>
            <button type="button" disabled={Boolean(busy || recoveryDraft)} aria-current={point.id === selectedPointId && step < 3 ? "step" : undefined} onClick={() => selectPoint(point)} aria-label={text(point.role === "check" ? "check_place" : "edit_place", { number: index + 1 })}>
              {point.role === "check" ? <span className="cameraPanoramaPointRole">{text("point_check")}</span> : null}
              <span>{index + 1}</span><span aria-label={text(changed ? "point_incomplete" : pointError(point.id) ? "point_review" : "saved")}>{changed ? "◌" : pointError(point.id) ? "!" : "✓"}</span>
            </button>
          </li>;
        })}</ol>
        <button type="button" className="chipButton" disabled={editingLocked || Boolean(recoveryDraft) || navigationPoints.length >= 64} onClick={() => startPoint("fit")}>{text("add_point")}</button>
<div className="cameraPanoramaActions"><button className="chipButton" disabled={navigationIndex <= 0 || Boolean(busy || recoveryDraft)} onClick={() => selectPoint(navigationPoints[navigationIndex - 1])}>{text("previous_point")}</button><button className="chipButton" disabled={navigationIndex < 0 || navigationIndex >= navigationPoints.length - 1 || Boolean(busy || recoveryDraft)} onClick={() => selectPoint(navigationPoints[navigationIndex + 1])}>{text("next_point")}</button></div>
      </nav></details> : null}
      <div className="cameraPanoramaInstruction">
        <div><h2>{instruction}</h2><p className="cardMeta">{step === 0 ? text(captureRunning ? "prepare_help" : "saved_panorama_visual_help") : text(step === 3 ? job?.active ? "active_help" : "finish_help" : pending?.role === "check" ? "holdout_help" : "pair_help")}</p></div>
        <span className="cameraPanoramaSaved" role="status" aria-live="polite">{loading ? text("loading") : busy ? text(busy) : unsavedPoints ? text("save_pending") : changedDrafts.length ? text(browserDraftSaved ? "pair_draft" : "pair_unsaved") : job ? text("saved") : ""}</span>
      </div>
      {physicalOperation ? <button className="chipButton" disabled={stopping} onClick={() => {
        if (!jobRef.current || stopping) return;
        setStopping(true);
        void updateCameraPanorama(cameraId, jobRef.current.id, "cancel", { revision: jobRef.current.revision }).then(acceptJob).catch(setError).finally(() => setStopping(false));
      }}>{text(stopping ? "cancelling" : "stop_camera")}</button> : null}
      {job?.navigation?.can_return ? <button className="chipButton" disabled={Boolean(busy)} onClick={() => void operate("returning_camera", async () => { await mutate("return-framing"); setNotice("camera_framing_restored"); })}>{text("return_camera_framing")}</button> : null}
      {failure ? <div className="cameraPanoramaError" role="alert"><span>{t(`ext.cameras.panorama.${failureKey}`, {}, text("request_failed"))}</span>{failureDetails ? <details><summary>{text("details")}</summary><p>{failureDetails}</p></details> : null}{unsavedPoints ? <button className="chipButton" disabled={Boolean(busy)} onClick={() => { if (pending?.panorama && pending.world) void saveCurrentPoint(); else void savePoints(unsavedPoints); }}>{text("retry_save")}</button> : null}{loading || captureRunning ? null : <button className="chipButton" disabled={Boolean(busy)} onClick={() => void reloadSavedRevision()}>{text("reload")}</button>}</div> : null}
      {recoveryDraft ? <div className="cameraPanoramaError" role="alert"><span>{text("draft_revision_changed")}</span><button className="chipButton" onClick={() => {
        const url = URL.createObjectURL(new Blob([recoveryDraft], { type: "application/json" }));
        const link = document.createElement("a"); link.href = url; link.download = "toposync-point-draft.json"; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }}>{text("download_draft")}</button><button className="chipButton" onClick={() => { try { localStorage.setItem(`${draftKey}.previous`, recoveryDraft); setRecoveryDraft(null); setNotice("draft_preserved"); } catch { setError("browser_draft_failed"); } }}>{text("continue_saved_revision")}</button></div> : null}
      {job?.stop_confirmed === false ? <div className="cameraPanoramaError" role="alert">{text("stop_unconfirmed")}</div> : null}
      {notice && notice !== "place_saved" ? <p className="cardMeta" role="status" aria-live="polite">{text(notice)}</p> : null}
      {loading ? <div className="cameraPanoramaEmpty"><span>{text("loading")}</span></div> : step === 0 ? <>
        {captureRunning ? <section className="card"><div className="cardBody cameraPanoramaMapping"><strong>{text(job?.state === "processing" ? "processing" : "capture_progress", { captured: job?.progress.captured ?? 0, total: job?.progress.total ?? 0 })}</strong><progress className="cameraPanoramaProgress" max={job?.progress.total || 1} value={job?.state === "processing" ? undefined : job?.progress.captured ?? 0} /><p className="cardMeta">{text("capture_background")}</p><button className="chipButton" disabled={Boolean(busy)} onClick={() => void operate("cancelling", async () => { await mutate("cancel"); })}>{text("stop_capture")}</button></div></section> : <section className="card"><div className="cardBody cameraPanoramaMapping">
          {(context?.sources.length ?? 0) > 1 ? <label className="label">{text("source")}<select className="input" value={sourceId} onChange={(event) => setSourceId(event.target.value)}>{context?.sources.map((source) => <option key={source.id} value={source.id}>{source.label}</option>)}</select></label> : null}
          {availableSource?.panorama?.compatible ? <button className="primaryButton" disabled={Boolean(busy || recoveryDraft)} onClick={() => void beginMapping()}>{text("continue_calibration")}</button> : availableSource ? <>
            {availableSource.panorama ? <p role="status">{text("source_geometry_unavailable")}</p> : null}
            <CameraSourcePanoramaSection key={sourceId} ui={host.ui} cameraId={cameraId} sourceId={sourceId} cameraName={element.name || cameraId} sourceName={availableSource.label} enabled persisted i18n={i18n} onActiveArtifactChange={(artifact, changed) => { if (changed || availableSource.panorama?.id !== artifact.id) setPreparationRevision((value) => value + 1); }} />
          </> : <p role="status">{text("error_video_source_required")}</p>}
          {outdated ? <p className="cameraPanoramaError">{text("setup_required")}</p> : null}
        </div></section>}
      </> : <>
        {step === 3 && !job?.active ? <div className="cameraPanoramaInstruction"><strong>{text("checked_summary", { count: checkPoints.length })}</strong><p className="cardMeta">{text("support_help")}</p></div> : null}
        <div hidden={step === 3 && !verifyingAim} className={`cameraPanoramaViews ${expandedView ? "hasExpandedView" : ""}`} style={step === 3 && !verifyingAim ? { display: "none" } : undefined}>
          <section className={`cameraPanoramaView ${pending && !pending.panorama ? "isAwaiting" : ""}`} aria-labelledby="panorama-image-title" hidden={expandedView === "plan"}>
            <div className="cameraPanoramaViewHeader"><h3 id="panorama-image-title">{text("panorama")}</h3><div className="cameraPanoramaActions"><button className="iconButton" aria-label={text("image_zoom_out")} disabled={imageZoom <= 0.5} onClick={() => changeImageZoom(imageZoom / 1.25)}>−</button><span className="cardMeta">{Math.round(imageZoom * 100)}%</span><button className="iconButton" aria-label={text("image_zoom_in")} disabled={imageZoom >= 16} onClick={() => changeImageZoom(imageZoom * 1.25)}>+</button><button className="iconButton" aria-label={t("ext.cameras.source_panorama.fit_view")} onClick={() => panoramaNavigation.current?.fit()}>⊡</button><button className="iconButton" aria-label={text(expandedView ? "show_both_views" : "expand_panorama")} onClick={() => setExpandedView((value) => value ? null : "panorama")}><i className={`fa-solid fa-${expandedView ? "compress" : "expand"}`} aria-hidden="true" /></button></div></div>
            <host.ui.NavigableViewport className="cameraPanoramaViewport" viewportRef={panoramaViewportRef}
              contentSize={imageSize} contentKey={job?.panorama_url ?? undefined} initialBounds={initialImageBounds} controllerRef={panoramaNavigation}
              label={text("image_keyboard")} onKeyDown={onImageKey} onNavigationStart={() => previewAt(null)}
              onViewChange={(state) => { setImageZoom(state.zoom); panoramaCenterRef.current = { x: state.center.x / imageSize.width, y: state.center.y / imageSize.height }; }}
              onContentClick={onImageClick}
              onContentPointerMove={(_point, event) => { const point = imagePoint(event, imageRef.current); previewAt(point ? { from: "panorama", point } : null); }}
              onPointerLeave={() => previewAt(null)} onBlur={() => previewAt(null)}>
              <div className={`cameraPanoramaImage ${job?.coverage_bounds ? "hasCoverage" : ""}`} style={{ width: "100%", height: "100%" }}><div className="cameraPanoramaImageContent">
                {job?.panorama_url ? <img ref={imageRef} src={resolveToposyncUrl(job.panorama_url)} alt={text("panorama_alt")} draggable={false} onLoad={(event) => { const image = event.currentTarget; setImageSize({ width: image.naturalWidth, height: image.naturalHeight }); }} onError={() => setError("image_failed")} /> : <div className="cameraPanoramaEmpty">{text("image_failed")}</div>}
                {navigationPoints.map((point, index) => !point.panorama || point.id === pending?.id ? null : <button key={point.id} className={`cameraPanoramaMarker ${point.role === "check" ? "isCheck" : ""}`} style={{ left: `${point.panorama.x * 100}%`, top: `${point.panorama.y * 100}%` }} onClick={(event) => { event.stopPropagation(); selectPoint(point); }} disabled={editingLocked} aria-label={text("edit_place", { number: index + 1 })}>{index + 1}</button>)}
                {pending?.panorama ? <span className={`cameraPanoramaMarker ${selectedChanged ? "isPending" : "isSelected"} ${pending.role === "check" ? "isCheck" : ""}`} style={{ left: `${pending.panorama.x * 100}%`, top: `${pending.panorama.y * 100}%` }}>{pointNumber}</span> : null}
                {keyboardImage ? <span className="cameraPanoramaMarker isPending" style={{ left: `${keyboardImage.x * 100}%`, top: `${keyboardImage.y * 100}%` }}>+</span> : null}
                {ghostImage ? <span data-testid="panorama-ghost" className="cameraPanoramaGhost" aria-hidden="true" style={{ left: `${ghostImage.x * 100}%`, top: `${ghostImage.y * 100}%` }} /> : null}
              </div></div>
            </host.ui.NavigableViewport>
            {keyboardImage && pending && !editingLocked ? <button className="chipButton" onClick={() => { setPending({ ...pending, panorama: keyboardImage }); setKeyboardImage(null); }}>{text("confirm_image_mark")}</button> : null}
          </section>
          <section className={`cameraPanoramaView ${pending && !pending.world ? "isAwaiting" : ""}`} aria-labelledby="panorama-plan-title" hidden={expandedView === "panorama"}>
            <div className="cameraPanoramaViewHeader"><h3 id="panorama-plan-title">{text("composition")}</h3><div className="cameraPanoramaActions"><button className="iconButton" aria-label={text("rotate_plan")} onClick={() => { setPlanRotation((value) => ((value + 90) % 360) as 0 | 90 | 180 | 270); previewAt(null); }}>↻</button><button className="chipButton" onClick={() => { setPlanPreviewCenter(null); setPlanFit((value) => value === "content" ? "camera" : "content"); }}>{text(planFit === "content" ? "focus_camera" : "whole_plan")}</button><button className="iconButton" aria-label={text(expandedView ? "show_both_views" : "expand_plan")} onClick={() => setExpandedView((value) => value ? null : "plan")}><i className={`fa-solid fa-${expandedView ? "compress" : "expand"}`} aria-hidden="true" /></button></div></div>
            <div className="cameraPanoramaViewport" tabIndex={0} role="group" aria-label={text("plan_keyboard")} onKeyDown={onPlanKey} onPointerDownCapture={() => previewAt(null)} onWheelCapture={() => previewAt(null)} onPointerLeave={() => previewAt(null)} onBlur={() => previewAt(null)}><host.ui.Viewport2DReplica initialFit={!planPreviewCenter && planFit === "content" ? "content" : undefined} initialCenter={planPreviewCenter ?? (planFit === "camera" ? { x: element.position.x, z: element.position.z } : undefined)} initialScale={planFit === "camera" ? 34 : undefined} minScale={4} interactionMode="navigate" displayRotationDegrees={planRotation} session={session} style={{ width: "100%", height: "100%" }} /></div>
            {keyboardWorld && pending && !editingLocked ? <button className="chipButton" onClick={() => { placeWorld(keyboardWorld); setKeyboardWorld(null); }}>{text("confirm_plan_mark")}</button> : null}
          </section>
        </div>
        <div className="cameraPanoramaActions" hidden={step === 3} style={step === 3 ? { display: "none" } : undefined}><span className="cardMeta">{text(fitCount === 1 ? "place_count" : "places_count", { count: fitCount.toLocaleString(locale) })} · {text(checkPoints.length === 1 ? "check_count" : "checks_count", { count: checkPoints.length.toLocaleString(locale) })}</span><span className="cardMeta" data-testid="panorama-preview-status" role="status">{pending?.role === "check" ? text("preview_hidden_check") : text(previewStatus)}</span>{previewOutsideViewport() ? <button className="chipButton" onClick={revealPreviewDestination}>{text("show_prediction")}</button> : null}</div>
        {step < 3 ? <>
          {job?.solution && job.solution.quality.status !== "ready" ? <div className="cardMeta" role="status">{text(fitCount < MINIMUM_FIT_POINTS ? "more_places" : job.solution.quality.reasons?.every((reason) => reason === "at_least_two_independent_check_points_required") ? "add_holdouts_help" : "review_geometry")}</div> : null}
          {job?.source_panorama ? <p className="cardMeta" role="status">{text(job.permissions?.map_validated ? "visual_map_ready" : "visual_map_help")}</p> : physicalBlocked ? <p className="cardMeta" role="status">{text("physical_geometry_unavailable")}</p> : null}
          {(verifyingAim || !job?.source_panorama) && pending?.role === "check" && selectedCheckId && !selectedChanged && !physicalBlocked ? <section className="card"><div className="cardBody cameraPanoramaMapping">
            <strong>{text("check_place", { number: pointNumber })}</strong><p className="cardMeta">{text("physical_check")}</p>
            <button className={!currentCheck && !dirty && job?.solution?.quality.status === "ready" ? "primaryButton" : "chipButton"} disabled={Boolean(busy || dirty) || job?.solution?.quality.status !== "ready"} onClick={() => void operate("checking", async () => { await mutate("check", { point_id: selectedCheckId }); })}>{text("point_camera")}</button>
            {currentCheck?.image_url && checkLens ? <><div className="cameraPanoramaTarget"><img className="cameraPanoramaCheckImage" src={resolveToposyncUrl(currentCheck.image_url)} alt={text("check_image_alt")} onLoad={() => setCheckImageReadyId(currentCheck.id)} onError={() => { setCheckImageReadyId(null); setError("image_failed"); }} /><span className="cameraPanoramaTargetCross" aria-hidden="true" style={{ left: `${checkLens.cx / checkLens.width * 100}%`, top: `${checkLens.cy / checkLens.height * 100}%` }} /></div><p>{text("check_question")}</p><div className="cameraPanoramaActions">{(["correct", "offset", "unverifiable"] as const).map((result) => <button key={result} className={result === "correct" && currentCheck.result !== "correct" && !dirty ? "primaryButton" : "chipButton"} disabled={Boolean(busy || dirty) || checkImageReadyId !== currentCheck.id} onClick={() => void operate("saving", async () => { await mutate("check-result", { point_id: currentCheck.point_id, check_id: currentCheck.id, result }); setNotice(`result_${result}`); })}>{text(`answer_${result}`)}</button>)}</div></> : null}
          </div></section> : null}
          <div className="cameraPanoramaFooter">

            <div className="cameraPanoramaActions">
              {pending && selectedChanged ? <button className="primaryButton" disabled={!pending.panorama || !pending.world || Boolean(busy || recoveryDraft)} onClick={() => void saveCurrentPoint()}>{text("confirm_place")}</button> : fitCount >= MINIMUM_FIT_POINTS && checkPoints.length < MINIMUM_CHECK_POINTS ? <button className="primaryButton" disabled={editingLocked || Boolean(recoveryDraft) || !job?.solution?.preview?.eligible} onClick={() => startPoint("check")}>{text("add_check")}</button> : null}
              {checkPoints.length >= MINIMUM_CHECK_POINTS && !selectedChanged ? <button className={canActivate && !dirty ? "primaryButton" : "chipButton"} disabled={!canActivate || dirty || Boolean(busy || recoveryDraft)} onClick={() => setStep(3)}>{text("continue_finish")}</button> : null}
            </div>
          </div>
          <details><summary>{text("point_help")}</summary><p className="cardMeta">{text("point_help_details")}</p><p className="cardMeta">{text("keyboard_help")}</p><div className="cameraPanoramaActions">
            {selectedIndex >= 0 && pending ? <button className="chipButton" disabled={editingLocked} aria-label={text("remove_place", { number: pointNumber })} onClick={() => { const id = pending.id; void savePoints(points.filter((point) => point.id !== id)).then((saved) => { if (saved) { setDrafts((current) => current.filter((point) => point.id !== id)); setSelectedPointId(navigationPoints.find((point) => point.id !== id)?.id ?? null); } }); }}>{text("remove_place", { number: pointNumber })}</button> : null}
            {pending && selectedChanged ? <button className="chipButton" disabled={Boolean(busy)} onClick={cancelCurrentChanges}>{text("cancel_pair")}</button> : null}
            {undo.length ? <button className="chipButton" disabled={editingLocked} onClick={() => { const previous = undo[undo.length - 1]; void savePoints(previous, false).then((saved) => { if (saved) { setUndo((history) => history.slice(0, -1)); setSelectedPointId(previous[0]?.id ?? null); } }); }}>{text("undo")}</button> : null}
          </div>{job?.solution?.quality.reasons.length ? <ul>{job.solution.quality.reasons.map((reason) => <li key={reason}>{t(`ext.cameras.panorama.reason_${reason}`, {}, text("review_geometry"))}</li>)}</ul> : null}</details>
        </> : job?.active ? <><div className="cameraPanoramaActions"><button className="primaryButton" disabled={Boolean(busy)} onClick={() => void beginMapping(true)}>{text("review_calibration")}</button><button className="chipButton" onClick={close}>{text("return_composition")}</button></div>{job.permissions?.can_verify_aim !== false ? <details onToggle={(event) => setVerifyingAim(event.currentTarget.open)}><summary>{text("verify_camera_pointing")}</summary><div className="cameraPanoramaActions"><p className="cardMeta">{text(aimTarget ? "aim_selected" : "choose_aim")}</p><button className={aimTarget ? "primaryButton" : "chipButton"} disabled={!aimTarget || Boolean(busy) || job.permissions?.aim_enabled === false} onClick={() => void operate("aiming", async () => { if (aimTarget) { acceptJob(await aimCameraPanorama(cameraId, element.id, aimTarget)); setNotice("aim_complete"); } })}>{text("point_camera")}</button>{job.permissions?.aim_enabled === false ? <><p className="cardMeta">{text("visual_aim_not_verified")}</p><button className="chipButton" disabled={Boolean(busy)} onClick={() => { const point = navigationPoints.find((item) => item.role === "check"); if (point) { setVerifyingAim(true); selectPoint(point); } }}>{text("verify_camera_pointing")}</button></> : null}<button className="chipButton" onClick={close}>{text("return_composition")}</button></div></details> : null}</> : <div className="cameraPanoramaFooter"><button className="chipButton" onClick={() => { const point = navigationPoints.find((point) => point.id === selectedPointId) ?? navigationPoints[0]; if (point) selectPoint(point); }}>{text("back")}</button><button className="primaryButton" disabled={!canActivate || dirty || Boolean(busy)} onClick={() => void operate("activating", async () => { const activated = await mutate("activate"); setContext((value) => value ? { ...value, previous: value.active, active: { job_id: activated.id, revision: activated.revision } } : value); onActivate(activated); setNotice("activation_complete"); })}>{text("activate")}</button></div>}
      </>}
      {context?.active && !outdated && (step === 0 || context.active.job_id !== job?.id) ? <div className="cameraPanoramaActions"><button className="chipButton" disabled={Boolean(busy || dirty)} onClick={() => void operate("loading", async () => { const activeJob = await fetchCameraPanoramaJob(cameraId, context.active!.job_id); if (!activeJob.active) throw new CameraPanoramaRequestError("active_changed", "", 409); acceptJob(activeJob); setDrafts([]); setStep(3); })}>{text("view_active")}</button><p className="cardMeta">{text("new_panorama_help")}</p></div> : null}
      {job ? <details><summary>{text("details")}</summary>
      {job?.active && context?.previous && step === 3 ? <button className="chipButton" disabled={Boolean(busy)} onClick={() => void operate("restoring", async () => { const restored = await restoreCameraPanorama(cameraId, element.id, job.id, job.revision); acceptJob(restored); setContext((value) => value ? { ...value, previous: { job_id: job.id, revision: job.revision }, active: { job_id: restored.id, revision: restored.revision } } : value); onActivate(restored); setNotice("restored_mapping"); })}>{text("restore_previous")}</button> : null}
      {job?.active && step === 3 ? <button className="chipButton" onClick={() => onOpenSettings(job.source_id)}>{t("ext.cameras.source_panorama.open_settings")}</button> : null}
      {job?.aim_image_url && step === 3 ? <img className="cameraPanoramaCheckImage" src={resolveToposyncUrl(job.aim_image_url)} alt={text("check_image_alt")} /> : null}
      {job && !job.active && !captureRunning ? <details><summary>{text("discard_draft")}</summary><p className="cardMeta">{text(job.points.length === 1 ? "discard_draft_help_one" : "discard_draft_help", { count: job.points.length.toLocaleString(locale) })}</p><button className="dangerButton" disabled={Boolean(busy)} onClick={() => void operate("discarding", async () => { const result = await deleteCameraPanorama(cameraId, job.id, job.revision); if (!result.deleted) throw new CameraPanoramaRequestError("panorama_request_failed", "", 400); localStorage.removeItem(draftKey); setDrafts([]); setSelectedPointId(null); setUnsavedPoints(null); await load(); })}>{text("confirm_discard")}</button></details> : null}
      {job ? <><p className="cardMeta">{text("revision", { revision: job.revision })} · {new Date(typeof job.updated_at === "number" ? job.updated_at * 1000 : job.updated_at).toLocaleString(locale)}</p><p className="cardMeta">{text("support_help")}</p>{job.coverage_url ? <img src={resolveToposyncUrl(job.coverage_url)} alt={text("coverage_alt")} style={{ maxWidth: "100%", maxHeight: 120 }} /> : null}</> : null}
      </details> : null}
    </div>
  </SubModal>;
}
