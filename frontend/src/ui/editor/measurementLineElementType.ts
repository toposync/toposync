import type { ElementType, PlanePoint, Viewport2DContext } from "@toposync/plugin-api";

import { i18n, type Locale } from "../../util/i18n";

export const MEASUREMENT_LINE_ELEMENT_TYPE_ID = "com.toposync.core.measurement.line";

const DEFAULT_MEASUREMENT_COLOR = "#38bdf8";
const DEFAULT_MEASUREMENT_WIDTH_M = 0.02;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function readString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function readPlanePoint(value: unknown, fallback: PlanePoint): PlanePoint {
  if (!isRecord(value)) return fallback;
  const x = value.x;
  const z = value.z;
  if (typeof x !== "number" || typeof z !== "number") return fallback;
  if (!Number.isFinite(x) || !Number.isFinite(z)) return fallback;
  return { x, z };
}

const numberFmtByLocale = new Map<Locale, Intl.NumberFormat>();

function formatMeters(meters: number): string {
  const locale = i18n.getLocale();
  let fmt = numberFmtByLocale.get(locale);
  if (!fmt) {
    fmt = new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    numberFmtByLocale.set(locale, fmt);
  }
  return `${fmt.format(meters)} m`;
}

function distance(a: PlanePoint, b: PlanePoint): number {
  return Math.hypot(a.x - b.x, a.z - b.z);
}

export function drawMeasurementLine2D(args: {
  ctx: CanvasRenderingContext2D;
  viewport: Viewport2DContext;
  aWorld: PlanePoint;
  bWorld: PlanePoint;
  color?: string;
  widthM?: number;
  dashed?: boolean;
  showLabel?: boolean;
}): void {
  const { ctx, viewport } = args;
  const a = viewport.worldToScreen(args.aWorld);
  const b = viewport.worldToScreen(args.bWorld);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy);
  if (len < 1e-6) return;

  const widthM = typeof args.widthM === "number" && Number.isFinite(args.widthM) ? args.widthM : DEFAULT_MEASUREMENT_WIDTH_M;
  const widthPx = Math.max(2, widthM * viewport.scale);
  const color = args.color ?? DEFAULT_MEASUREMENT_COLOR;

  ctx.save();
  if (args.dashed !== false) ctx.setLineDash([8, 6]);
  ctx.lineCap = "round";
  ctx.strokeStyle = color;
  ctx.lineWidth = widthPx;
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.stroke();
  ctx.setLineDash([]);

  const r = Math.max(4, widthPx * 0.9);
  ctx.fillStyle = "rgba(255,255,255,0.92)";
  ctx.strokeStyle = "rgba(0,0,0,0.35)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(a.x, a.y, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(b.x, b.y, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();

  const showLabel = args.showLabel !== false;
  if (showLabel) {
    const labelText = formatMeters(distance(args.aWorld, args.bWorld));
    const midX = (a.x + b.x) / 2;
    const midY = (a.y + b.y) / 2;
    const nx = -dy / len;
    const ny = dx / len;
    const offsetPx = 16;
    const labelX = midX + nx * offsetPx;
    const labelY = midY + ny * offsetPx;

    ctx.font = "12px ui-sans-serif, system-ui";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const metrics = ctx.measureText(labelText);
    const boxW = metrics.width + 16;
    const boxH = 22;
    const x0 = labelX - boxW / 2;
    const y0 = labelY - boxH / 2;

    ctx.fillStyle = "rgba(8,12,26,0.86)";
    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.lineWidth = 1;
    ctx.fillRect(x0, y0, boxW, boxH);
    ctx.strokeRect(x0 + 0.5, y0 + 0.5, boxW - 1, boxH - 1);
    ctx.fillStyle = "rgba(230,232,242,0.95)";
    ctx.fillText(labelText, labelX, labelY);
  }
  ctx.restore();
}

export function createMeasurementLineElementType(): ElementType {
  return {
    type: MEASUREMENT_LINE_ELEMENT_TYPE_ID,
    layerGroup: "measurements",
    placeable: false,
    name: { key: "core.elements.measurement_line.name", fallback: "Measurement" },
    description: { key: "core.elements.measurement_line.desc", fallback: "A persistent ruler line (editor-only)." },
    defaultProps: {
      a: { x: -1, z: 0 },
      b: { x: 1, z: 0 },
      color: DEFAULT_MEASUREMENT_COLOR,
      width: DEFAULT_MEASUREMENT_WIDTH_M,
    },
    render2D: ({ ctx, element, viewport }) => {
      const fallbackA = { x: element.position.x, z: element.position.z };
      const fallbackB = { x: element.position.x + 1, z: element.position.z };
      const a = readPlanePoint(element.props.a, fallbackA);
      const b = readPlanePoint(element.props.b, fallbackB);
      const color = readString(element.props.color, DEFAULT_MEASUREMENT_COLOR);
      const widthM = readNumber(element.props.width, DEFAULT_MEASUREMENT_WIDTH_M);

      drawMeasurementLine2D({ ctx, viewport, aWorld: a, bWorld: b, color, widthM, dashed: true, showLabel: true });
    },
  };
}

