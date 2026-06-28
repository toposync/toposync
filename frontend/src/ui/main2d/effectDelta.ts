import type { Main2DEffectBlendMode } from "@toposync/plugin-api";

export type Main2DEffectPixelBuffer = {
  width: number;
  height: number;
  data: Uint8ClampedArray;
};

export type Main2DEffectDeltaCrop = {
  data: Uint8ClampedArray;
  crop: { x: number; y: number; width: number; height: number };
};

function createAbortError(): Error {
  if (typeof DOMException !== "undefined") {
    return new DOMException("Aborted", "AbortError") as unknown as Error;
  }
  const err = new Error("Aborted");
  err.name = "AbortError";
  return err;
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw createAbortError();
}

function alphaRequirementForIncrease(delta: number, base: number): number {
  if (delta <= 0) return 0;
  const headroom = 255 - base;
  return headroom <= 1e-6 ? 1 : delta / headroom;
}

function solveSourceOverChannel(base: number, delta: number, alpha: number): number {
  if (delta <= 0) return base;
  return Math.max(0, Math.min(255, Math.round(base + delta / Math.max(1e-6, alpha))));
}

function solveScreenChannel(base: number, delta: number, alpha: number): number {
  if (delta <= 0) return 0;
  const headroom = 255 - base;
  if (headroom <= 1e-6) return 0;
  return Math.max(0, Math.min(255, Math.round((delta / (headroom * Math.max(1e-6, alpha))) * 255)));
}

export function computeMain2DEffectDeltaCrop(
  base: Main2DEffectPixelBuffer,
  active: Main2DEffectPixelBuffer,
  options: { blendMode?: Main2DEffectBlendMode; signal?: AbortSignal } = {},
): Main2DEffectDeltaCrop | null {
  if (base.width !== active.width || base.height !== active.height || base.data.length !== active.data.length) {
    throw new Error("Effect buffers must have matching dimensions");
  }
  throwIfAborted(options.signal);
  const blendMode = options.blendMode ?? "source-over";
  const width = base.width;
  const height = base.height;
  const baseData = base.data;
  const activeData = active.data;
  const outData = new Uint8ClampedArray(width * height * 4);
  let minX = width;
  let minY = height;
  let maxX = -1;
  let maxY = -1;

  for (let y = 0; y < height; y += 1) {
    if ((y & 15) === 0) throwIfAborted(options.signal);
    for (let x = 0; x < width; x += 1) {
      const idx = (y * width + x) * 4;
      const baseAlpha = baseData[idx + 3] / 255;
      const activeAlpha = activeData[idx + 3] / 255;
      const baseR = baseData[idx] * baseAlpha;
      const baseG = baseData[idx + 1] * baseAlpha;
      const baseB = baseData[idx + 2] * baseAlpha;
      const activeR = activeData[idx] * activeAlpha;
      const activeG = activeData[idx + 1] * activeAlpha;
      const activeB = activeData[idx + 2] * activeAlpha;
      const dr = Math.max(0, activeR - baseR);
      const dg = Math.max(0, activeG - baseG);
      const db = Math.max(0, activeB - baseB);
      const da = Math.max(0, activeData[idx + 3] - baseData[idx + 3]);
      const delta = Math.max(dr, dg, db, da);
      if (delta <= 4) continue;

      const rgbAlpha = Math.max(
        alphaRequirementForIncrease(dr, baseR),
        alphaRequirementForIncrease(dg, baseG),
        alphaRequirementForIncrease(db, baseB),
      );
      const alphaAlpha = activeAlpha > baseAlpha && baseAlpha < 0.999 ? (activeAlpha - baseAlpha) / (1 - baseAlpha) : 0;
      const overlayAlpha = Math.max(0.025, Math.min(1, Math.max(rgbAlpha, alphaAlpha)));

      if (blendMode === "screen") {
        outData[idx] = solveScreenChannel(baseR, dr, overlayAlpha);
        outData[idx + 1] = solveScreenChannel(baseG, dg, overlayAlpha);
        outData[idx + 2] = solveScreenChannel(baseB, db, overlayAlpha);
      } else {
        outData[idx] = solveSourceOverChannel(baseR, dr, overlayAlpha);
        outData[idx + 1] = solveSourceOverChannel(baseG, dg, overlayAlpha);
        outData[idx + 2] = solveSourceOverChannel(baseB, db, overlayAlpha);
      }
      outData[idx + 3] = Math.round(overlayAlpha * 255);
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }

  throwIfAborted(options.signal);
  if (maxX < minX || maxY < minY) return null;

  const pad = 2;
  minX = Math.max(0, minX - pad);
  minY = Math.max(0, minY - pad);
  maxX = Math.min(width - 1, maxX + pad);
  maxY = Math.min(height - 1, maxY + pad);
  const cropWidth = maxX - minX + 1;
  const cropHeight = maxY - minY + 1;

  return { data: outData, crop: { x: minX, y: minY, width: cropWidth, height: cropHeight } };
}
