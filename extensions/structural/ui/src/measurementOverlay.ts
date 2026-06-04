import type {
  HostI18n,
  PlanePoint,
  Viewport2DContext,
} from "@toposync/plugin-api";

import { distanceBetweenPoints } from "./geometry";

type MeasurementI18n = Pick<HostI18n, "getLocale"> | null | undefined;

const numberFormatters = new Map<string, Intl.NumberFormat>();

function resolveLocale(i18n: MeasurementI18n): string {
  try {
    const locale = i18n?.getLocale?.();
    if (locale) return locale;
  } catch {
    // Test harnesses can pass a partial i18n object.
  }
  return "en";
}

function numberFormatter(i18n: MeasurementI18n): Intl.NumberFormat {
  const locale = resolveLocale(i18n);
  let formatter = numberFormatters.get(locale);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    numberFormatters.set(locale, formatter);
  }
  return formatter;
}

export function formatMeters(meters: number, i18n?: MeasurementI18n): string {
  return `${numberFormatter(i18n).format(meters)} m`;
}

export function formatSquareMeters(
  squareMeters: number,
  i18n?: MeasurementI18n,
): string {
  return `${numberFormatter(i18n).format(squareMeters)} m²`;
}

export function polygonAreaSquareMeters(vertices: PlanePoint[]): number {
  if (vertices.length < 3) return 0;
  let sum = 0;
  for (let i = 0; i < vertices.length; i += 1) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    sum += a.x * b.z - b.x * a.z;
  }
  return Math.abs(sum) / 2;
}

export function polygonMeasurementAnchor(vertices: PlanePoint[]): PlanePoint {
  if (vertices.length === 0) return { x: 0, z: 0 };
  if (vertices.length < 3) return averagePoint(vertices);

  let crossSum = 0;
  let cx = 0;
  let cz = 0;
  for (let i = 0; i < vertices.length; i += 1) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    const cross = a.x * b.z - b.x * a.z;
    crossSum += cross;
    cx += (a.x + b.x) * cross;
    cz += (a.z + b.z) * cross;
  }

  if (Math.abs(crossSum) <= 1e-9) return averagePoint(vertices);
  return { x: cx / (3 * crossSum), z: cz / (3 * crossSum) };
}

function averagePoint(points: PlanePoint[]): PlanePoint {
  if (points.length === 0) return { x: 0, z: 0 };
  let x = 0;
  let z = 0;
  for (const point of points) {
    x += point.x;
    z += point.z;
  }
  return { x: x / points.length, z: z / points.length };
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function drawMeasurementPill(args: {
  ctx: CanvasRenderingContext2D;
  viewport: Viewport2DContext;
  text: string;
  screenX: number;
  screenY: number;
}): void {
  const { ctx, viewport, text } = args;
  ctx.save();
  ctx.font = "12px ui-sans-serif, system-ui";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";

  const textWidth = ctx.measureText(text).width;
  const boxWidth = textWidth + 16;
  const boxHeight = 22;
  const minX = 4 + boxWidth / 2;
  const maxX = Math.max(minX, viewport.width - 4 - boxWidth / 2);
  const minY = 4 + boxHeight / 2;
  const maxY = Math.max(minY, viewport.height - 4 - boxHeight / 2);
  const centerX = clamp(args.screenX, minX, maxX);
  const centerY = clamp(args.screenY, minY, maxY);
  const x = centerX - boxWidth / 2;
  const y = centerY - boxHeight / 2;

  ctx.fillStyle = "rgba(8,12,26,0.86)";
  ctx.fillRect(x, y, boxWidth, boxHeight);
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 1;
  ctx.strokeRect(x + 0.5, y + 0.5, boxWidth - 1, boxHeight - 1);
  ctx.fillStyle = "rgba(230,232,242,0.95)";
  ctx.fillText(text, centerX, centerY);
  ctx.restore();
}

export function drawSegmentLengthLabel(args: {
  ctx: CanvasRenderingContext2D;
  viewport: Viewport2DContext;
  aWorld: PlanePoint;
  bWorld: PlanePoint;
  i18n?: MeasurementI18n;
  offsetPx?: number;
}): void {
  const length = distanceBetweenPoints(args.aWorld, args.bWorld);
  if (!(length > 1e-6)) return;

  const a = args.viewport.worldToScreen(args.aWorld);
  const b = args.viewport.worldToScreen(args.bWorld);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const screenLength = Math.hypot(dx, dy);
  if (!(screenLength > 1e-6)) return;

  const normalX = -dy / screenLength;
  const normalY = dx / screenLength;
  const offsetPx = args.offsetPx ?? 16;
  drawMeasurementPill({
    ctx: args.ctx,
    viewport: args.viewport,
    text: formatMeters(length, args.i18n),
    screenX: (a.x + b.x) / 2 + normalX * offsetPx,
    screenY: (a.y + b.y) / 2 + normalY * offsetPx,
  });
}

export function drawAreaLabel(args: {
  ctx: CanvasRenderingContext2D;
  viewport: Viewport2DContext;
  vertices: PlanePoint[];
  i18n?: MeasurementI18n;
}): void {
  const area = polygonAreaSquareMeters(args.vertices);
  if (!(area > 1e-6)) return;
  const anchor = args.viewport.worldToScreen(
    polygonMeasurementAnchor(args.vertices),
  );
  drawMeasurementPill({
    ctx: args.ctx,
    viewport: args.viewport,
    text: formatSquareMeters(area, args.i18n),
    screenX: anchor.x,
    screenY: anchor.y,
  });
}

export function drawPolygonMeasurementOverlay(args: {
  ctx: CanvasRenderingContext2D;
  viewport: Viewport2DContext;
  vertices: PlanePoint[];
  i18n?: MeasurementI18n;
  includeClosing?: boolean;
  includeArea?: boolean;
}): void {
  const { vertices } = args;
  if (vertices.length < 2) return;

  const edgeCount = args.includeClosing ? vertices.length : vertices.length - 1;
  for (let i = 0; i < edgeCount; i += 1) {
    drawSegmentLengthLabel({
      ctx: args.ctx,
      viewport: args.viewport,
      aWorld: vertices[i],
      bWorld: vertices[(i + 1) % vertices.length],
      i18n: args.i18n,
    });
  }

  if (args.includeArea && vertices.length >= 3) {
    drawAreaLabel(args);
  }
}
