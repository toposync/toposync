import React, { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  getStreamingTransmissionCameraPresets,
  getStreamingTransmissionCameraStatus,
  gotoStreamingTransmissionCameraPreset,
  moveStreamingTransmissionCamera,
  stopStreamingTransmissionCamera,
  type StreamingTransmissionCameraPreset,
  type StreamingTransmissionCameraStatus,
} from "../../util/api";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";

type Props = {
  open: boolean;
  transmissionId: string;
  label: string;
  onClose: () => void;
};

type MoveVector = { pan: number; tilt: number; zoom: number };

const MOVE_REPEAT_MS = 260;
const MOVE_TIMEOUT_S = 0.8;
const DEFAULT_PAN_SPEED = 0.55;
const DEFAULT_TILT_SPEED = 0.55;
const DEFAULT_ZOOM_SPEED = 0.65;
const PRESET_MATCH_PAN_TILT_EPS = 0.02;
const PRESET_MATCH_ZOOM_EPS = 0.03;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function getFullscreenElement(): Element | null {
  if (typeof document === "undefined") return null;
  const anyDoc = document as any;
  return anyDoc.fullscreenElement || anyDoc.webkitFullscreenElement || anyDoc.mozFullScreenElement || anyDoc.msFullscreenElement || null;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!target || !(target instanceof Element)) return false;
  const el = target as Element;
  const tag = el.tagName.toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return Boolean((el as any).isContentEditable);
}

function asErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || i18n.t("core.ui.streams.error_unknown", {}, "Unknown error"));
}

function pickMatchingPresetToken(
  presets: StreamingTransmissionCameraPreset[],
  status: StreamingTransmissionCameraStatus | null,
): string | null {
  if (!status) return null;
  const pan = typeof status.pan === "number" ? status.pan : null;
  const tilt = typeof status.tilt === "number" ? status.tilt : null;
  const zoom = typeof status.zoom === "number" ? status.zoom : null;
  if (pan === null || tilt === null) return null;

  let best: { token: string; dist: number } | null = null;
  for (const preset of presets) {
    const pPan = typeof preset.pan === "number" ? preset.pan : null;
    const pTilt = typeof preset.tilt === "number" ? preset.tilt : null;
    if (pPan === null || pTilt === null) continue;
    const pZoom = typeof preset.zoom === "number" ? preset.zoom : null;
    const dz = zoom !== null && pZoom !== null ? (zoom - pZoom) ** 2 : 0;
    const dist = (pan - pPan) ** 2 + (tilt - pTilt) ** 2 + dz;
    if (!best || dist < best.dist) best = { token: String(preset.token || "").trim(), dist };
  }
  const token = best?.token || "";
  if (!token) return null;

  const matchedPreset = presets.find((p) => String(p.token || "").trim() === token) ?? null;
  if (!matchedPreset) return null;
  const pPan = typeof matchedPreset.pan === "number" ? matchedPreset.pan : null;
  const pTilt = typeof matchedPreset.tilt === "number" ? matchedPreset.tilt : null;
  if (pPan === null || pTilt === null) return null;

  const dp = Math.abs(pan - pPan);
  const dt = Math.abs(tilt - pTilt);
  if (dp > PRESET_MATCH_PAN_TILT_EPS || dt > PRESET_MATCH_PAN_TILT_EPS) return null;

  const pZoom = typeof matchedPreset.zoom === "number" ? matchedPreset.zoom : null;
  if (zoom !== null && pZoom !== null) {
    const dz = Math.abs(zoom - pZoom);
    if (dz > PRESET_MATCH_ZOOM_EPS) return null;
  }

  return token;
}

export function StreamsPtzOverlay({ open, transmissionId, label, onClose }: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const lastTransmissionIdRef = useRef<string>("");
  const [portalRoot, setPortalRoot] = useState<HTMLElement | null>(null);

  const [position, setPosition] = useState<{ x: number; y: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{
    pointerId: number;
    offsetX: number;
    offsetY: number;
    width: number;
    height: number;
  } | null>(null);

  const [presets, setPresets] = useState<StreamingTransmissionCameraPreset[]>([]);
  const [presetsLoading, setPresetsLoading] = useState(false);
  const [presetsError, setPresetsError] = useState<string | null>(null);

  const [status, setStatus] = useState<StreamingTransmissionCameraStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusError, setStatusError] = useState<string | null>(null);

  const [selectedPresetToken, setSelectedPresetToken] = useState("");
  const [commandBusy, setCommandBusy] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [activeMoveId, setActiveMoveId] = useState<string | null>(null);

  const moveVectorRef = useRef<MoveVector | null>(null);
  const moveHeldRef = useRef(false);
  const moveTimerRef = useRef<number | null>(null);
  const moveInFlightRef = useRef(false);
  const stopInFlightRef = useRef(false);

  const ptzEnabled = open && Boolean(transmissionId.trim());

  useEffect(() => {
    if (typeof document === "undefined") return;

    const resolveRoot = (): HTMLElement => {
      const fs = getFullscreenElement();
      if (fs && fs instanceof HTMLElement) return fs;
      return document.body;
    };

    const update = () => setPortalRoot(resolveRoot());
    update();

    document.addEventListener("fullscreenchange", update);
    // Safari legacy.
    document.addEventListener("webkitfullscreenchange" as any, update);
    return () => {
      document.removeEventListener("fullscreenchange", update);
      document.removeEventListener("webkitfullscreenchange" as any, update);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const tid = transmissionId.trim();
    if (tid) lastTransmissionIdRef.current = tid;
  }, [open, transmissionId]);

  const overlayStyle = useMemo(() => {
    if (!position) return undefined;
    return { left: `${position.x}px`, top: `${position.y}px`, transform: "none" as const };
  }, [position]);

  const stopMove = async (): Promise<void> => {
    const shouldStop =
      moveHeldRef.current || moveVectorRef.current !== null || moveTimerRef.current !== null || activeMoveId !== null;
    setActiveMoveId(null);
    moveHeldRef.current = false;
    moveVectorRef.current = null;
    if (moveTimerRef.current !== null) {
      window.clearInterval(moveTimerRef.current);
      moveTimerRef.current = null;
    }
    if (!shouldStop) return;
    const tid = lastTransmissionIdRef.current || transmissionId.trim();
    if (!tid) return;
    if (stopInFlightRef.current) return;
    stopInFlightRef.current = true;
    try {
      await stopStreamingTransmissionCamera(tid, { pan_tilt: true, zoom: true });
    } catch (error) {
      // Avoid spamming errors for a best-effort stop.
      setCommandError((prev) => prev || asErrorMessage(error));
    } finally {
      stopInFlightRef.current = false;
    }
  };

  const sendMove = async (): Promise<void> => {
    if (!ptzEnabled) return;
    const tid = transmissionId.trim();
    if (!tid) return;
    const vec = moveVectorRef.current;
    if (!vec || !moveHeldRef.current) return;
    if (moveInFlightRef.current) return;

    moveInFlightRef.current = true;
    try {
      await moveStreamingTransmissionCamera(tid, {
        pan: clamp(vec.pan, -1, 1),
        tilt: clamp(vec.tilt, -1, 1),
        zoom: clamp(vec.zoom, -1, 1),
        timeout_s: MOVE_TIMEOUT_S,
      });
      setCommandError(null);
    } catch (error) {
      setCommandError(asErrorMessage(error));
    } finally {
      moveInFlightRef.current = false;
    }
  };

  const startMove = (vec: MoveVector, moveId: string | null): void => {
    if (!ptzEnabled) return;
    setActiveMoveId(moveId);
    moveHeldRef.current = true;
    moveVectorRef.current = vec;
    void sendMove();
    if (moveTimerRef.current === null) {
      moveTimerRef.current = window.setInterval(() => {
        if (!moveHeldRef.current || !moveVectorRef.current) return;
        void sendMove();
      }, MOVE_REPEAT_MS);
    }
  };

  useEffect(() => {
    if (!open) return;
    setPosition(null);
    setDragging(false);
    setCommandError(null);
    setSelectedPresetToken("");
    rootRef.current?.focus?.();
  }, [open, transmissionId]);

  useEffect(() => {
    if (!open || !transmissionId.trim()) return;
    let cancelled = false;

    setPresetsLoading(true);
    setPresetsError(null);
    setPresets([]);
    setStatusLoading(true);
    setStatusError(null);
    setStatus(null);

    void (async () => {
      try {
        const [presetsRes, statusRes] = await Promise.allSettled([
          getStreamingTransmissionCameraPresets(transmissionId),
          getStreamingTransmissionCameraStatus(transmissionId),
        ]);

        if (cancelled) return;

        const nextPresets =
          presetsRes.status === "fulfilled" && Array.isArray(presetsRes.value.presets) ? presetsRes.value.presets : [];
        setPresets(nextPresets);
        if (presetsRes.status === "rejected") setPresetsError(asErrorMessage(presetsRes.reason));

        const nextStatus =
          statusRes.status === "fulfilled" && statusRes.value && typeof statusRes.value.status === "object"
            ? statusRes.value.status
            : null;
        setStatus(nextStatus);
        if (statusRes.status === "rejected") setStatusError(asErrorMessage(statusRes.reason));

        const selected = pickMatchingPresetToken(nextPresets, nextStatus);
        setSelectedPresetToken(selected || "");
      } finally {
        if (!cancelled) {
          setPresetsLoading(false);
          setStatusLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, transmissionId]);

  useEffect(() => {
    if (open) return;
    void stopMove();
  }, [open]);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (!ptzEnabled) return;
      if (event.defaultPrevented) return;
      if (isEditableTarget(event.target)) return;

      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      const key = event.key;
      if (key !== "ArrowUp" && key !== "ArrowDown" && key !== "ArrowLeft" && key !== "ArrowRight") return;
      if (event.repeat) return;

      event.preventDefault();
      if (key === "ArrowUp") startMove({ pan: 0, tilt: DEFAULT_TILT_SPEED, zoom: 0 }, "up");
      if (key === "ArrowDown") startMove({ pan: 0, tilt: -DEFAULT_TILT_SPEED, zoom: 0 }, "down");
      if (key === "ArrowLeft") startMove({ pan: -DEFAULT_PAN_SPEED, tilt: 0, zoom: 0 }, "left");
      if (key === "ArrowRight") startMove({ pan: DEFAULT_PAN_SPEED, tilt: 0, zoom: 0 }, "right");
    };

    const onKeyUp = (event: KeyboardEvent) => {
      if (!ptzEnabled) return;
      const key = event.key;
      if (key !== "ArrowUp" && key !== "ArrowDown" && key !== "ArrowLeft" && key !== "ArrowRight") return;
      event.preventDefault();
      void stopMove();
    };

    const onWindowBlur = () => {
      void stopMove();
    };

    const onVisibility = () => {
      if (document.visibilityState !== "visible") void stopMove();
    };

    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("blur", onWindowBlur);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
      window.removeEventListener("blur", onWindowBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      void stopMove();
    };
  }, [open, onClose, ptzEnabled, transmissionId]);

  if (!open) return null;

  const headerTitle = label || transmissionId;
  const presetOptions = presets.filter((p) => String(p.token || "").trim());

  const handlePresetChange = async (token: string) => {
    const next = String(token || "").trim();
    setSelectedPresetToken(next);
    if (!ptzEnabled || !next) return;

    setCommandBusy(true);
    setCommandError(null);
    try {
      await gotoStreamingTransmissionCameraPreset(transmissionId, next);
      setCommandError(null);
      try {
        const refreshed = await getStreamingTransmissionCameraStatus(transmissionId);
        setStatus(refreshed.status ?? null);
      } catch {
        // ignore
      }
    } catch (error) {
      setCommandError(asErrorMessage(error));
    } finally {
      setCommandBusy(false);
    }
  };

  const handleDragStart = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!rootRef.current) return;
    if (event.button !== 0) return;
    if (isEditableTarget(event.target)) return;
    const target = event.target as Element | null;
    if (target && target.closest("button, a, select, input, textarea")) return;
    const rect = rootRef.current.getBoundingClientRect();
    const pointerId = event.pointerId;
    try {
      event.currentTarget.setPointerCapture(pointerId);
    } catch {
      // ignore
    }

    dragRef.current = {
      pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
      width: rect.width,
      height: rect.height,
    };
    setDragging(true);
    setPosition({ x: rect.left, y: rect.top });
  };

  const handleDragMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (event.pointerId !== drag.pointerId) return;

    const margin = 10;
    const maxX = Math.max(margin, window.innerWidth - margin - drag.width);
    const maxY = Math.max(margin, window.innerHeight - margin - drag.height);
    const x = clamp(event.clientX - drag.offsetX, margin, maxX);
    const y = clamp(event.clientY - drag.offsetY, margin, maxY);
    setPosition({ x, y });
  };

  const handleDragEnd = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (event.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // ignore
    }
  };

  const makeMoveHandlers = (vec: MoveVector, moveId: string) => ({
    onPointerDown: (event: React.PointerEvent<HTMLButtonElement>) => {
      if (!ptzEnabled) return;
      event.preventDefault();
      try {
        event.currentTarget.setPointerCapture(event.pointerId);
      } catch {
        // ignore
      }
      startMove(vec, moveId);
    },
    onPointerUp: (event: React.PointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      void stopMove();
      try {
        event.currentTarget.releasePointerCapture(event.pointerId);
      } catch {
        // ignore
      }
    },
    onPointerCancel: () => {
      void stopMove();
    },
    onLostPointerCapture: () => {
      void stopMove();
    },
  });

  const padButtons = [
    { id: "up", label: t("core.ui.streams.ptz.up", {}, "Up"), icon: "chevron-up", vec: { pan: 0, tilt: DEFAULT_TILT_SPEED, zoom: 0 } },
    { id: "left", label: t("core.ui.streams.ptz.left", {}, "Left"), icon: "chevron-left", vec: { pan: -DEFAULT_PAN_SPEED, tilt: 0, zoom: 0 } },
    { id: "right", label: t("core.ui.streams.ptz.right", {}, "Right"), icon: "chevron-right", vec: { pan: DEFAULT_PAN_SPEED, tilt: 0, zoom: 0 } },
    { id: "down", label: t("core.ui.streams.ptz.down", {}, "Down"), icon: "chevron-down", vec: { pan: 0, tilt: -DEFAULT_TILT_SPEED, zoom: 0 } },
    { id: "zoom_in", label: t("core.ui.streams.ptz.zoom_in", {}, "Zoom in"), icon: "plus", vec: { pan: 0, tilt: 0, zoom: DEFAULT_ZOOM_SPEED } },
    { id: "zoom_out", label: t("core.ui.streams.ptz.zoom_out", {}, "Zoom out"), icon: "minus", vec: { pan: 0, tilt: 0, zoom: -DEFAULT_ZOOM_SPEED } },
  ];

  const content = (
    <div
      ref={rootRef}
      className={["streamsPtzOverlay", dragging ? "isDragging" : ""].filter(Boolean).join(" ")}
      role="dialog"
      aria-label={t("core.ui.streams.ptz.title", {}, "Camera controls")}
      tabIndex={-1}
      style={overlayStyle as any}
    >
      <div
        className="streamsPtzHeader"
        onPointerDown={handleDragStart}
        onPointerMove={handleDragMove}
        onPointerUp={handleDragEnd}
        onPointerCancel={handleDragEnd}
      >
        <div className="streamsPtzTitle">{headerTitle}</div>
        <button
          type="button"
          className="iconButton streamsPtzCloseButton"
          aria-label={t("core.actions.close", {}, "Close")}
          title={t("core.actions.close", {}, "Close")}
          onClick={() => onClose()}
        >
          <Icon name="xmark" />
        </button>
      </div>

      <div className="streamsPtzBody">
        <div className="streamsPtzPad">
          <div />
          <button
            type="button"
            className={["iconButton", "streamsPtzPadButton", activeMoveId === "up" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-label={padButtons[0].label}
            title={padButtons[0].label}
            {...makeMoveHandlers(padButtons[0].vec, "up")}
          >
            <Icon name={padButtons[0].icon} />
          </button>
          <div />

          <button
            type="button"
            className={["iconButton", "streamsPtzPadButton", activeMoveId === "left" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-label={padButtons[1].label}
            title={padButtons[1].label}
            {...makeMoveHandlers(padButtons[1].vec, "left")}
          >
            <Icon name={padButtons[1].icon} />
          </button>

          <button
            type="button"
            className="iconButton streamsPtzPadButton streamsPtzPadCenter"
            aria-label={t("core.ui.streams.ptz.stop", {}, "Stop")}
            title={t("core.ui.streams.ptz.stop", {}, "Stop")}
            onClick={() => void stopMove()}
          >
            <Icon name="hand" />
          </button>

          <button
            type="button"
            className={["iconButton", "streamsPtzPadButton", activeMoveId === "right" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-label={padButtons[2].label}
            title={padButtons[2].label}
            {...makeMoveHandlers(padButtons[2].vec, "right")}
          >
            <Icon name={padButtons[2].icon} />
          </button>

          <div />
          <button
            type="button"
            className={["iconButton", "streamsPtzPadButton", activeMoveId === "down" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-label={padButtons[3].label}
            title={padButtons[3].label}
            {...makeMoveHandlers(padButtons[3].vec, "down")}
          >
            <Icon name={padButtons[3].icon} />
          </button>
          <div />
        </div>

        <div className="streamsPtzSide">
          <div className="streamsPtzFieldLabel">{t("core.ui.streams.ptz.preset", {}, "Preset")}</div>
          <select
            className="input streamsPtzSelect"
            value={selectedPresetToken}
            disabled={presetsLoading || commandBusy || presetOptions.length === 0}
            onChange={(event) => void handlePresetChange(event.target.value)}
          >
            {presetOptions.length > 0 ? (
              <option value="">
                {t("core.ui.streams.ptz.custom_position", {}, "Current position (custom)")}
              </option>
            ) : null}
            {presetOptions.length === 0 ? (
              <option value="">
                {presetsLoading
                  ? t("core.ui.streams.ptz.loading", {}, "Loading…")
                  : t("core.ui.streams.ptz.no_presets", {}, "No presets")}
              </option>
            ) : null}
            {presetOptions.map((preset) => {
              const token = String(preset.token || "").trim();
              const name = String(preset.name || "").trim();
              return (
                <option key={token} value={token}>
                  {name || token}
                </option>
              );
            })}
          </select>

          <div className="streamsPtzZoomRow">
            <button
              type="button"
              className={["iconButton", "streamsPtzZoomButton", activeMoveId === "zoom_in" ? "isActive" : ""].filter(Boolean).join(" ")}
              aria-label={padButtons[4].label}
              title={padButtons[4].label}
              {...makeMoveHandlers(padButtons[4].vec, "zoom_in")}
            >
              <Icon name={padButtons[4].icon} />
            </button>
            <button
              type="button"
              className={["iconButton", "streamsPtzZoomButton", activeMoveId === "zoom_out" ? "isActive" : ""].filter(Boolean).join(" ")}
              aria-label={padButtons[5].label}
              title={padButtons[5].label}
              {...makeMoveHandlers(padButtons[5].vec, "zoom_out")}
            >
              <Icon name={padButtons[5].icon} />
            </button>
          </div>

          <div className="streamsPtzMeta">
            {statusLoading ? t("core.ui.streams.ptz.status_loading", {}, "Loading camera status…") : null}
            {!statusLoading && (presetsError || statusError || commandError) ? (
              <span className="streamsPtzError">{presetsError || statusError || commandError}</span>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );

  // When a tile is fullscreen, only that element's subtree is visible. Render within it.
  return portalRoot ? createPortal(content, portalRoot) : content;
}
