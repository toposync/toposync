import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  CompositionElement,
  EditorToolPointerEvent,
  EditorToolSession,
  HostI18n,
  ToposyncHost,
  Viewport2DContext,
} from "@toposync/plugin-api";

import {
  captureCameraPtzViewAnchor,
  fetchCameraPtzStatus,
  fetchCameraSnapshot,
  moveCameraPtz,
  moveCameraPtzAbsolute,
  solveCameraProjection,
} from "../api/camerasApi";
import { createUniqueId } from "../parsing";
import type {
  CameraGroundLens,
  CameraPoseReference,
  CameraProjectionSolveResult,
  CameraRayGroundCalibratedView,
  CameraSourceConfig,
  PanTiltZoomState,
} from "../types";
import { SubModal } from "../ui/SubModal";
import { CameraGroundLensEditor } from "./CameraGroundLensEditor";
import { applyGroundLensDraft, groundImageResolutionStatus } from "../groundLensEditor";
import type { GroundImageSize } from "../groundLensEditor";

type PendingImagePoint = { x: number; y: number };
type DragTarget = { id: string; side: "image" | "world" };
type PtzFeedbackState = "idle" | "sending" | "following" | "settling" | "error";

const REQUIRED_FIT_POINTS = 6;
const REQUIRED_CHECK_POINTS = 2;
const MAX_CHECK_ERROR_METERS = 0.5;
const PTZ_PAN_TILT_STEP = 0.28;
const PTZ_ZOOM_STEP = 0.22;
const PTZ_MOVE_DURATION_SECONDS = 0.32;
const PTZ_FOLLOW_INTERVAL_MS = 550;
const PTZ_MINIMUM_FOLLOW_MS = 3000;
const PTZ_MAXIMUM_FOLLOW_MS = 7200;

function asRayGroundView(value: unknown): CameraRayGroundCalibratedView | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const projection = record.projection_model;
  if (!projection || typeof projection !== "object" || (projection as Record<string, unknown>).type !== "camera_ray_ground_v2") {
    return null;
  }
  return value as CameraRayGroundCalibratedView;
}

function cloneView(view: CameraRayGroundCalibratedView): CameraRayGroundCalibratedView {
  return structuredClone(view);
}

function lensForSource(source: CameraSourceConfig | undefined): CameraGroundLens {
  const profile = (source?.metadata ?? source?.origin?.metadata)?.lens_profile;
  if (!profile || typeof profile !== "object") return { type: "identity_rectilinear_v1" };
  const value = profile as Record<string, unknown>;
  const type = value.type;
  const fx = Number(value.fx);
  const fy = Number(value.fy);
  const cx = Number(value.cx);
  const cy = Number(value.cy);
  const coefficients = Array.isArray(value.coefficients) ? value.coefficients.map(Number) : [];
  if (
    (type !== "rectilinear_brown_v1" && type !== "fisheye_kb4_v1") ||
    ![fx, fy, cx, cy, ...coefficients].every(Number.isFinite) ||
    fx <= 0 || fy <= 0 ||
    (type === "rectilinear_brown_v1" && ![4, 5, 8].includes(coefficients.length)) ||
    (type === "fisheye_kb4_v1" && coefficients.length !== 4)
  ) return { type: "identity_rectilinear_v1" };
  return { type, fx, fy, cx, cy, coefficients };
}

function numericPose(status: PanTiltZoomState | null): CameraPoseReference | null {
  if (!status) return null;
  const pose = {
    pan: Number.isFinite(status.pan) ? Number(status.pan) : null,
    tilt: Number.isFinite(status.tilt) ? Number(status.tilt) : null,
    zoom: Number.isFinite(status.zoom) ? Number(status.zoom) : null,
  };
  return pose.pan === null && pose.tilt === null && pose.zoom === null ? null : pose;
}

function sourceHasPtz(source: CameraSourceConfig | undefined): boolean {
  return source?.origin?.has_ptz === true;
}

function sourceForView(
  view: CameraRayGroundCalibratedView,
  sources: CameraSourceConfig[],
): CameraSourceConfig | undefined {
  const sourceId = view.stream_scope.compatible_source_ids[0] ?? "";
  return sources.find((source) => source.id === sourceId);
}

function formatPtzAxis(value: number | null | undefined): string {
  if (!Number.isFinite(value)) return "—";
  return Number(value).toLocaleString("pt-BR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function ptzPositionLabel(
  pose: CameraPoseReference | PanTiltZoomState | null | undefined,
  emptyLabel = "posição ainda não vinculada",
): string {
  const numeric = numericPose(pose ?? null);
  if (numeric) {
    return `Pan ${formatPtzAxis(numeric.pan)} · Tilt ${formatPtzAxis(numeric.tilt)} · Zoom ${formatPtzAxis(numeric.zoom)}`;
  }
  const preset = String(pose?.preset_name || pose?.preset_token || "").trim();
  return preset ? `Posição ${preset}` : emptyLabel;
}

function ptzPosesMatch(
  expected: CameraPoseReference | null | undefined,
  current: PanTiltZoomState | null,
): boolean | null {
  const expectedPose = numericPose(expected ?? null);
  const currentPose = numericPose(current);
  if (!expectedPose || !currentPose) return null;
  const comparable = [
    [expectedPose.pan, currentPose.pan],
    [expectedPose.tilt, currentPose.tilt],
    [expectedPose.zoom, currentPose.zoom],
  ].filter((pair): pair is [number, number] => typeof pair[0] === "number" && typeof pair[1] === "number");
  if (!comparable.length) return null;
  return comparable.every(([expectedValue, currentValue]) => Math.abs(expectedValue - currentValue) <= 0.015);
}

function waitForPtzRefresh(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

function viewRotationDegrees(view: CameraRayGroundCalibratedView | null): 0 | 90 | 180 | 270 {
  const rotation = view?.editor_view_rotation_degrees;
  return rotation === 90 || rotation === 180 || rotation === 270 ? rotation : 0;
}

function rotatedDegrees(current: 0 | 90 | 180 | 270, direction: -1 | 1): 0 | 90 | 180 | 270 {
  return (((current + direction * 90 + 360) % 360) as 0 | 90 | 180 | 270);
}

function nextView(
  cameraSources: CameraSourceConfig[],
  index: number,
  preferredSourceId = "",
): CameraRayGroundCalibratedView {
  const source =
    cameraSources.find((item) => item.id === preferredSourceId && item.enabled !== false && item.kind === "video") ??
    cameraSources.find((item) => item.enabled !== false && item.kind === "video" && item.is_default) ??
    cameraSources.find((item) => item.enabled !== false && item.kind === "video") ??
    cameraSources[0];
  const sourceId = source?.id ?? "";
  const physicalViewId = source?.view_id || sourceId;
  return {
    id: createUniqueId(),
    label: index === 0 ? "Vista padrão" : `Vista ${index + 1}`,
    editor_view_rotation_degrees: 0,
    pose_reference: null,
    requires_pose_evidence: sourceHasPtz(source),
    stream_scope: {
      physical_view_id: physicalViewId,
      compatible_source_ids: sourceId ? [sourceId] : [],
      compatible_roles: source?.role ? [source.role] : ["main"],
    },
    projection_model: {
      type: "camera_ray_ground_v2",
      solver_version: 1,
      source_geometry: {
        width: source?.video?.width ?? 1920,
        height: source?.video?.height ?? 1080,
        rotation_degrees: 0,
        mirror_x: false,
        mirror_y: false,
      },
      lens: lensForSource(source),
      correspondences: [],
    },
    projection_quality: { status: "incomplete", estimated: false },
  };
}

type ImageContentBox = { left: number; top: number; width: number; height: number };

function imageContentBox(image: HTMLImageElement | null): ImageContentBox {
  if (!image || image.naturalWidth <= 0 || image.naturalHeight <= 0) {
    return { left: 0, top: 0, width: 1, height: 1 };
  }
  const rect = image.getBoundingClientRect();
  const naturalAspect = image.naturalWidth / image.naturalHeight;
  const renderedAspect = rect.width / Math.max(1, rect.height);
  if (renderedAspect > naturalAspect) {
    const width = naturalAspect / renderedAspect;
    return { left: (1 - width) / 2, top: 0, width, height: 1 };
  }
  const height = renderedAspect / naturalAspect;
  return { left: 0, top: (1 - height) / 2, width: 1, height };
}

function imagePoint(
  event: React.PointerEvent<HTMLElement>, image: HTMLImageElement | null,
): PendingImagePoint | null {
  if (!image?.complete || image.naturalWidth < 2 || image.naturalHeight < 2) return null;
  const rect = event.currentTarget.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const content = imageContentBox(image);
  const x = (event.clientX - rect.left) / Math.max(1, rect.width);
  const y = (event.clientY - rect.top) / Math.max(1, rect.height);
  if (
    x < content.left || x > content.left + content.width ||
    y < content.top || y > content.top + content.height
  ) return null;
  return {
    x: Math.max(0, Math.min(1, (x - content.left) / content.width)),
    y: Math.max(0, Math.min(1, (y - content.top) / content.height)),
  };
}

function pointLabel(index: number): string {
  return String(index + 1);
}

function pointColor(index: number): string {
  return ["#38bdf8", "#fbbf24", "#34d399", "#fb7185", "#a78bfa", "#fb923c"][index % 6];
}

export function CameraGroundMappingModal({
  open,
  onClose,
  host,
  i18n,
  element,
  cameraId,
  cameraSources,
  initialViews,
  initialDraftViews,
  onSaveDraft,
  onActivate,
}: {
  open: boolean;
  onClose: () => void;
  host: ToposyncHost;
  i18n: HostI18n;
  element: CompositionElement;
  cameraId: string;
  cameraSources: CameraSourceConfig[];
  initialViews: unknown;
  initialDraftViews: unknown;
  onSaveDraft: (views: unknown[]) => void;
  onActivate: (views: unknown[]) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [views, setViews] = useState<CameraRayGroundCalibratedView[]>([]);
  const [legacyViews, setLegacyViews] = useState<unknown[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotState, setSnapshotState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [loadedImage, setLoadedImage] = useState<(GroundImageSize & { url: string; context: string }) | null>(null);
  const [message, setMessage] = useState("Capture uma vista e marque pontos no chão.");
  const [isMarkingImage, setIsMarkingImage] = useState(false);
  const [planScope, setPlanScope] = useState<"camera" | "content">("camera");
  const [isPtzControlsOpen, setIsPtzControlsOpen] = useState(false);
  const [draftSaveState, setDraftSaveState] = useState<"saved" | "saving">("saved");
  const [pendingImage, setPendingImage] = useState<PendingImagePoint | null>(null);
  const [dragTarget, setDragTarget] = useState<DragTarget | null>(null);
  const [solutions, setSolutions] = useState<Record<string, CameraProjectionSolveResult | undefined>>({});
  const [lensEditingId, setLensEditingId] = useState<string | null>(null);
  const [ptzState, setPtzState] = useState<PanTiltZoomState | null>(null);
  const [ptzFeedbackState, setPtzFeedbackState] = useState<PtzFeedbackState>("idle");
  const [ptzFeedbackDetail, setPtzFeedbackDetail] = useState("");
  const [ptzFramesReceived, setPtzFramesReceived] = useState(0);
  const [ptzCommandInFlight, setPtzCommandInFlight] = useState(false);
  const [undo, setUndo] = useState<CameraRayGroundCalibratedView[][]>([]);
  const [redo, setRedo] = useState<CameraRayGroundCalibratedView[][]>([]);
  const viewsRef = useRef<CameraRayGroundCalibratedView[]>([]);
  const selectedIdRef = useRef<string | null>(null);
  const pendingImageRef = useRef<PendingImagePoint | null>(null);
  const dragRef = useRef<DragTarget | null>(null);
  const pointEditingBlockedRef = useRef(false);
  const hoveredWorldPointIdRef = useRef<string | null>(null);
  const plantViewportRef = useRef<Viewport2DContext | null>(null);
  const snapshotAbortRef = useRef<AbortController | null>(null);
  const ptzFollowAbortRef = useRef<AbortController | null>(null);
  const ptzCommandAbortRef = useRef<AbortController | null>(null);
  const ptzCommandInFlightRef = useRef(false);
  const captureRef = useRef<(bindPose?: boolean) => Promise<void>>();
  const solveAbortRef = useRef<AbortController | null>(null);
  const snapshotImageRef = useRef<HTMLImageElement | null>(null);
  const snapshotContextRef = useRef("");
  const snapshotRequestContextRef = useRef("");
  const onSaveDraftRef = useRef(onSaveDraft);
  const lastPersistedDraftSignatureRef = useRef("");
  const [, setImageLayoutRevision] = useState(0);

  const selectedView = useMemo(
    () => views.find((view) => view.id === selectedId) ?? views[0] ?? null,
    [selectedId, views],
  );
  const selectedSolution = selectedView ? solutions[selectedView.id] : undefined;
  useEffect(() => { setLensEditingId(null); }, [open, selectedView?.id]);
  const selectedViewRotation = viewRotationDegrees(selectedView);
  const renderedImage = imageContentBox(snapshotImageRef.current);
  const videoSources = useMemo(
    () => cameraSources.filter((source) => source.enabled !== false && source.kind === "video"),
    [cameraSources],
  );
  const selectedSource = useMemo(() => {
    const sourceId = selectedView?.stream_scope.compatible_source_ids[0] ?? "";
    if (sourceId) return videoSources.find((source) => source.id === sourceId);
    return videoSources.find((source) => source.is_default) ?? videoSources[0];
  }, [selectedView, videoSources]);
  const selectedSourceHasPtz = sourceHasPtz(selectedSource);
  const snapshotContext = JSON.stringify([cameraId, selectedView?.id, selectedSource?.id]);
  snapshotContextRef.current = snapshotContext;
  const imageSize = snapshotState === "ready" && loadedImage?.url === snapshotUrl && loadedImage.context === snapshotContext ? loadedImage : null;
  const resolutionStatus = groundImageResolutionStatus(imageSize, selectedView?.projection_model.source_geometry ?? { width: 0, height: 0 });
  const imageEditingReady = resolutionStatus === "match";
  const imageDiagnostic = t(imageSize ? "ext.cameras.ground_lens.resolution_mismatch" : "ext.cameras.ground_lens.image_required");
  const points = selectedView?.projection_model.correspondences ?? [];
  const fitCount = points.filter((point) => point.role === "fit").length;
  const checkCount = points.filter((point) => point.role === "check").length;
  const ptzCurrentMatchesView = selectedSourceHasPtz
    ? ptzPosesMatch(selectedView?.pose_reference, ptzState)
    : null;
  const ptzFeedbackIsActive = ptzFeedbackState === "sending" || ptzFeedbackState === "following" || ptzFeedbackState === "settling";
  const pointEditingBlocked = !imageEditingReady || lensEditingId === selectedView?.id || ptzFeedbackIsActive || Boolean(selectedView?.pose_reference && ptzCurrentMatchesView === false);
  pointEditingBlockedRef.current = pointEditingBlocked;

  useEffect(() => {
    if (!pointEditingBlocked) return;
    pendingImageRef.current = null;
    dragRef.current = null;
    setPendingImage(null);
    setDragTarget(null);
    setIsMarkingImage(false);
  }, [pointEditingBlocked]);

  useEffect(() => {
    viewsRef.current = views;
  }, [views]);
  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);
  useEffect(() => {
    pendingImageRef.current = pendingImage;
  }, [pendingImage]);
  useEffect(() => {
    dragRef.current = dragTarget;
  }, [dragTarget]);
  useEffect(() => {
    onSaveDraftRef.current = onSaveDraft;
  }, [onSaveDraft]);

  const restoreViews = useCallback((next: CameraRayGroundCalibratedView[], record = true) => {
    setViews((current) => {
      if (record) {
        setUndo((history) => [...history.slice(-49), current.map(cloneView)]);
        setRedo([]);
      }
      return next;
    });
  }, []);

  const updateSelected = useCallback(
    (mutate: (view: CameraRayGroundCalibratedView) => CameraRayGroundCalibratedView, record = true) => {
      const activeId = selectedIdRef.current;
      solveAbortRef.current?.abort();
      if (activeId) {
        setSolutions((current) => {
          const { [activeId]: _discarded, ...remaining } = current;
          return remaining;
        });
      }
      restoreViews(
        viewsRef.current.map((view) => (view.id === activeId ? mutate(cloneView(view)) : view)),
        record,
      );
    },
    [restoreViews],
  );

  const rotatePlant = useCallback((direction: -1 | 1) => {
    const activeId = selectedIdRef.current;
    if (!activeId) return;
    setUndo((history) => [...history.slice(-49), viewsRef.current.map(cloneView)]);
    setRedo([]);
    setViews((current) => current.map((view) => (
      view.id === activeId
        ? { ...view, editor_view_rotation_degrees: rotatedDegrees(viewRotationDegrees(view), direction) }
        : view
    )));
    setMessage("Orientação da planta alterada; o rascunho será salvo sem alterar as coordenadas reais.");
  }, []);

  const applySnapshotBlob = useCallback((blob: Blob) => {
    const nextUrl = URL.createObjectURL(blob);
    snapshotRequestContextRef.current = snapshotContextRef.current;
    setLoadedImage(null);
    setSnapshotUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return nextUrl;
    });
    // HTTP success is not image readiness: wait for the browser to decode it.
    setSnapshotState("loading");
  }, []);

  const followPtzMotion = useCallback(async (sourceId: string, actionLabel: string) => {
    ptzFollowAbortRef.current?.abort();
    const controller = new AbortController();
    ptzFollowAbortRef.current = controller;
    const startedAt = Date.now();
    let receivedFrames = 0;
    let consecutiveIdleReadings = 0;
    let lastStatus: PanTiltZoomState | null = null;
    let lastSnapshotError: unknown = null;

    setPtzFeedbackState("following");
    setPtzFeedbackDetail(`${actionLabel}. Atualizando o quadro automaticamente…`);
    setPtzFramesReceived(0);
    setSnapshotState((current) => current === "ready" ? current : "loading");

    while (!controller.signal.aborted) {
      await waitForPtzRefresh(PTZ_FOLLOW_INTERVAL_MS, controller.signal);
      if (controller.signal.aborted) return;

      const [statusResult, snapshotResult] = await Promise.allSettled([
        fetchCameraPtzStatus(cameraId, sourceId, controller.signal),
        fetchCameraSnapshot(cameraId, sourceId, controller.signal, true, "decoder"),
      ]);
      if (controller.signal.aborted) return;

      if (statusResult.status === "fulfilled") {
        lastStatus = statusResult.value.status ?? null;
        setPtzState(lastStatus);
      }
      if (snapshotResult.status === "fulfilled") {
        receivedFrames += 1;
        setPtzFramesReceived(receivedFrames);
        applySnapshotBlob(snapshotResult.value);
        lastSnapshotError = null;
      } else {
        lastSnapshotError = snapshotResult.reason;
      }

      const elapsed = Date.now() - startedAt;
      const isMoving = String(lastStatus?.move_status ?? "").toLowerCase() === "moving";
      if (isMoving || elapsed < PTZ_MINIMUM_FOLLOW_MS) consecutiveIdleReadings = 0;
      else consecutiveIdleReadings += 1;

      setPtzFeedbackState(isMoving ? "following" : "settling");
      setPtzFeedbackDetail(
        `${isMoving ? "Câmera em movimento" : "Aguardando a imagem estabilizar"} · ${receivedFrames} ${receivedFrames === 1 ? "quadro recebido" : "quadros recebidos"}`,
      );

      if (
        elapsed >= PTZ_MAXIMUM_FOLLOW_MS ||
        (elapsed >= PTZ_MINIMUM_FOLLOW_MS && consecutiveIdleReadings >= 2)
      ) break;
    }

    if (controller.signal.aborted) return;
    if (receivedFrames === 0) {
      setPtzFeedbackState("error");
      setPtzFeedbackDetail("A câmera recebeu o comando, mas o quadro automático não pôde ser atualizado.");
      setSnapshotState("error");
      setMessage(
        lastSnapshotError instanceof Error
          ? lastSnapshotError.message
          : "A câmera se moveu, mas não foi possível atualizar o quadro automaticamente.",
      );
      return;
    }

    const finalMoveStatus = String(lastStatus?.move_status ?? "").toLowerCase();
    if (finalMoveStatus === "moving") {
      setPtzFeedbackState("error");
      setPtzFeedbackDetail("O último quadro foi atualizado, mas a câmera ainda não confirmou que parou.");
      setMessage("A câmera ainda informa movimento. Aguarde um instante antes de vincular a posição ou ajustar pontos.");
      return;
    }
    setPtzFeedbackState("idle");
    setPtzFeedbackDetail("");
    setMessage(
      finalMoveStatus === "idle"
        ? "Câmera estabilizada e quadro atualizado automaticamente. Você já pode avaliar a nova posição."
        : "Acompanhamento concluído e quadro atualizado automaticamente. Confira a posição antes de ajustar pontos.",
    );
  }, [applySnapshotBlob, cameraId]);

  const startPtzCommand = useCallback(async (
    sourceId: string,
    actionLabel: string,
    command: (signal: AbortSignal) => Promise<unknown>,
  ) => {
    if (ptzCommandInFlightRef.current) return;
    snapshotAbortRef.current?.abort();
    ptzFollowAbortRef.current?.abort();
    ptzCommandAbortRef.current?.abort();
    const controller = new AbortController();
    ptzCommandAbortRef.current = controller;
    ptzCommandInFlightRef.current = true;
    setPtzCommandInFlight(true);
    setPtzFeedbackState("sending");
    setPtzFeedbackDetail(`${actionLabel}. Enviando comando…`);
    setPtzFramesReceived(0);
    setPendingImage(null);
    setIsMarkingImage(false);
    setMessage(`${actionLabel}. O quadro acompanhará o movimento automaticamente.`);
    try {
      await command(controller.signal);
      if (controller.signal.aborted) return;
      ptzCommandInFlightRef.current = false;
      setPtzCommandInFlight(false);
      void followPtzMotion(sourceId, actionLabel);
    } catch (error) {
      if (controller.signal.aborted) return;
      setPtzFeedbackState("error");
      const detail = error instanceof Error ? error.message : "Não foi possível mover a câmera.";
      setPtzFeedbackDetail(detail);
      setMessage(detail);
    } finally {
      if (ptzCommandAbortRef.current === controller) {
        ptzCommandInFlightRef.current = false;
        setPtzCommandInFlight(false);
      }
    }
  }, [followPtzMotion]);

  const capture = useCallback(async (bindPose = false) => {
    if (!cameraId || !selectedView) return;
    const sourceId = selectedView.stream_scope.compatible_source_ids[0] ?? "";
    if (!sourceId) {
      setSnapshotState("error");
      setMessage("Selecione a fonte física desta vista antes de carregar o quadro.");
      return;
    }
    ptzFollowAbortRef.current?.abort();
    setPtzFeedbackState("idle");
    setPtzFeedbackDetail("");
    setPtzFramesReceived(0);
    snapshotAbortRef.current?.abort();
    const controller = new AbortController();
    snapshotAbortRef.current = controller;
    setSnapshotState("loading");
    setMessage("Capturando um quadro novo…");
    try {
      const [blob, ptz] = await Promise.all([
        fetchCameraSnapshot(cameraId, sourceId, controller.signal, true, "decoder"),
        selectedSourceHasPtz ? fetchCameraPtzStatus(cameraId, sourceId, controller.signal) : Promise.resolve({ status: null }),
      ]);
      if (controller.signal.aborted) return;
      const status = ptz.status ?? null;
      setPtzState(status);
      if (selectedSourceHasPtz && String(status?.move_status ?? "").toLowerCase() === "moving") {
        setSnapshotState("idle");
        setMessage("A câmera ainda está movimentando. Espere parar e capture novamente.");
        return;
      }
      if (selectedSourceHasPtz && bindPose) {
        const pose = numericPose(status);
        if (!pose) {
          setSnapshotState("idle");
          setMessage("A posição PTZ não foi informada. Esta vista não pode ser ativada.");
          return;
        }
        const previousPose = selectedView.pose_reference;
        const previousPan = previousPose?.pan;
        const previousTilt = previousPose?.tilt;
        const previousZoom = previousPose?.zoom;
        const changedPose = [
          [previousPan, pose.pan],
          [previousTilt, pose.tilt],
          [previousZoom, pose.zoom],
        ].some(([before, after]) => typeof before === "number" && typeof after === "number" && Math.abs(before - after) > 0.001);
        if (previousPose?.preset_token && changedPose) {
          setSnapshotState("idle");
          setMessage("Esta é outra posição PTZ. Crie uma nova vista em vez de alterar a vista existente.");
          return;
        }
        const anchor = !bindPose || previousPose?.preset_token
          ? null
          : await captureCameraPtzViewAnchor(
              cameraId,
              {
                source_id: sourceId,
                name: `TSV-${selectedView.id.slice(-20)}`,
                idempotency_key: `camera-ray-ground:${selectedView.id}`,
              },
              controller.signal,
            );
        if (controller.signal.aborted) return;
        updateSelected((view) => ({
          ...view,
          pose_reference: {
            ...pose,
            preset_token: anchor?.token ?? previousPose?.preset_token ?? null,
            preset_name: anchor?.name ?? previousPose?.preset_name ?? null,
          },
          requires_pose_evidence: true,
        }));
      }
      applySnapshotBlob(blob);
      setPendingImage(null);
      setMessage(
        selectedSourceHasPtz
          ? bindPose
            ? "Posição PTZ vinculada e quadro atualizado. Adicione pontos no chão."
            : "Quadro atualizado. Você já pode marcar pontos; vincule a posição PTZ apenas antes de ativar o mapa."
          : "Quadro atualizado. Adicione um ponto no chão da imagem.",
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      setSnapshotState("error");
      setMessage(error instanceof Error ? error.message : "Não foi possível capturar a vista.");
    }
  }, [applySnapshotBlob, cameraId, selectedSourceHasPtz, selectedView, updateSelected]);

  captureRef.current = capture;

  useEffect(() => {
    if (!open) return;
    const rawViews = Array.isArray(initialViews) ? initialViews : [];
    const rawDraftViews = Array.isArray(initialDraftViews) ? initialDraftViews : [];
    const v2 = rawViews.map(asRayGroundView).filter((view): view is CameraRayGroundCalibratedView => Boolean(view));
    const drafts = rawDraftViews.map(asRayGroundView).filter((view): view is CameraRayGroundCalibratedView => Boolean(view));
    const initial = drafts.length ? drafts.map(cloneView) : v2.length ? v2.map(cloneView) : [nextView(cameraSources, 0)];
    setLegacyViews(rawViews.filter((view) => !asRayGroundView(view)));
    setViews(initial);
    lastPersistedDraftSignatureRef.current = JSON.stringify(initial);
    setSelectedId(initial[0]?.id ?? null);
    setUndo([]);
    setRedo([]);
    setSolutions({});
    setPtzState(null);
    setPtzFeedbackState("idle");
    setPtzFeedbackDetail("");
    setPtzFramesReceived(0);
    ptzCommandInFlightRef.current = false;
    setPtzCommandInFlight(false);
    setPendingImage(null);
    setDragTarget(null);
    hoveredWorldPointIdRef.current = null;
    plantViewportRef.current = null;
    setIsMarkingImage(false);
    setPlanScope("camera");
    setIsPtzControlsOpen(false);
    setDraftSaveState("saved");
    setMessage(drafts.length ? "Rascunho recuperado. Continue do próximo ponto." : "Comece marcando um ponto em comum.");
    return () => {
      snapshotAbortRef.current?.abort();
      ptzFollowAbortRef.current?.abort();
      ptzCommandAbortRef.current?.abort();
      solveAbortRef.current?.abort();
    };
  // The opened modal owns its draft. Parent updates from autosave must not reset in-progress work.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open || views.length === 0) return;
    const signature = JSON.stringify(views);
    if (signature === lastPersistedDraftSignatureRef.current) return;
    setDraftSaveState("saving");
    const timer = window.setTimeout(() => {
      onSaveDraftRef.current(views.map(cloneView));
      lastPersistedDraftSignatureRef.current = signature;
      setDraftSaveState("saved");
    }, 260);
    return () => window.clearTimeout(timer);
  }, [open, views]);

  useEffect(() => {
    snapshotAbortRef.current?.abort();
    ptzFollowAbortRef.current?.abort();
    ptzCommandAbortRef.current?.abort();
    ptzCommandInFlightRef.current = false;
    setSnapshotUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return null;
    });
    setSnapshotState("idle");
    setLoadedImage(null);
    setPtzState(null);
    setPtzFeedbackState("idle");
    setPtzFeedbackDetail("");
    setPtzFramesReceived(0);
    setPtzCommandInFlight(false);
    setPendingImage(null);
    setIsMarkingImage(false);
  }, [cameraId, selectedSource?.id, selectedView?.id]);

  // The editor can open before the camera index arrives. Bind the new empty view
  // as soon as its real source becomes available, otherwise capture would keep
  // asking for an empty source identifier and leave the user at "Sem captura".
  useEffect(() => {
    if (!open || !selectedView || !selectedSource) return;
    const sourceId = selectedView.stream_scope.compatible_source_ids[0] ?? "";
    if (sourceId) return;
    updateSelected((view) => ({
      ...view,
      requires_pose_evidence: sourceHasPtz(selectedSource),
      stream_scope: {
        physical_view_id: selectedSource.view_id || selectedSource.id,
        compatible_source_ids: [selectedSource.id],
        compatible_roles: [selectedSource.role],
      },
      // Source discovery must not overwrite parameters already entered in this draft.
    }), false);
    setMessage("Fonte da vista encontrada. Carregando o quadro atual…");
  }, [open, selectedSource, selectedView, updateSelected]);

  useEffect(() => {
    if (!open || !cameraId || !selectedView || !selectedSource) return;
    void captureRef.current?.(false);
  }, [cameraId, open, selectedSource?.id, selectedView?.id]);

  useEffect(() => () => {
    if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
  }, [snapshotUrl]);

  useEffect(() => {
    if (!selectedView || fitCount < 4 || !imageEditingReady || lensEditingId === selectedView.id) return;
    solveAbortRef.current?.abort();
    const controller = new AbortController();
    solveAbortRef.current = controller;
    const timer = window.setTimeout(() => {
      void solveCameraProjection(selectedView, controller.signal)
        .then((result) => {
          if (!controller.signal.aborted) setSolutions((current) => ({ ...current, [selectedView.id]: result }));
        })
        .catch(() => undefined);
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [fitCount, selectedView, lensEditingId, imageEditingReady]);

  const addWorldPoint = useCallback((world: { x: number; z: number }) => {
    if (pointEditingBlockedRef.current) return;
    const image = pendingImageRef.current;
    if (!image || !selectedIdRef.current) return;
    const active = viewsRef.current.find((view) => view.id === selectedIdRef.current);
    if (!active) return;
    const existing = active.projection_model.correspondences;
    const existingFitCount = existing.filter((point) => point.role === "fit").length;
    const existingCheckCount = existing.filter((point) => point.role === "check").length;
    const role = existingFitCount < REQUIRED_FIT_POINTS ? "fit" : "check";
    if (role === "check" && existingCheckCount >= REQUIRED_CHECK_POINTS) {
      setPendingImage(null);
      setIsMarkingImage(false);
      setMessage("Os 8 pontos já foram marcados. Não há outro ponto a adicionar: ajuste ou refaça os dois pontos de conferência abaixo.");
      return;
    }
    updateSelected(
      (view) => ({
        ...view,
        projection_quality: { status: "incomplete", estimated: false },
        projection_model: {
          ...view.projection_model,
          correspondences: [
            ...view.projection_model.correspondences,
            { id: createUniqueId(), role, origin: "manual", image, world },
          ],
        },
      }),
    );
    setPendingImage(null);
    setIsMarkingImage(false);
    setMessage(
      role === "fit"
        ? "Ponto de ajuste salvo. Marque outro marco no chão."
        : existingCheckCount + 1 === REQUIRED_CHECK_POINTS
          ? "Os dois pontos de conferência foram salvos. Veja o resultado da validação abaixo."
          : "Primeiro ponto de conferência salvo. Marque mais um marco diferente para conferir o resultado.",
    );
  }, [updateSelected]);

  const movePoint = useCallback((id: string, side: DragTarget["side"], point: { x: number; y: number } | { x: number; z: number }) => {
    if (pointEditingBlockedRef.current) return;
    updateSelected(
      (view) => ({
        ...view,
        projection_quality: { status: "incomplete", estimated: false },
        projection_model: {
          ...view.projection_model,
          correspondences: view.projection_model.correspondences.map((candidate) =>
            candidate.id === id ? { ...candidate, [side === "image" ? "image" : "world"]: point } : candidate,
          ),
        },
      }),
      false,
    );
  }, [updateSelected]);

  const worldPointAtScreen = useCallback((
    view: CameraRayGroundCalibratedView,
    screen: { x: number; y: number },
  ) => {
    const viewport = plantViewportRef.current;
    if (!viewport) return null;
    let nearest: CameraRayGroundCalibratedView["projection_model"]["correspondences"][number] | null = null;
    let nearestDistance = Number.POSITIVE_INFINITY;
    for (const point of view.projection_model.correspondences) {
      const marker = viewport.worldToScreen(point.world);
      const distance = Math.hypot(marker.x - screen.x, marker.y - screen.y);
      if (distance < nearestDistance) {
        nearest = point;
        nearestDistance = distance;
      }
    }
    return nearestDistance <= 16 ? nearest : null;
  }, []);

  const beginPointDrag = useCallback((
    view: CameraRayGroundCalibratedView,
    pointId: string,
    side: DragTarget["side"],
  ) => {
    const index = view.projection_model.correspondences.findIndex((point) => point.id === pointId);
    if (index < 0) return;
    const target = { id: pointId, side } satisfies DragTarget;
    setUndo((history) => [...history.slice(-49), viewsRef.current.map(cloneView)]);
    setRedo([]);
    dragRef.current = target;
    setDragTarget(target);
    setMessage(`Ajustando o ponto ${pointLabel(index)} na ${side === "image" ? "imagem" : "planta"}. Arraste e solte na posição correta.`);
  }, []);

  const finishPointDrag = useCallback((target: DragTarget) => {
    const active = viewsRef.current.find((view) => view.id === selectedIdRef.current);
    const index = active?.projection_model.correspondences.findIndex((point) => point.id === target.id) ?? -1;
    dragRef.current = null;
    setDragTarget(null);
    setMessage(`${index >= 0 ? `Ponto ${pointLabel(index)} ajustado` : "Ponto ajustado"} na ${target.side === "image" ? "imagem" : "planta"}. Recalculando a validação…`);
  }, []);

  const session = useMemo<EditorToolSession>(() => ({
    shouldCapturePointer: (event: EditorToolPointerEvent) => {
      if (event.kind !== "down" || event.button !== 0) return false;
      if (pointEditingBlockedRef.current) return false;
      if (pendingImageRef.current) return true;
      const active = viewsRef.current.find((view) => view.id === selectedIdRef.current);
      return Boolean(active && worldPointAtScreen(active, event.screen));
    },
    onPointerEvent: (event: EditorToolPointerEvent) => {
      if (pointEditingBlockedRef.current) return;
      const active = viewsRef.current.find((view) => view.id === selectedIdRef.current);
      if (!active) return;
      const currentDrag = dragRef.current;
      if (event.kind === "down") {
        if (pendingImageRef.current) {
          addWorldPoint({ x: event.world.x, z: event.world.z });
          return;
        }
        const near = worldPointAtScreen(active, event.screen);
        if (near) {
          hoveredWorldPointIdRef.current = near.id;
          beginPointDrag(active, near.id, "world");
          return;
        }
        return;
      }
      if (event.kind === "move") {
        if (currentDrag?.side === "world") {
          movePoint(currentDrag.id, "world", { x: event.world.x, z: event.world.z });
          return;
        }
        hoveredWorldPointIdRef.current = worldPointAtScreen(active, event.screen)?.id ?? null;
        return;
      }
      if ((event.kind === "up" || event.kind === "cancel") && currentDrag?.side === "world") {
        finishPointDrag(currentDrag);
      }
    },
    renderOverlay2D: ({ ctx, viewport }) => {
      plantViewportRef.current = viewport;
      const active = viewsRef.current.find((view) => view.id === selectedIdRef.current);
      if (!active) return;
      const solution = solutions[active.id];
      const polygon = solution?.valid_world_polygon ?? [];
      if (polygon.length >= 3) {
        ctx.save();
        ctx.fillStyle = "rgba(56, 189, 248, 0.12)";
        ctx.strokeStyle = "rgba(56, 189, 248, 0.92)";
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        polygon.forEach((point, index) => {
          const screen = viewport.worldToScreen(point);
          if (index === 0) ctx.moveTo(screen.x, screen.y);
          else ctx.lineTo(screen.x, screen.y);
        });
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.restore();
      }
      active.projection_model.correspondences.forEach((point, index) => {
        const screen = viewport.worldToScreen(point.world);
        const isActive = dragRef.current?.side === "world" && dragRef.current.id === point.id;
        const isHovered = hoveredWorldPointIdRef.current === point.id;
        ctx.save();
        if (isActive || isHovered) {
          ctx.strokeStyle = isActive ? "rgba(255, 255, 255, .98)" : "rgba(255, 255, 255, .78)";
          ctx.lineWidth = 2;
          ctx.setLineDash(isActive ? [] : [3, 3]);
          ctx.beginPath();
          ctx.arc(screen.x, screen.y, isActive ? 15 : 13, 0, Math.PI * 2);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        ctx.fillStyle = pointColor(index);
        ctx.strokeStyle = "rgba(15, 23, 42, .94)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        if (point.role === "check") ctx.roundRect(screen.x - 9, screen.y - 9, 18, 18, 4);
        else ctx.arc(screen.x, screen.y, 9, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#0f172a";
        ctx.font = "bold 11px system-ui";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(pointLabel(index), screen.x, screen.y + 0.5);
        ctx.restore();
      });
      const camera = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      ctx.save();
      ctx.fillStyle = "rgba(255,255,255,.92)";
      ctx.beginPath();
      ctx.arc(camera.x, camera.y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    },
    getCursor: () => (pointEditingBlockedRef.current ? "not-allowed" : dragRef.current ? "grabbing" : pendingImageRef.current ? "crosshair" : "grab"),
  }), [addWorldPoint, beginPointDrag, element.position.x, element.position.z, finishPointDrag, movePoint, solutions, worldPointAtScreen]);

  if (!open) return null;

  function onImagePointerDown(event: React.PointerEvent<HTMLElement>): void {
    if (event.button !== 0) return;
    if (pointEditingBlockedRef.current) {
      setMessage(!imageEditingReady ? imageDiagnostic : "Aguarde a câmera estabilizar ou volte à posição vinculada antes de ajustar pontos.");
      return;
    }
    const image = imagePoint(event, snapshotImageRef.current);
    const active = selectedView;
    if (!active || image === null) {
      if (snapshotState === "ready") setMessage("Marque apenas dentro da imagem, não nas faixas vazias.");
      return;
    }
    const nearest = active.projection_model.correspondences.find(
      (point) => Math.hypot(point.image.x - image.x, point.image.y - image.y) < 0.035,
    );
    if (nearest) {
      beginPointDrag(active, nearest.id, "image");
      event.currentTarget.setPointerCapture(event.pointerId);
      return;
    }
    if (snapshotState !== "ready") {
      setMessage("Capture uma vista antes de marcar pontos.");
      return;
    }
    if (!isMarkingImage) {
      setMessage("Use “Marcar ponto de ajuste” ou “Marcar ponto de conferência” e então escolha um marco no chão da imagem.");
      return;
    }
    const activeFitCount = active.projection_model.correspondences.filter((point) => point.role === "fit").length;
    const activeCheckCount = active.projection_model.correspondences.filter((point) => point.role === "check").length;
    if (activeFitCount >= REQUIRED_FIT_POINTS && activeCheckCount >= REQUIRED_CHECK_POINTS) {
      setPendingImage(null);
      setIsMarkingImage(false);
      setMessage("Os 8 pontos necessários já estão marcados. Agora ajuste ou refaça um ponto de conferência se a validação pedir.");
      return;
    }
    setPendingImage(image);
    setIsMarkingImage(false);
    setMessage("Agora clique no mesmo marco na planta.");
  }

  function onImagePointerMove(event: React.PointerEvent<HTMLElement>): void {
    if (pointEditingBlockedRef.current) return;
    const current = dragRef.current;
    const point = imagePoint(event, snapshotImageRef.current);
    if (current?.side === "image" && point) movePoint(current.id, "image", point);
  }

  function onImagePointerUp(event: React.PointerEvent<HTMLElement>): void {
    const current = dragRef.current;
    if (current?.side === "image") {
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      finishPointDrag(current);
    }
  }

  const sourceCanChange = points.length === 0 && !selectedView?.pose_reference;
  const selectPhysicalSource = (sourceId: string) => {
    const source = videoSources.find((candidate) => candidate.id === sourceId);
    if (!source || !sourceCanChange) {
      setMessage("Crie uma nova vista para usar outra fonte física.");
      return;
    }
    updateSelected((view) => ({
      ...view,
      pose_reference: null,
      requires_pose_evidence: sourceHasPtz(source),
      stream_scope: {
        physical_view_id: source.view_id || source.id,
        compatible_source_ids: [source.id],
        compatible_roles: [source.role],
      },
      projection_model: {
        ...view.projection_model,
        source_geometry: {
          width: source.video?.width ?? 1920,
          height: source.video?.height ?? 1080,
          rotation_degrees: 0,
          mirror_x: false,
          mirror_y: false,
        },
        lens: lensForSource(source),
        correspondences: [],
      },
      projection_quality: { status: "incomplete", estimated: false },
    }));
    setIsMarkingImage(false);
    setMessage("Fonte alterada. Carregando o quadro atual desta ótica…");
  };

  const poseIsBound = !selectedSourceHasPtz || Boolean(selectedView?.pose_reference && numericPose(selectedView.pose_reference));
  const canActivate = Boolean(selectedSolution?.accepted && poseIsBound && imageEditingReady && !pointEditingBlocked && lensEditingId !== selectedView?.id);
  const pointsAreComplete = fitCount >= REQUIRED_FIT_POINTS && checkCount >= REQUIRED_CHECK_POINTS;
  const canAddPoint = !pointsAreComplete;
  const nextPointDescription = fitCount < REQUIRED_FIT_POINTS
    ? `ponto de ajuste ${fitCount + 1} de ${REQUIRED_FIT_POINTS}`
    : `ponto de conferência ${checkCount + 1} de ${REQUIRED_CHECK_POINTS}`;
  const checkErrors = selectedSolution?.quality.check_errors_meters ?? [];
  const failedCheckIndexes = checkErrors
    .map((error, index) => Number.isFinite(error) && error <= MAX_CHECK_ERROR_METERS ? null : index)
    .filter((index): index is number => index !== null);
  const failedCheckLabel = failedCheckIndexes.map((index) => index + 1).join(" e ");
  const failedCheckVerb = failedCheckIndexes.length === 1 ? "ficou" : "ficaram";
  const workflowInstruction = pendingImage
    ? "2. Agora clique no mesmo marco na planta."
    : isMarkingImage
      ? "1. Clique em um marco fixo que esteja no chão da imagem."
      : canActivate
        ? "A validação passou. Ative este mapeamento quando estiver satisfeito."
        : fitCount < REQUIRED_FIT_POINTS
          ? `Marque o ${nextPointDescription}. Prefira quinas, juntas de piso e bases de objetos.`
          : checkCount < REQUIRED_CHECK_POINTS
            ? `Marque o ${nextPointDescription}. Ele não ajusta o mapa: só mede se os seis pontos anteriores funcionam.`
            : failedCheckIndexes.length
              ? `A${failedCheckIndexes.length === 1 ? " conferência" : "s conferências"} ${failedCheckLabel} ${failedCheckVerb} fora do limite de ${MAX_CHECK_ERROR_METERS.toFixed(2)} m. Arraste ou refaça ${failedCheckIndexes.length === 1 ? "apenas esse ponto" : "apenas esses pontos"}.`
              : "Os 8 pontos foram marcados. A validação está sendo calculada.";
  const movePtz = (pan: number, tilt: number, zoom: number, actionLabel: string) => {
    const sourceId = selectedSource?.id ?? selectedView?.stream_scope.compatible_source_ids[0] ?? "";
    if (!cameraId || !sourceId) return;
    void startPtzCommand(
      sourceId,
      actionLabel,
      (signal) => moveCameraPtz(
        cameraId,
        { source_id: sourceId, pan, tilt, zoom, timeout_s: PTZ_MOVE_DURATION_SECONDS },
        signal,
      ),
    );
  };
  const goToSelectedView = () => {
    const pose = selectedView?.pose_reference;
    const sourceId = selectedSource?.id ?? selectedView?.stream_scope.compatible_source_ids[0] ?? "";
    if (!cameraId || !sourceId || !pose || !numericPose(pose)) return;
    void startPtzCommand(
      sourceId,
      `Indo para ${selectedView.label}`,
      (signal) => moveCameraPtzAbsolute(
        cameraId,
        { source_id: sourceId, pan: pose.pan, tilt: pose.tilt, zoom: pose.zoom },
        signal,
      ),
    );
  };
  const startMarking = () => {
    if (pointEditingBlockedRef.current) {
      setMessage(!imageEditingReady ? imageDiagnostic : "Aguarde a câmera estabilizar ou volte à posição vinculada antes de marcar pontos.");
      return;
    }
    if (pendingImage) {
      setMessage("Conclua o par atual clicando no mesmo marco na planta.");
      return;
    }
    if (snapshotState !== "ready") {
      setMessage("Espere o quadro atual da câmera carregar antes de marcar um ponto.");
      return;
    }
    if (!canAddPoint) {
      setPendingImage(null);
      setIsMarkingImage(false);
      setMessage("Os 8 pontos necessários já existem. Não há ponto de revisão para adicionar; ajuste ou refaça um ponto de conferência se ele estiver fora do limite.");
      return;
    }
    setIsMarkingImage(true);
    setMessage(`1. Clique no ${nextPointDescription} na imagem.`);
  };
  const activateCurrentMapping = () => {
    if (!canActivate) return;
    const saved = views.map((view) => {
      const result = solutions[view.id];
      return {
        ...view,
        projection_quality: result?.accepted
          ? {
              status: "ready" as const,
              estimated: false,
              calibration_digest: result.calibration_digest,
              fit_points: result.quality.number_of_fit_points,
              fit_inliers: result.quality.number_of_inliers,
              check_points: view.projection_model.correspondences.filter((point) => point.role === "check").length,
              check_errors_meters: result.quality.check_errors_meters,
              image_coverage_ratio: result.quality.image_hull_area_ratio_uv,
              solver: "camera_ray_ground_v2",
            }
          : { status: "incomplete" as const, estimated: false },
      };
    });
    onActivate([...legacyViews, ...saved]);
    setMessage("Mapeamento ativado. O rascunho continua disponível para ajustes futuros.");
  };
  return (
    <SubModal
      open
      onClose={onClose}
      title="Mapear câmera"
      panelStyle={{ width: "min(1440px, calc(100vw - 28px))", height: "calc(100dvh - 28px)", maxHeight: "calc(100dvh - 28px)" }}
      bodyStyle={{ padding: 0, minHeight: 0 }}
    >
      <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, gap: 10, padding: 12, overflow: "auto" }}>
        <div className="rowWrap" style={{ justifyContent: "space-between", gap: 8 }}>
          <div className="rowWrap" style={{ gap: 6 }}>
            {views.map((view) => {
              const viewHasPtz = view.requires_pose_evidence === true || sourceHasPtz(sourceForView(view, videoSources));
              return (
                <button
                  key={view.id}
                  className="chipButton"
                  type="button"
                  onClick={() => setSelectedId(view.id)}
                  aria-current={view.id === selectedView?.id ? "true" : undefined}
                  title={viewHasPtz ? `${view.label} · PTZ · ${ptzPositionLabel(view.pose_reference)}` : view.label}
                  style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 2, minHeight: 44 }}
                >
                  <span>{view.label}</span>
                  {viewHasPtz ? <small className="cardMeta" style={{ fontSize: 10 }}>PTZ · {ptzPositionLabel(view.pose_reference)}</small> : null}
                </button>
              );
            })}
            <button className="chipButton" type="button" onClick={() => {
              const view = nextView(cameraSources, views.length, selectedSource?.id);
              restoreViews([...viewsRef.current, view]);
              setSelectedId(view.id);
              setSolutions({});
            }}>Nova vista</button>
          </div>
          <div className="rowWrap" style={{ gap: 6 }}>
            {selectedSourceHasPtz ? <button className="chipButton" type="button" aria-expanded={isPtzControlsOpen} onClick={() => setIsPtzControlsOpen((value) => !value)}>{isPtzControlsOpen ? "Fechar PTZ" : "Posicionar PTZ"}</button> : null}
            <span className="cardMeta" role="status" aria-live="polite">{draftSaveState === "saving" ? "Salvando rascunho…" : "Rascunho salvo"}</span>
            <button className="chipButton" type="button" disabled={!undo.length} onClick={() => {
              const previous = undo[undo.length - 1];
              setRedo((history) => [...history, viewsRef.current.map(cloneView)]);
              setUndo((history) => history.slice(0, -1));
              setViews(previous.map(cloneView));
              setMessage("Última alteração desfeita. O rascunho anterior foi restaurado.");
            }}>Desfazer</button>
            <button className="chipButton" type="button" disabled={!redo.length} onClick={() => {
              const next = redo[redo.length - 1];
              setUndo((history) => [...history, viewsRef.current.map(cloneView)]);
              setRedo((history) => history.slice(0, -1));
              setViews(next.map(cloneView));
              setMessage("Alteração refeita. Recalculando a validação…");
            }}>Refazer</button>
            {canActivate ? <button className="primaryButton" type="button" onClick={activateCurrentMapping}>Ativar mapeamento</button> : null}
          </div>
        </div>

        <section className="card" style={{ margin: 0 }}>
          <div className="cardBody rowWrap" style={{ justifyContent: "space-between", gap: 10, padding: 12 }}>
            <div>
              <strong>{pendingImage ? "Complete este par" : isMarkingImage ? "Escolha o ponto na imagem" : pointsAreComplete ? "Confira o resultado" : "Marque pontos em comum"}</strong>
              <div className="cardMeta" role="status" aria-live="polite">{workflowInstruction}</div>
              <div className="cardMeta">{message}</div>
            </div>
            <div className="rowWrap" style={{ gap: 8 }}>
              <span className="cardMeta">{fitCount}/{REQUIRED_FIT_POINTS} ajuste · {checkCount}/{REQUIRED_CHECK_POINTS} conferência</span>
              {canAddPoint ? <button className="primaryButton" type="button" onClick={startMarking} disabled={snapshotState === "loading" || Boolean(pendingImage) || pointEditingBlocked}>{isMarkingImage ? "Clique na imagem" : `Marcar ${nextPointDescription}`}</button> : null}
            </div>
          </div>
        </section>

        {points.length ? <div className="cardMeta" style={{ paddingInline: 2 }}><strong>Ajustar:</strong> arraste qualquer marcador numerado na imagem ou na planta. O rascunho salva sozinho e Desfazer reverte o movimento.</div> : null}

        {legacyViews.length ? <div className="cardMeta">A calibração anterior continua ativa até você ativar um novo mapeamento validado.</div> : null}
        {!imageEditingReady ? <div className="cardMeta" role="status">{imageDiagnostic}</div> : null}

        {selectedSourceHasPtz && isPtzControlsOpen ? <section className="card" style={{ margin: 0 }}>
          <div className="cardBody" style={{ display: "flex", flexDirection: "column", gap: 10, padding: 12 }}>
            <div className="rowWrap" style={{ justifyContent: "space-between", gap: 8 }}>
              <div>
                <strong>Posicionar câmera PTZ</strong>
                <div className="cardMeta">Esta ação vale para {selectedView?.label ?? "esta vista"}. Cada toque usa um passo preciso e o quadro acompanha a câmera automaticamente.</div>
              </div>
              <span className="cardMeta">Posição atual · {ptzState ? ptzPositionLabel(ptzState, "telemetria indisponível") : "telemetria indisponível"}</span>
            </div>
            <div
              role="status"
              aria-live="polite"
              style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, minHeight: 34, padding: "7px 10px", border: "1px solid var(--color-border-subtle)", borderRadius: 9, background: "rgba(15, 23, 42, .34)" }}
            >
              {ptzFeedbackIsActive ? <i className="fa-solid fa-spinner fa-spin" aria-hidden="true" /> : <i className={`fa-solid ${ptzFeedbackState === "error" ? "fa-triangle-exclamation" : "fa-crosshairs"}`} aria-hidden="true" />}
              <strong style={{ fontSize: 12 }}>
                {ptzFeedbackState === "sending"
                  ? "Enviando movimento"
                  : ptzFeedbackState === "following"
                    ? "Câmera em movimento"
                    : ptzFeedbackState === "settling"
                      ? "Estabilizando imagem"
                      : ptzFeedbackState === "error"
                        ? "Atualização interrompida"
                        : "Câmera pronta"}
              </strong>
              <span className="cardMeta">
                {ptzFeedbackDetail || (
                  selectedView?.pose_reference
                    ? ptzCurrentMatchesView === true
                      ? "A câmera está na posição vinculada a esta vista."
                      : ptzCurrentMatchesView === false
                        ? "A câmera está fora da posição vinculada a esta vista. Volte para a vista antes de ajustar pontos."
                        : "Compare a posição atual com a posição mostrada no botão desta vista."
                    : "Posicione a câmera e vincule a posição quando o enquadramento estiver correto."
                )}
              </span>
              {ptzFeedbackIsActive && ptzFramesReceived > 0 ? <span className="cardMeta">{ptzFramesReceived} {ptzFramesReceived === 1 ? "quadro atualizado" : "quadros atualizados"}</span> : null}
            </div>
            <div className="rowWrap" style={{ gap: 8 }}>
              <button className="chipButton" type="button" onClick={goToSelectedView} disabled={!numericPose(selectedView?.pose_reference ?? null) || ptzCommandInFlight}>Ir para posição desta vista</button>
              <button className="chipButton" type="button" onClick={() => void capture(false)} disabled={!cameraId || snapshotState === "loading" || ptzFeedbackIsActive}>Atualizar agora</button>
              <button className="chipButton" type="button" onClick={() => void capture(true)} disabled={!cameraId || snapshotState === "loading" || ptzFeedbackIsActive}>Vincular posição atual</button>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(5, minmax(42px, 1fr))", gap: 5, maxWidth: 360 }} aria-label="Controles PTZ">
              <span /><button className="chipButton" type="button" disabled={ptzCommandInFlight} onClick={() => movePtz(0, PTZ_PAN_TILT_STEP, 0, "Movendo para cima")} aria-label="Mover para cima, passo preciso">↑</button><span /><button className="chipButton" type="button" disabled={ptzCommandInFlight} onClick={() => movePtz(0, 0, PTZ_ZOOM_STEP, "Aproximando")} aria-label="Aproximar, passo preciso">+</button><span />
              <button className="chipButton" type="button" disabled={ptzCommandInFlight} onClick={() => movePtz(-PTZ_PAN_TILT_STEP, 0, 0, "Movendo para a esquerda")} aria-label="Mover para a esquerda, passo preciso">←</button><span className="cardMeta" style={{ display: "grid", placeItems: "center", textAlign: "center", lineHeight: 1.1 }}>Passo<br />preciso</span><button className="chipButton" type="button" disabled={ptzCommandInFlight} onClick={() => movePtz(PTZ_PAN_TILT_STEP, 0, 0, "Movendo para a direita")} aria-label="Mover para a direita, passo preciso">→</button><button className="chipButton" type="button" disabled={ptzCommandInFlight} onClick={() => movePtz(0, 0, -PTZ_ZOOM_STEP, "Afastando")} aria-label="Afastar, passo preciso">−</button><span />
              <span /><button className="chipButton" type="button" disabled={ptzCommandInFlight} onClick={() => movePtz(0, -PTZ_PAN_TILT_STEP, 0, "Movendo para baixo")} aria-label="Mover para baixo, passo preciso">↓</button><span /><span /><span />
            </div>
          </div>
        </section> : null}

        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 }}>
          <section className="card" style={{ margin: 0, overflow: "hidden", display: "flex", flexDirection: "column", minWidth: 0 }}>
            <div className="cardBody rowWrap" style={{ padding: 10, borderBottom: "1px solid var(--color-border-subtle)", justifyContent: "space-between" }}>
              <strong>1. Imagem atual</strong>
              <span className="cardMeta" role="status" aria-live="polite">
                {ptzFeedbackIsActive
                  ? `${ptzFeedbackState === "sending" ? "Enviando movimento" : ptzFeedbackState === "settling" ? "Estabilizando" : "Acompanhando PTZ"}${ptzFramesReceived ? ` · quadro ${ptzFramesReceived}` : ""}`
                  : snapshotState === "loading"
                    ? "Atualizando…"
                    : isMarkingImage
                      ? "Escolha um marco"
                      : ""}
              </span>
            </div>
            <div style={{ position: "relative", aspectRatio: "16 / 9", minHeight: 260, background: "#020617", overflow: "hidden", cursor: pointEditingBlocked ? "not-allowed" : dragTarget?.side === "image" ? "grabbing" : isMarkingImage ? "crosshair" : undefined, touchAction: "none", userSelect: "none" }} onPointerDown={onImagePointerDown} onPointerMove={onImagePointerMove} onPointerUp={onImagePointerUp} onPointerCancel={onImagePointerUp}>
              {snapshotUrl ? <img key={snapshotUrl} ref={snapshotImageRef} src={snapshotUrl} alt={t("ext.cameras.ground_lens.captured_image")} draggable={false} onLoad={(event) => {
                const image = event.currentTarget;
                if (image !== snapshotImageRef.current || snapshotRequestContextRef.current !== snapshotContext) return;
                const size = { width: image.naturalWidth, height: image.naturalHeight };
                if (!image.complete || groundImageResolutionStatus(size, size) !== "match") {
                  setLoadedImage(null);
                  setSnapshotState("error");
                  return;
                }
                setLoadedImage({ ...size, url: snapshotUrl, context: snapshotContext });
                setSnapshotState("ready");
                setImageLayoutRevision((value) => value + 1);
              }} onError={(event) => {
                if (event.currentTarget !== snapshotImageRef.current) return;
                setLoadedImage(null);
                setSnapshotState("error");
                setSnapshotUrl(null);
                setMessage(t("ext.cameras.ground_lens.image_decode_failed"));
              }} style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }} /> : null}
              {ptzFeedbackIsActive ? <div style={{ position: "absolute", top: 10, right: 10, zIndex: 3, display: "flex", alignItems: "center", gap: 7, padding: "6px 9px", borderRadius: 999, background: "rgba(2, 6, 23, .82)", border: "1px solid rgba(255, 255, 255, .22)", color: "white", fontSize: 11, pointerEvents: "none" }}>
                <i className="fa-solid fa-spinner fa-spin" aria-hidden="true" />
                <span>{ptzFeedbackState === "sending" ? "Enviando movimento" : ptzFeedbackState === "settling" ? "Estabilizando imagem" : "Atualizando com a câmera"}</span>
              </div> : null}
              {(imageEditingReady ? points : []).map((point, index) => (
                <span key={point.id} title={pointEditingBlocked ? `Aguarde para ajustar o ponto ${pointLabel(index)}` : `Arraste para ajustar o ponto ${pointLabel(index)} na imagem`} style={{ position: "absolute", left: `${(renderedImage.left + point.image.x * renderedImage.width) * 100}%`, top: `${(renderedImage.top + point.image.y * renderedImage.height) * 100}%`, transform: "translate(-50%, -50%)", width: 30, height: 30, borderRadius: point.role === "check" ? 5 : 999, background: pointColor(index), border: "2px solid #0f172a", boxShadow: dragTarget?.side === "image" && dragTarget.id === point.id ? "0 0 0 3px rgba(255,255,255,.95), 0 4px 14px rgba(0,0,0,.45)" : "0 0 0 2px rgba(255,255,255,.5), 0 3px 10px rgba(0,0,0,.35)", color: "#0f172a", fontWeight: 800, fontSize: 12, display: "grid", placeItems: "center", cursor: pointEditingBlocked ? "not-allowed" : dragTarget?.side === "image" && dragTarget.id === point.id ? "grabbing" : "grab", touchAction: "none" }}>{pointLabel(index)}</span>
              ))}
              {pendingImage ? <span style={{ position: "absolute", left: `${(renderedImage.left + pendingImage.x * renderedImage.width) * 100}%`, top: `${(renderedImage.top + pendingImage.y * renderedImage.height) * 100}%`, transform: "translate(-50%, -50%)", width: 24, height: 24, borderRadius: 999, border: "2px dashed white", pointerEvents: "none" }} /> : null}
              {!snapshotUrl ? <div className="cardMeta" style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", alignContent: "center", gap: 8, textAlign: "center", padding: 16 }}>
                <span>{snapshotState === "loading" ? "Capturando quadro atual…" : snapshotState === "error" ? "Não foi possível carregar o quadro." : "Preparando a fonte da câmera…"}</span>
                {snapshotState !== "loading" && cameraId && selectedSource ? <button className="chipButton" type="button" onPointerDown={(event) => event.stopPropagation()} onClick={() => void capture(false)}>Tentar novamente</button> : null}
              </div> : null}
            </div>
          </section>

          <section className="card" style={{ margin: 0, overflow: "hidden", display: "flex", flexDirection: "column", minWidth: 0 }}>
            <div className="cardBody rowWrap" style={{ padding: 10, borderBottom: "1px solid var(--color-border-subtle)", justifyContent: "space-between", gap: 6 }}>
              <strong>2. Mesmo ponto na planta</strong>
              <div className="rowWrap" style={{ gap: 4 }} aria-label="Orientação visual da planta">
                <button className="iconButton" type="button" onClick={() => rotatePlant(-1)} aria-label="Girar planta 90 graus para a esquerda" title="Girar planta 90° para a esquerda">↶</button>
                <span className="cardMeta" aria-label={`Planta em ${selectedViewRotation} graus`}>{selectedViewRotation}°</span>
                <button className="iconButton" type="button" onClick={() => rotatePlant(1)} aria-label="Girar planta 90 graus para a direita" title="Girar planta 90° para a direita">↷</button>
                <button className="chipButton" type="button" onClick={() => setPlanScope((scope) => scope === "camera" ? "content" : "camera")}>{planScope === "camera" ? "Planta inteira" : "Focar câmera"}</button>
              </div>
            </div>
            <div style={{ aspectRatio: "16 / 9", minHeight: 260 }}>
              <host.ui.Viewport2DReplica
                initialFit={planScope === "content" ? "content" : undefined}
                initialCenter={planScope === "camera" ? { x: element.position.x, z: element.position.z } : undefined}
                initialScale={planScope === "camera" ? 34 : undefined}
                interactionMode="navigate"
                minScale={4}
                displayRotationDegrees={selectedViewRotation}
                session={session}
                style={{ width: "100%", height: "100%" }}
              />
            </div>
          </section>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: 10 }}>
          <section className="card" style={{ margin: 0 }}>
            <div className="cardBody" style={{ display: "flex", flexDirection: "column", gap: 8, padding: 12 }}>
              <strong>Vista</strong>
              <label className="label" htmlFor="mapping-view-label">Nome</label>
              <input id="mapping-view-label" className="input" value={selectedView?.label ?? ""} onChange={(event) => updateSelected((view) => ({ ...view, label: event.target.value }))} />
              <label className="label" htmlFor="mapping-source">Fonte física</label>
              <select id="mapping-source" className="input" value={selectedSource?.id ?? ""} onChange={(event) => selectPhysicalSource(event.target.value)} disabled={!sourceCanChange}>
                {videoSources.map((source) => <option key={source.id} value={source.id}>{source.name || source.id}{sourceHasPtz(source) ? " · PTZ" : ""}</option>)}
              </select>
              <button className="chipButton" type="button" onClick={() => void capture(false)} disabled={!cameraId || !selectedSource || snapshotState === "loading"}>{snapshotUrl ? "Atualizar quadro" : "Carregar quadro"}</button>
              {!sourceCanChange ? <div className="cardMeta">A fonte fica fixa depois do primeiro ponto. Crie outra vista para outra ótica.</div> : null}
            </div>
          </section>

          {selectedView ? <CameraGroundLensEditor
            key={`${selectedView.id}-${JSON.stringify([selectedView.projection_model.lens, selectedView.projection_model.source_geometry])}`}
            view={selectedView} imageSize={imageSize} i18n={i18n}
            onDirty={() => {
              setLensEditingId(selectedView.id);
              updateSelected((view) => ({ ...view, projection_quality: { status: "incomplete", estimated: false } }));
            }}
            onApply={(draft) => {
              updateSelected((view) => applyGroundLensDraft(view, draft) ?? view);
              setLensEditingId(null);
            }}
          /> : null}
          <section className="card" style={{ margin: 0 }}>
            <div className="cardBody" style={{ display: "flex", flexDirection: "column", gap: 8, padding: 12 }}>
              <strong>Validação</strong>
              <div className="cardMeta">{t("ext.cameras.ground_lens.ground_status")}: {t(imageEditingReady && selectedSolution?.accepted ? "ext.cameras.ground_lens.ready" : "ext.cameras.ground_lens.unavailable")}</div>
              <div className="cardMeta" role="status">{t("ext.cameras.ground_lens.metric_status")}: {t(imageEditingReady && selectedSolution?.metric_geometry?.status === "ready" ? "ext.cameras.ground_lens.ready" : "ext.cameras.ground_lens.unavailable")}</div>
              {selectedSolution?.metric_geometry?.reason ? <div className="cardMeta">{t("ext.cameras.ground_lens.metric_reason")}: <code>{selectedSolution.metric_geometry.reason}</code></div> : null}
              <div className="cardMeta">Cobertura: {selectedSolution ? `${Math.round(selectedSolution.quality.image_hull_area_ratio_uv * 100)}%` : "ainda não calculada"}</div>
              <div className="cardMeta">{canActivate ? "Pronta para ativar." : fitCount < REQUIRED_FIT_POINTS ? "Faltam pontos de ajuste." : checkCount < REQUIRED_CHECK_POINTS ? "Faltam pontos de conferência." : failedCheckIndexes.length ? `Refaça ou ajuste a${failedCheckIndexes.length === 1 ? " conferência" : "s conferências"} ${failedCheckLabel}.` : "Aguarde o resultado da validação."}</div>
              {selectedSourceHasPtz && !poseIsBound ? <div className="cardMeta">Antes de ativar: vincule uma posição PTZ estável.</div> : null}
              {checkCount > 0 ? <div className="cardMeta">Os pontos de conferência são quadrados: eles não mudam o mapa, apenas medem o erro.</div> : null}
              {checkErrors.map((error, index) => <div className="cardMeta" key={`check-${index}`}>Conferência {index + 1}: {Number.isFinite(error) ? `${error.toFixed(2)} m${error <= MAX_CHECK_ERROR_METERS ? " · dentro do limite" : ` · refaça (limite ${MAX_CHECK_ERROR_METERS.toFixed(2)} m)`}` : "inválida · refaça"}</div>)}
            </div>
          </section>

          <section className="card" style={{ margin: 0 }}>
            <div className="cardBody" style={{ display: "flex", flexDirection: "column", gap: 6, padding: 12 }}>
              <strong>Pontos marcados</strong>
              {points.length ? points.map((point, index) => <div key={point.id} className="rowWrap" style={{ justifyContent: "space-between", gap: 6 }}><span className="cardMeta"><span style={{ color: pointColor(index), fontWeight: 800 }}>{pointLabel(index)}</span> · {point.role === "fit" ? "ajuste" : `conferência ${index - fitCount + 1}`}</span><button className="iconButton" type="button" aria-label={`Remover ponto ${pointLabel(index)}`} onClick={() => {
                setPendingImage(null);
                setIsMarkingImage(false);
                updateSelected((view) => ({ ...view, projection_model: { ...view.projection_model, correspondences: view.projection_model.correspondences.filter((candidate) => candidate.id !== point.id) } }));
                setMessage(`Ponto ${pointLabel(index)} removido. Marque novamente o mesmo marco na imagem e na planta.`);
              }}><i className="fa-solid fa-trash" aria-hidden="true" /></button></div>) : <div className="cardMeta">Nenhum ponto ainda.</div>}
            </div>
          </section>

        </div>

        <div className="rowWrap" style={{ justifyContent: "space-between" }}>
          <span className="cardMeta">A imagem não é deformada. O mapa só é ativado depois de validado.</span>
          <button className="chipButton" type="button" onClick={onClose}>{t("core.actions.close")}</button>
        </div>
      </div>
    </SubModal>
  );
}
