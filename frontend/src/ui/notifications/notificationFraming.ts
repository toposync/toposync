import type { Notification2DPin } from "@toposync/plugin-api";

/** A selection gets one automatic fit, including evidence arriving after selection. */
export class NotificationFraming {
  private key = "";
  private fitted = false;
  private manual = false;

  select(key: string): void {
    if (key === this.key) return;
    this.key = key;
    this.fitted = false;
    this.manual = false;
  }
  interact(): void { this.manual = true; }
  get canFitResize(): boolean { return !this.manual; }
  get preserveView(): boolean { return this.manual || this.fitted; }
  canFit(force = false): boolean { return force || (!this.manual && !this.fitted); }
  didFit(): void { this.fitted = true; }
  reset(): void { this.key = ""; this.fitted = false; this.manual = false; }
}

export function pinPoints(pin: Notification2DPin): Array<{ x: number; z: number }> {
  return [pin, ...(pin.trails?.flat() ?? pin.trail ?? [])]
    .filter(point => Number.isFinite(point.x) && Number.isFinite(point.z));
}

/** Fit evidence with room for the 48-pixel pin and an unambiguous single point. */
export function fitNotificationPoints(width: number, height: number, points: Array<{ x: number; y: number }>) {
  const valid = points.filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
  if (!valid.length || width <= 0 || height <= 0) return null;
  const minX = Math.min(...valid.map(point => point.x));
  const maxX = Math.max(...valid.map(point => point.x));
  const minY = Math.min(...valid.map(point => point.y));
  const maxY = Math.max(...valid.map(point => point.y));
  const scale = Math.min(Math.max(1, width - 112) / Math.max(160, maxX - minX),
    Math.max(1, height - 160) / Math.max(160, maxY - minY));
  return { scale, x: width / 2 - (minX + maxX) / 2 * scale,
    y: (height + 48) / 2 - (minY + maxY) / 2 * scale };
}
