import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchCameraSnapshot, fetchRtspSnapshot } from "../../../../../util/api";
import { i18n } from "../../../../../util/i18n";
import { Modal } from "../../../../Modal";

export type SnapshotSource =
  | { kind: "camera"; cameraId: string }
  | { kind: "rtsp"; url: string; username?: string; password?: string };

type SnapshotState = {
  url: string | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
};

function useSnapshotObjectUrl(open: boolean, source: SnapshotSource | null): SnapshotState {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => setNonce((prev) => prev + 1), []);

  useEffect(() => {
    return () => {
      setUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
    };
  }, []);

  useEffect(() => {
    if (!open || !source) {
      setLoading(false);
      setError(null);
      setUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    const load = async () => {
      const blob =
        source.kind === "camera"
          ? await fetchCameraSnapshot(source.cameraId, controller.signal)
          : await fetchRtspSnapshot(
              { url: source.url, username: source.username, password: source.password },
              controller.signal,
            );
      if (cancelled) return;
      const nextUrl = URL.createObjectURL(blob);
      setUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return nextUrl;
      });
    };

    load()
      .catch((err: any) => {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return null;
        });
        setError(String(err?.message ?? err));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [open, source?.kind, (source as any)?.cameraId, (source as any)?.url, (source as any)?.username, (source as any)?.password, nonce]);

  return { url, loading, error, refresh };
}

type ImageDims = { width: number; height: number };

type Point01 = { x: number; y: number };

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function roundToStep(value: number, step: number): number {
  const s = Number.isFinite(step) && step > 0 ? step : 1;
  return Math.round(value / s) * s;
}

function point01FromPointerEvent(event: React.PointerEvent, rect: DOMRect): Point01 {
  const x = clamp01((event.clientX - rect.left) / Math.max(1, rect.width));
  const y = clamp01((event.clientY - rect.top) / Math.max(1, rect.height));
  return { x, y };
}

type CropRectValues = { left: number; top: number; right: number; bottom: number };
type Rect01 = { x1: number; y1: number; x2: number; y2: number };

function normalizeRect01(rect: Rect01): Rect01 {
  const x1 = clamp01(rect.x1);
  const y1 = clamp01(rect.y1);
  const x2 = clamp01(rect.x2);
  const y2 = clamp01(rect.y2);
  return {
    x1: Math.min(x1, x2),
    y1: Math.min(y1, y2),
    x2: Math.max(x1, x2),
    y2: Math.max(y1, y2),
  };
}

function rect01FromValues(values: CropRectValues, units: "percent" | "pixels", dims: ImageDims): Rect01 {
  const left = Number(values.left);
  const top = Number(values.top);
  const right = Number(values.right);
  const bottom = Number(values.bottom);

  if (units === "pixels") {
    return normalizeRect01({
      x1: Number.isFinite(left) ? left / Math.max(1, dims.width) : 0,
      y1: Number.isFinite(top) ? top / Math.max(1, dims.height) : 0,
      x2: Number.isFinite(right) ? right / Math.max(1, dims.width) : 1,
      y2: Number.isFinite(bottom) ? bottom / Math.max(1, dims.height) : 1,
    });
  }

  return normalizeRect01({
    x1: Number.isFinite(left) ? left / 100 : 0,
    y1: Number.isFinite(top) ? top / 100 : 0,
    x2: Number.isFinite(right) ? right / 100 : 1,
    y2: Number.isFinite(bottom) ? bottom / 100 : 1,
  });
}

function valuesFromRect01(rect01: Rect01, units: "percent" | "pixels", dims: ImageDims): CropRectValues {
  const rect = normalizeRect01(rect01);
  if (units === "pixels") {
    return {
      left: Math.max(0, roundToStep(rect.x1 * dims.width, 1)),
      top: Math.max(0, roundToStep(rect.y1 * dims.height, 1)),
      right: Math.max(0, roundToStep(rect.x2 * dims.width, 1)),
      bottom: Math.max(0, roundToStep(rect.y2 * dims.height, 1)),
    };
  }
  return {
    left: roundToStep(rect.x1 * 100, 0.5),
    top: roundToStep(rect.y1 * 100, 0.5),
    right: roundToStep(rect.x2 * 100, 0.5),
    bottom: roundToStep(rect.y2 * 100, 0.5),
  };
}

type CropModalProps = {
  open: boolean;
  onClose: () => void;
  snapshotSource: SnapshotSource | null;
  units: "percent" | "pixels";
  values: CropRectValues;
  onChange: (values: CropRectValues) => void;
};

export function CropRectangleDrawModal({
  open,
  onClose,
  snapshotSource,
  units,
  values,
  onChange,
}: CropModalProps): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const snapshot = useSnapshotObjectUrl(open, snapshotSource);
  const [dims, setDims] = useState<ImageDims | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);

  const [rect01, setRect01] = useState<Rect01>({ x1: 0, y1: 0, x2: 1, y2: 1 });
  const initializedRef = useRef(false);

  const pendingRectRef = useRef<Rect01 | null>(null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!open) {
      setDims(null);
      initializedRef.current = false;
      return;
    }
  }, [open]);

  useEffect(() => {
    if (!open || !dims) return;
    if (initializedRef.current) return;
    initializedRef.current = true;
    setRect01(rect01FromValues(values, units, dims));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, dims?.width, dims?.height]);

  useEffect(() => {
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      pendingRectRef.current = null;
    };
  }, []);

  const commitRect = useCallback(
    (next: Rect01) => {
      if (!dims) return;
      pendingRectRef.current = next;
      if (rafRef.current) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        const pending = pendingRectRef.current;
        pendingRectRef.current = null;
        if (!pending) return;
        onChange(valuesFromRect01(pending, units, dims));
      });
    },
    [dims, onChange, units],
  );

  type DragState =
    | { kind: "draw"; pointerId: number; start: Point01 }
    | { kind: "move"; pointerId: number; start: Point01; startRect: Rect01 }
    | { kind: "resize"; pointerId: number; start: Point01; startRect: Rect01; handle: "tl" | "tr" | "br" | "bl" };

  const [drag, setDrag] = useState<DragState | null>(null);

  const hitTestRect = useCallback((p: Point01) => {
    const r = normalizeRect01(rect01);
    return p.x >= r.x1 && p.x <= r.x2 && p.y >= r.y1 && p.y <= r.y2;
  }, [rect01]);

  const onOverlayPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!dims) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const p = point01FromPointerEvent(event, rect);

      const pointerId = event.pointerId;
      (event.currentTarget as HTMLDivElement).setPointerCapture(pointerId);
      event.preventDefault();

      if (hitTestRect(p)) {
        setDrag({ kind: "move", pointerId, start: p, startRect: rect01 });
      } else {
        setDrag({ kind: "draw", pointerId, start: p });
        const initial = normalizeRect01({ x1: p.x, y1: p.y, x2: p.x, y2: p.y });
        setRect01(initial);
        commitRect(initial);
      }
    },
    [dims, hitTestRect, rect01, commitRect],
  );

  const onHandlePointerDown = useCallback(
    (handle: "tl" | "tr" | "br" | "bl", event: React.PointerEvent<HTMLDivElement>) => {
      if (!dims) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const p = point01FromPointerEvent(event, rect);
      const pointerId = event.pointerId;
      overlayRef.current?.setPointerCapture(pointerId);
      event.preventDefault();
      event.stopPropagation();
      setDrag({ kind: "resize", pointerId, start: p, startRect: rect01, handle });
    },
    [dims, rect01],
  );

  const onOverlayPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!dims) return;
      if (!drag) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const p = point01FromPointerEvent(event, rect);
      event.preventDefault();

      if (drag.kind === "draw") {
        const next = normalizeRect01({ x1: drag.start.x, y1: drag.start.y, x2: p.x, y2: p.y });
        setRect01(next);
        commitRect(next);
        return;
      }

      if (drag.kind === "move") {
        const dx = p.x - drag.start.x;
        const dy = p.y - drag.start.y;
        const startRect = normalizeRect01(drag.startRect);
        const w = startRect.x2 - startRect.x1;
        const h = startRect.y2 - startRect.y1;
        let x1 = startRect.x1 + dx;
        let y1 = startRect.y1 + dy;
        x1 = Math.max(0, Math.min(1 - w, x1));
        y1 = Math.max(0, Math.min(1 - h, y1));
        const next = normalizeRect01({ x1, y1, x2: x1 + w, y2: y1 + h });
        setRect01(next);
        commitRect(next);
        return;
      }

      const startRect = normalizeRect01(drag.startRect);
      let next: Rect01 = startRect;
      if (drag.handle === "tl") next = { ...startRect, x1: p.x, y1: p.y };
      if (drag.handle === "tr") next = { ...startRect, x2: p.x, y1: p.y };
      if (drag.handle === "br") next = { ...startRect, x2: p.x, y2: p.y };
      if (drag.handle === "bl") next = { ...startRect, x1: p.x, y2: p.y };
      next = normalizeRect01(next);
      setRect01(next);
      commitRect(next);
    },
    [dims, drag, commitRect],
  );

  const onOverlayPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag) return;
    if (event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    setDrag(null);
  }, [drag]);

  const reset = useCallback(() => {
    const next = { x1: 0, y1: 0, x2: 1, y2: 1 };
    setRect01(next);
    if (dims) onChange(valuesFromRect01(next, units, dims));
  }, [dims, onChange, units]);

  const rectStyle = useMemo(() => {
    const r = normalizeRect01(rect01);
    const left = `${r.x1 * 100}%`;
    const top = `${r.y1 * 100}%`;
    const width = `${Math.max(0, (r.x2 - r.x1) * 100)}%`;
    const height = `${Math.max(0, (r.y2 - r.y1) * 100)}%`;
    return { left, top, width, height };
  }, [rect01]);

  const canInteract = open && Boolean(snapshot.url) && Boolean(dims);
  const snapshotError = snapshot.loading ? null : snapshot.error;
  const showNoSnapshot = !snapshot.loading && !snapshot.url && !snapshotError;

  return (
    <Modal
      open={open}
      title={t("core.ui.pipelines.panels.image_draw.modal_title.crop")}
      onClose={onClose}
      panelStyle={{
        width: "min(1200px, calc(100vw - 28px))",
        height: "calc(100vh - 28px)",
        maxHeight: "calc(100vh - 28px)",
      }}
      bodyStyle={{ display: "flex", flexDirection: "column", gap: 10, overflow: "hidden" }}
    >
      <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_draw.crop_instructions")}</div>
        <div className="rowWrap" style={{ gap: 8 }}>
          <button className="chipButton" type="button" onClick={snapshot.refresh} disabled={!open || snapshot.loading}>
            {t("core.ui.pipelines.panels.image_draw.refresh")}
          </button>
          <button className="chipButton" type="button" onClick={reset} disabled={!canInteract}>
            {t("core.ui.pipelines.panels.image_draw.reset")}
          </button>
        </div>
      </div>

      {snapshotError ? <div className="pipelinesInlineError">{snapshotError}</div> : null}
      {!snapshotError && snapshot.loading ? (
        <div className="pipelinesHint">{t("core.ui.pipelines.panels.image_draw.loading")}</div>
      ) : null}

      <div
        style={{
          flex: 1,
          minHeight: 0,
          borderRadius: 16,
          border: "1px solid var(--color-border-subtle)",
          background: "rgba(0,0,0,0.22)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 10,
          overflow: "auto",
        }}
      >
        {snapshot.url ? (
          <div ref={containerRef} style={{ position: "relative", display: "inline-block" }}>
            <img
              src={snapshot.url}
              alt={t("core.ui.pipelines.panels.image_draw.snapshot_alt")}
              style={{
                display: "block",
                maxWidth: "100%",
                maxHeight: "100%",
                borderRadius: 14,
                border: "1px solid rgba(255,255,255,0.12)",
                userSelect: "none",
                WebkitUserSelect: "none",
              }}
              onLoad={(event) => {
                const img = event.currentTarget;
                const width = Number(img.naturalWidth || 0);
                const height = Number(img.naturalHeight || 0);
                if (width > 1 && height > 1) setDims({ width, height });
              }}
              draggable={false}
            />

            <div
              role="presentation"
              ref={overlayRef}
              onPointerDown={onOverlayPointerDown}
              onPointerMove={onOverlayPointerMove}
              onPointerUp={onOverlayPointerUp}
              onPointerCancel={onOverlayPointerUp}
              style={{
                position: "absolute",
                inset: 0,
                cursor: drag?.kind === "move" ? "grabbing" : drag?.kind ? "crosshair" : canInteract ? "crosshair" : "default",
                touchAction: "none",
              }}
            >
              <div
                aria-hidden="true"
                style={{
                  position: "absolute",
                  ...rectStyle,
                  border: "2px solid rgba(56,189,248,0.92)",
                  background: "rgba(56,189,248,0.10)",
                  boxShadow: "0 12px 28px rgba(0,0,0,0.22)",
                }}
              />

              {(["tl", "tr", "br", "bl"] as const).map((handle) => {
                const r = normalizeRect01(rect01);
                const x = handle === "tl" || handle === "bl" ? r.x1 : r.x2;
                const y = handle === "tl" || handle === "tr" ? r.y1 : r.y2;
                const cursor =
                  handle === "tl" || handle === "br" ? "nwse-resize" : "nesw-resize";
                return (
                  <div
                    key={handle}
                    role="presentation"
                    onPointerDown={(event) => onHandlePointerDown(handle, event)}
                    style={{
                      position: "absolute",
                      left: `${x * 100}%`,
                      top: `${y * 100}%`,
                      transform: "translate(-50%,-50%)",
                      width: 14,
                      height: 14,
                      borderRadius: 4,
                      background: "rgba(56,189,248,0.95)",
                      border: "2px solid rgba(255,255,255,0.95)",
                      boxShadow: "0 10px 18px rgba(0,0,0,0.28)",
                      cursor,
                      pointerEvents: canInteract ? "auto" : "none",
                    }}
                  />
                );
              })}
            </div>
          </div>
        ) : showNoSnapshot ? (
          <div className="pipelinesHint">
            {t("core.ui.pipelines.panels.image_draw.no_snapshot")}
          </div>
        ) : (
          <div className="pipelinesHint">{t("core.ui.pipelines.panels.image_draw.loading")}</div>
        )}
      </div>

      <div className="modalFooter" style={{ justifyContent: "space-between" }}>
        <div className="pipelinesHint">
          {dims ? t("core.ui.pipelines.panels.image_draw.snapshot_meta", { w: dims.width, h: dims.height, units }) : ""}
        </div>
        <button className="primaryButton" type="button" onClick={onClose}>
          {t("core.ui.pipelines.panels.image_draw.close")}
        </button>
      </div>
    </Modal>
  );
}

type PerspectiveModalProps = {
  open: boolean;
  onClose: () => void;
  snapshotSource: SnapshotSource | null;
  units: "percent" | "pixels";
  points: number[][];
  onChange: (points: number[][]) => void;
};

function points01FromValues(points: number[][], units: "percent" | "pixels", dims: ImageDims): Point01[] {
  const safe = Array.isArray(points) ? points : [];
  const fallback: Point01[] = [
    { x: 0, y: 0 },
    { x: 1, y: 0 },
    { x: 1, y: 1 },
    { x: 0, y: 1 },
  ];

  const denomX = units === "pixels" ? Math.max(1, dims.width - 1) : 100;
  const denomY = units === "pixels" ? Math.max(1, dims.height - 1) : 100;

  const out: Point01[] = [];
  for (let i = 0; i < 4; i += 1) {
    const item = safe[i];
    const xRaw = Array.isArray(item) && item.length >= 2 ? Number(item[0]) : NaN;
    const yRaw = Array.isArray(item) && item.length >= 2 ? Number(item[1]) : NaN;
    const x = Number.isFinite(xRaw) ? clamp01(xRaw / denomX) : fallback[i]!.x;
    const y = Number.isFinite(yRaw) ? clamp01(yRaw / denomY) : fallback[i]!.y;
    out.push({ x, y });
  }
  return out;
}

function valuesFromPoints01(points01: Point01[], units: "percent" | "pixels", dims: ImageDims): number[][] {
  const denomX = units === "pixels" ? Math.max(1, dims.width - 1) : 100;
  const denomY = units === "pixels" ? Math.max(1, dims.height - 1) : 100;
  const step = units === "pixels" ? 1 : 0.5;

  return points01.slice(0, 4).map((p) => {
    const x = roundToStep(clamp01(p.x) * denomX, step);
    const y = roundToStep(clamp01(p.y) * denomY, step);
    return [x, y];
  });
}

export function PerspectiveCropDrawModal({
  open,
  onClose,
  snapshotSource,
  units,
  points,
  onChange,
}: PerspectiveModalProps): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const snapshot = useSnapshotObjectUrl(open, snapshotSource);
  const [dims, setDims] = useState<ImageDims | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);

  const [points01, setPoints01] = useState<Point01[]>([
    { x: 0, y: 0 },
    { x: 1, y: 0 },
    { x: 1, y: 1 },
    { x: 0, y: 1 },
  ]);
  const initializedRef = useRef(false);

  const pendingPointsRef = useRef<Point01[] | null>(null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!open) {
      setDims(null);
      initializedRef.current = false;
      return;
    }
  }, [open]);

  useEffect(() => {
    if (!open || !dims) return;
    if (initializedRef.current) return;
    initializedRef.current = true;
    setPoints01(points01FromValues(points, units, dims));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, dims?.width, dims?.height]);

  useEffect(() => {
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      pendingPointsRef.current = null;
    };
  }, []);

  const commitPoints = useCallback(
    (next: Point01[]) => {
      if (!dims) return;
      pendingPointsRef.current = next;
      if (rafRef.current) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        const pending = pendingPointsRef.current;
        pendingPointsRef.current = null;
        if (!pending) return;
        onChange(valuesFromPoints01(pending, units, dims));
      });
    },
    [dims, onChange, units],
  );

  type DragState =
    | { kind: "point"; pointerId: number; index: number }
    | { kind: "move"; pointerId: number; start: Point01; startPoints: Point01[] };

  const [drag, setDrag] = useState<DragState | null>(null);

  const bounds = useMemo(() => {
    const xs = points01.map((p) => p.x);
    const ys = points01.map((p) => p.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    return { minX, maxX, minY, maxY };
  }, [points01]);

  const canInteract = open && Boolean(snapshot.url) && Boolean(dims);
  const snapshotError = snapshot.loading ? null : snapshot.error;
  const showNoSnapshot = !snapshot.loading && !snapshot.url && !snapshotError;

  const onPointPointerDown = useCallback(
    (index: number, event: React.PointerEvent<HTMLDivElement>) => {
      if (!dims) return;
      const pointerId = event.pointerId;
      overlayRef.current?.setPointerCapture(pointerId);
      event.preventDefault();
      event.stopPropagation();
      setDrag({ kind: "point", pointerId, index });
    },
    [dims],
  );

  const onOverlayPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!dims) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const p = point01FromPointerEvent(event, rect);
      const insideBounds =
        p.x >= bounds.minX - 0.02 && p.x <= bounds.maxX + 0.02 && p.y >= bounds.minY - 0.02 && p.y <= bounds.maxY + 0.02;
      if (!insideBounds) return;
      const pointerId = event.pointerId;
      (event.currentTarget as HTMLDivElement).setPointerCapture(pointerId);
      event.preventDefault();
      setDrag({ kind: "move", pointerId, start: p, startPoints: points01 });
    },
    [dims, bounds, points01],
  );

  const onOverlayPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!dims) return;
      if (!drag) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const p = point01FromPointerEvent(event, rect);
      event.preventDefault();

      if (drag.kind === "point") {
        const next = points01.map((prev, idx) => (idx === drag.index ? { x: p.x, y: p.y } : prev));
        setPoints01(next);
        commitPoints(next);
        return;
      }

      const dx = p.x - drag.start.x;
      const dy = p.y - drag.start.y;
      const minX = Math.min(...drag.startPoints.map((pt) => pt.x));
      const maxX = Math.max(...drag.startPoints.map((pt) => pt.x));
      const minY = Math.min(...drag.startPoints.map((pt) => pt.y));
      const maxY = Math.max(...drag.startPoints.map((pt) => pt.y));
      const clampedDx = Math.max(-minX, Math.min(1 - maxX, dx));
      const clampedDy = Math.max(-minY, Math.min(1 - maxY, dy));
      const next = drag.startPoints.map((pt) => ({ x: pt.x + clampedDx, y: pt.y + clampedDy }));
      setPoints01(next);
      commitPoints(next);
    },
    [dims, drag, points01, commitPoints],
  );

  const onOverlayPointerUp = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!drag) return;
      if (event.pointerId !== drag.pointerId) return;
      event.preventDefault();
      setDrag(null);
    },
    [drag],
  );

  const reset = useCallback(() => {
    const next = [
      { x: 0, y: 0 },
      { x: 1, y: 0 },
      { x: 1, y: 1 },
      { x: 0, y: 1 },
    ];
    setPoints01(next);
    if (dims) onChange(valuesFromPoints01(next, units, dims));
  }, [dims, onChange, units]);

  const pathD = useMemo(() => {
    const pts = points01.slice(0, 4);
    if (pts.length < 4) return "";
    return `M ${pts[0]!.x * 100} ${pts[0]!.y * 100} L ${pts[1]!.x * 100} ${pts[1]!.y * 100} L ${pts[2]!.x * 100} ${pts[2]!.y * 100} L ${pts[3]!.x * 100} ${pts[3]!.y * 100} Z`;
  }, [points01]);

  return (
    <Modal
      open={open}
      title={t("core.ui.pipelines.panels.image_draw.modal_title.perspective")}
      onClose={onClose}
      panelStyle={{
        width: "min(1200px, calc(100vw - 28px))",
        height: "calc(100vh - 28px)",
        maxHeight: "calc(100vh - 28px)",
      }}
      bodyStyle={{ display: "flex", flexDirection: "column", gap: 10, overflow: "hidden" }}
    >
      <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div className="pipelinesStepHint">{t("core.ui.pipelines.panels.image_draw.perspective_instructions")}</div>
        <div className="rowWrap" style={{ gap: 8 }}>
          <button className="chipButton" type="button" onClick={snapshot.refresh} disabled={!open || snapshot.loading}>
            {t("core.ui.pipelines.panels.image_draw.refresh")}
          </button>
          <button className="chipButton" type="button" onClick={reset} disabled={!canInteract}>
            {t("core.ui.pipelines.panels.image_draw.reset")}
          </button>
        </div>
      </div>

      {snapshotError ? <div className="pipelinesInlineError">{snapshotError}</div> : null}
      {!snapshotError && snapshot.loading ? (
        <div className="pipelinesHint">{t("core.ui.pipelines.panels.image_draw.loading")}</div>
      ) : null}

      <div
        style={{
          flex: 1,
          minHeight: 0,
          borderRadius: 16,
          border: "1px solid var(--color-border-subtle)",
          background: "rgba(0,0,0,0.22)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 10,
          overflow: "auto",
        }}
      >
        {snapshot.url ? (
          <div ref={containerRef} style={{ position: "relative", display: "inline-block" }}>
            <img
              src={snapshot.url}
              alt={t("core.ui.pipelines.panels.image_draw.snapshot_alt")}
              style={{
                display: "block",
                maxWidth: "100%",
                maxHeight: "100%",
                borderRadius: 14,
                border: "1px solid rgba(255,255,255,0.12)",
                userSelect: "none",
                WebkitUserSelect: "none",
              }}
              onLoad={(event) => {
                const img = event.currentTarget;
                const width = Number(img.naturalWidth || 0);
                const height = Number(img.naturalHeight || 0);
                if (width > 1 && height > 1) setDims({ width, height });
              }}
              draggable={false}
            />

            <div
              role="presentation"
              ref={overlayRef}
              onPointerDown={onOverlayPointerDown}
              onPointerMove={onOverlayPointerMove}
              onPointerUp={onOverlayPointerUp}
              onPointerCancel={onOverlayPointerUp}
              style={{
                position: "absolute",
                inset: 0,
                cursor: drag?.kind === "move" ? "grabbing" : canInteract ? "crosshair" : "default",
                touchAction: "none",
              }}
            >
              <svg
                viewBox="0 0 100 100"
                preserveAspectRatio="none"
                style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
                aria-hidden="true"
              >
                <path d={pathD} fill="rgba(56,189,248,0.10)" stroke="rgba(56,189,248,0.92)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
              </svg>

              {points01.slice(0, 4).map((p, idx) => (
                <div
                  key={`pt-${idx}`}
                  role="presentation"
                  onPointerDown={(event) => onPointPointerDown(idx, event)}
                  style={{
                    position: "absolute",
                    left: `${p.x * 100}%`,
                    top: `${p.y * 100}%`,
                    transform: "translate(-50%,-50%)",
                    width: 18,
                    height: 18,
                    borderRadius: 999,
                    background: "rgba(56,189,248,0.95)",
                    border: "2px solid rgba(255,255,255,0.95)",
                    boxShadow: "0 10px 18px rgba(0,0,0,0.28)",
                    cursor: "grab",
                    pointerEvents: canInteract ? "auto" : "none",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 11,
                    fontWeight: 800,
                    color: "rgba(0,0,0,0.82)",
                  }}
                >
                  {idx === 0 ? "TL" : idx === 1 ? "TR" : idx === 2 ? "BR" : "BL"}
                </div>
              ))}
            </div>
          </div>
        ) : showNoSnapshot ? (
          <div className="pipelinesHint">{t("core.ui.pipelines.panels.image_draw.no_snapshot")}</div>
        ) : (
          <div className="pipelinesHint">{t("core.ui.pipelines.panels.image_draw.loading")}</div>
        )}
      </div>

      <div className="modalFooter" style={{ justifyContent: "space-between" }}>
        <div className="pipelinesHint">
          {dims ? t("core.ui.pipelines.panels.image_draw.snapshot_meta", { w: dims.width, h: dims.height, units }) : ""}
        </div>
        <button className="primaryButton" type="button" onClick={onClose}>
          {t("core.ui.pipelines.panels.image_draw.close")}
        </button>
      </div>
    </Modal>
  );
}
