import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Vector2 } from "@toposync/plugin-api";

import { clamp } from "../parsing";

type Unit = "m" | "cm" | "mm" | "ft" | "in";

type Line2D = {
  a: Vector2;
  b: Vector2;
};

type NaturalSize = { width: number; height: number };

type Props = {
  t: (key: string, params?: Record<string, unknown>, fallback?: string) => string;
  locale: string;
  imageUrl: string;
  pixelWidth?: number | null;
  pixelHeight?: number | null;
  scaleRef?: unknown;
  onApply: (patch: Record<string, unknown>) => void;
};

const UNIT_FACTORS_METERS: Record<Unit, number> = {
  m: 1,
  cm: 0.01,
  mm: 0.001,
  ft: 0.3048,
  in: 0.0254,
};

const UNIT_STORAGE_KEY = "toposync.images.scaleUnit";

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return Boolean(v) && typeof v === "object" && !Array.isArray(v);
}

function readUnit(v: unknown, fallback: Unit): Unit {
  if (v === "m" || v === "cm" || v === "mm" || v === "ft" || v === "in") return v;
  return fallback;
}

function guessDefaultUnit(locale: string): Unit {
  if (locale.startsWith("en-US")) return "ft";
  return "m";
}

function loadPreferredUnit(locale: string): Unit {
  try {
    return readUnit(localStorage.getItem(UNIT_STORAGE_KEY), guessDefaultUnit(locale));
  } catch {
    return guessDefaultUnit(locale);
  }
}

function savePreferredUnit(unit: Unit): void {
  try {
    localStorage.setItem(UNIT_STORAGE_KEY, unit);
  } catch {
    // ignore
  }
}

function parseScaleRef(scaleRef: unknown): { line: Line2D | null; value: number | null; unit: Unit | null } {
  if (!isRecord(scaleRef)) return { line: null, value: null, unit: null };
  const ax = scaleRef.ax;
  const ay = scaleRef.ay;
  const bx = scaleRef.bx;
  const by = scaleRef.by;
  const value = scaleRef.value;
  const unit = scaleRef.unit;

  const hasLine =
    isFiniteNumber(ax) &&
    isFiniteNumber(ay) &&
    isFiniteNumber(bx) &&
    isFiniteNumber(by) &&
    ax >= 0 &&
    ax <= 1 &&
    ay >= 0 &&
    ay <= 1 &&
    bx >= 0 &&
    bx <= 1 &&
    by >= 0 &&
    by <= 1;

  return {
    line: hasLine ? { a: { x: ax, y: ay }, b: { x: bx, y: by } } : null,
    value: isFiniteNumber(value) ? value : null,
    unit: readUnit(unit, "m"),
  };
}

function parseLocaleNumber(text: string): number | null {
  const raw = text.trim();
  if (!raw) return null;
  const normalized = raw.replace(/\s+/g, "").replace(",", ".");
  const value = Number.parseFloat(normalized);
  if (!Number.isFinite(value)) return null;
  return value;
}

export function ImageScaleReference({
  t,
  locale,
  imageUrl,
  pixelWidth,
  pixelHeight,
  scaleRef,
  onApply,
}: Props): React.ReactElement {
  const parsedRef = useMemo(() => parseScaleRef(scaleRef), [scaleRef]);

  const [unit, setUnit] = useState<Unit>(() => parsedRef.unit ?? loadPreferredUnit(locale));
  const [lengthText, setLengthText] = useState<string>(() =>
    parsedRef.value && Number.isFinite(parsedRef.value) ? String(parsedRef.value) : "",
  );
  const [line, setLine] = useState<Line2D | null>(() => parsedRef.line);
  const [isDrawing, setIsDrawing] = useState(false);
  const [naturalSize, setNaturalSize] = useState<NaturalSize | null>(null);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const numberFormatter = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  const renderCanvas = useCallback(() => {
    const container = containerRef.current;
    const canvas = canvasRef.current;
    if (!container || !canvas) return;

    const rect = container.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;

    const widthPx = Math.max(1, Math.floor(rect.width * dpr));
    const heightPx = Math.max(1, Math.floor(rect.height * dpr));
    if (canvas.width !== widthPx || canvas.height !== heightPx) {
      canvas.width = widthPx;
      canvas.height = heightPx;
    }

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    if (!line) return;

    const a = { x: line.a.x * rect.width, y: line.a.y * rect.height };
    const b = { x: line.b.x * rect.width, y: line.b.y * rect.height };

    ctx.lineWidth = 2;
    ctx.strokeStyle = "rgba(251,191,36,0.92)";
    ctx.shadowColor = "rgba(251,191,36,0.25)";
    ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.shadowBlur = 0;

    ctx.fillStyle = "rgba(251,191,36,0.92)";
    ctx.strokeStyle = "rgba(0,0,0,0.55)";
    ctx.lineWidth = 1.5;
    for (const p of [a, b]) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
  }, [line]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => renderCanvas());
    ro.observe(container);
    renderCanvas();
    return () => ro.disconnect();
  }, [renderCanvas]);

  useEffect(() => {
    renderCanvas();
  }, [renderCanvas, line, isDrawing]);

  useEffect(() => {
    if (!naturalSize) return;
    const hasPixelSize = Number.isFinite(pixelWidth) && Number.isFinite(pixelHeight);
    if (hasPixelSize) return;
    onApply({ pixel_width: naturalSize.width, pixel_height: naturalSize.height });
  }, [naturalSize, onApply, pixelHeight, pixelWidth]);

  const pixelDistance = useMemo(() => {
    if (!line) return null;
    const w = naturalSize?.width ?? (Number.isFinite(pixelWidth) ? pixelWidth : null);
    const h = naturalSize?.height ?? (Number.isFinite(pixelHeight) ? pixelHeight : null);
    if (!w || !h) return null;
    const a = { x: line.a.x * w, y: line.a.y * h };
    const b = { x: line.b.x * w, y: line.b.y * h };
    const d = Math.hypot(a.x - b.x, a.y - b.y);
    return d >= 1 ? d : null;
  }, [line, naturalSize, pixelHeight, pixelWidth]);

  const lengthValue = useMemo(() => parseLocaleNumber(lengthText), [lengthText]);

  const computed = useMemo(() => {
    const w = naturalSize?.width ?? (Number.isFinite(pixelWidth) ? pixelWidth : null);
    const h = naturalSize?.height ?? (Number.isFinite(pixelHeight) ? pixelHeight : null);
    if (!w || !h) return null;
    if (!pixelDistance || !lengthValue || lengthValue <= 0) return null;
    const lengthMeters = lengthValue * UNIT_FACTORS_METERS[unit];
    const metersPerPixel = lengthMeters / pixelDistance;
    const widthMeters = w * metersPerPixel;
    const depthMeters = h * metersPerPixel;
    return {
      metersPerPixel,
      widthMeters,
      depthMeters,
      lengthMeters,
      pixelWidth: w,
      pixelHeight: h,
    };
  }, [lengthValue, naturalSize, pixelDistance, pixelHeight, pixelWidth, unit]);

  const applyScale = useCallback(() => {
    if (!line) return;
    if (!computed) return;
    onApply({
      width_m: clamp(computed.widthMeters, 0.05, 200),
      depth_m: clamp(computed.depthMeters, 0.05, 200),
      pixel_width: computed.pixelWidth,
      pixel_height: computed.pixelHeight,
      scale_ref: {
        ax: line.a.x,
        ay: line.a.y,
        bx: line.b.x,
        by: line.b.y,
        value: lengthValue,
        unit,
      },
    });
  }, [computed, lengthValue, line, onApply, unit]);

  const clear = useCallback(() => {
    setLine(null);
    onApply({ scale_ref: null });
  }, [onApply]);

  return (
    <div>
      <div className="rowWrap" style={{ alignItems: "baseline", justifyContent: "space-between" }}>
        <div className="label">{t("ext.images.scale.title")}</div>
        <div className="hint">{t("ext.images.scale.hint")}</div>
      </div>

      <div className="hint" style={{ marginTop: 6, marginBottom: 10 }}>
        {t("ext.images.scale.desc")}
      </div>

      <div
        ref={containerRef}
        style={{
          position: "relative",
          width: "100%",
          aspectRatio: naturalSize ? `${naturalSize.width} / ${naturalSize.height}` : "16 / 10",
          borderRadius: 12,
          overflow: "hidden",
          border: "1px solid rgba(255,255,255,0.10)",
          background: "rgba(255,255,255,0.04)",
        }}
      >
        <img
          src={imageUrl}
          alt={t("ext.images.editor.image")}
          style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
          onLoad={(e) => {
            const img = e.currentTarget;
            if (!img.naturalWidth || !img.naturalHeight) return;
            setNaturalSize({ width: img.naturalWidth, height: img.naturalHeight });
          }}
        />
        <canvas
          ref={canvasRef}
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", touchAction: "none", cursor: "crosshair" }}
          onPointerDown={(e) => {
            const container = containerRef.current;
            if (!container) return;
            const rect = container.getBoundingClientRect();
            const x = (e.clientX - rect.left) / rect.width;
            const y = (e.clientY - rect.top) / rect.height;
            const a = { x: clamp(x, 0, 1), y: clamp(y, 0, 1) };
            setLine({ a, b: a });
            setIsDrawing(true);
            (e.currentTarget as HTMLCanvasElement).setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!isDrawing) return;
            const container = containerRef.current;
            if (!container) return;
            const rect = container.getBoundingClientRect();
            const x = (e.clientX - rect.left) / rect.width;
            const y = (e.clientY - rect.top) / rect.height;
            setLine((prev) => {
              if (!prev) return prev;
              return { ...prev, b: { x: clamp(x, 0, 1), y: clamp(y, 0, 1) } };
            });
          }}
          onPointerUp={(e) => {
            setIsDrawing(false);
            try {
              (e.currentTarget as HTMLCanvasElement).releasePointerCapture(e.pointerId);
            } catch {
              // ignore
            }
          }}
          onPointerCancel={() => setIsDrawing(false)}
        />
      </div>

      <div className="rowWrap" style={{ marginTop: 10 }}>
        <div className="field" style={{ flex: 1, minWidth: 180 }}>
          <div className="label">{t("ext.images.scale.distance")}</div>
          <input
            className="input"
            inputMode="decimal"
            value={lengthText}
            placeholder={t("ext.images.scale.distance_placeholder")}
            onChange={(e) => setLengthText(e.target.value)}
          />
        </div>
        <div className="field" style={{ width: 120, minWidth: 120 }}>
          <div className="label">{t("ext.images.scale.unit")}</div>
          <select
            className="input"
            value={unit}
            onChange={(e) => {
              const next = readUnit(e.target.value, unit);
              setUnit(next);
              savePreferredUnit(next);
            }}
          >
            <option value="m">{t("ext.images.units.m")}</option>
            <option value="cm">{t("ext.images.units.cm")}</option>
            <option value="mm">{t("ext.images.units.mm")}</option>
            <option value="ft">{t("ext.images.units.ft")}</option>
            <option value="in">{t("ext.images.units.in")}</option>
          </select>
        </div>
      </div>

      <div className="rowWrap" style={{ alignItems: "baseline", justifyContent: "space-between" }}>
        <div className="hint">
          {pixelDistance ? `${t("ext.images.scale.pixel_distance")}: ${numberFormatter.format(pixelDistance)} px` : null}
        </div>
        <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
          <button className="chipButton" type="button" onClick={clear} disabled={!line}>
            {t("ext.images.scale.clear")}
          </button>
          <button className="primaryButton" type="button" onClick={applyScale} disabled={!computed || !line}>
            {t("ext.images.scale.apply")}
          </button>
        </div>
      </div>

      {computed ? (
        <div className="card" style={{ marginTop: 10 }}>
          <div className="cardBody">
            {t("ext.images.scale.result", {
              w: numberFormatter.format(computed.widthMeters),
              d: numberFormatter.format(computed.depthMeters),
              mpp: numberFormatter.format(computed.metersPerPixel),
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
