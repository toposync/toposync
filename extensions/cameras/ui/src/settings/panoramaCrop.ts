import type { CameraSourcePanoramaArtifact, CameraSourcePanoramaCrop } from "../types";

export type PanoramaPoint = { x: number; y: number };
export type PanoramaCropHandle = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
export const FULL_PANORAMA_CROP: CameraSourcePanoramaCrop = { u_start: 0, u_width: 1, v_start: 0, v_height: 1 };
const MINIMUM_EXTENT = 0.005;

export function wrapPanoramaCoordinate(value: number): number {
  return ((value % 1) + 1) % 1;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

export function validPanoramaCrop(crop: CameraSourcePanoramaCrop | null | undefined): crop is CameraSourcePanoramaCrop {
  return Boolean(crop && Object.values(crop).every(Number.isFinite)
    && crop.u_start >= 0 && crop.u_start < 1 && crop.u_width > 0 && crop.u_width <= 1
    && crop.v_start >= 0 && crop.v_height > 0 && crop.v_start + crop.v_height <= 1 + 1e-9);
}

export function samePanoramaCrop(left: CameraSourcePanoramaCrop, right: CameraSourcePanoramaCrop): boolean {
  return (Object.keys(FULL_PANORAMA_CROP) as Array<keyof CameraSourcePanoramaCrop>)
    .every((key) => Math.abs(left[key] - right[key]) < 1e-9);
}

/** Bounds use exclusive right/bottom edges in the original panorama coordinates. */
export function panoramaCoverageCrop(width: number, height: number, bounds: unknown): CameraSourcePanoramaCrop {
  if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0
    || !bounds || typeof bounds !== "object") return FULL_PANORAMA_CROP;
  const { left, top, right, bottom } = bounds as Record<string, unknown>;
  if (typeof left !== "number" || typeof top !== "number" || typeof right !== "number" || typeof bottom !== "number"
    || ![left, top, right, bottom].every(Number.isFinite)
    || left < 0 || top < 0 || right > width || bottom > height || left >= right || top >= bottom) return FULL_PANORAMA_CROP;
  return { u_start: left / width, u_width: (right - left) / width, v_start: top / height, v_height: (bottom - top) / height };
}

/** Fit presentation only; an explicitly saved selection, including the full image, wins. */
export function panoramaPreviewCrop(artifact: Pick<CameraSourcePanoramaArtifact, "width" | "height" | "crop" | "crop_revision" | "coverage">): CameraSourcePanoramaCrop {
  if (validPanoramaCrop(artifact.crop) && (artifact.crop_revision > 1 || !samePanoramaCrop(artifact.crop, FULL_PANORAMA_CROP))) return artifact.crop;
  return panoramaCoverageCrop(artifact.width, artifact.height, artifact.coverage?.bounds_pixels);
}

/** Two corners in the displayed image; the seam shift is presentation only. */
export function panoramaCropFromCorners(start: PanoramaPoint, end: PanoramaPoint, seamOffset: number): CameraSourcePanoramaCrop {
  const left = clamp(Math.min(start.x, end.x), 0, 1 - MINIMUM_EXTENT);
  const top = clamp(Math.min(start.y, end.y), 0, 1 - MINIMUM_EXTENT);
  const width = clamp(Math.abs(end.x - start.x), MINIMUM_EXTENT, 1 - left);
  return {
    u_start: width === 1 ? 0 : wrapPanoramaCoordinate(left + seamOffset),
    u_width: width,
    v_start: top,
    v_height: clamp(Math.abs(end.y - start.y), MINIMUM_EXTENT, 1 - top),
  };
}

/** A periodic selection may be represented by two rectangles at the seam. */
export function panoramaCropSegments(crop: CameraSourcePanoramaCrop, seamOffset: number): Array<{ left: number; width: number }> {
  if (crop.u_width >= 1) return [{ left: 0, width: 1 }];
  const left = wrapPanoramaCoordinate(crop.u_start - seamOffset);
  const width = Math.min(crop.u_width, 1 - left);
  const segments = [{ left, width }];
  if (crop.u_width - width > 1e-9) segments.push({ left: 0, width: crop.u_width - width });
  return segments;
}

export function panoramaCropSeamOffset(crop: CameraSourcePanoramaCrop): number {
  return crop.u_width === 1 ? 0 : wrapPanoramaCoordinate(crop.u_start - (1 - crop.u_width) / 2);
}

export function adjustPanoramaCrop(crop: CameraSourcePanoramaCrop, handle: PanoramaCropHandle, horizontal: number, vertical: number): CameraSourcePanoramaCrop {
  if (handle === "move") return {
    ...crop,
    u_start: crop.u_width === 1 ? 0 : wrapPanoramaCoordinate(crop.u_start + horizontal),
    v_start: clamp(crop.v_start + vertical, 0, 1 - crop.v_height),
  };
  const next = { ...crop };
  if (handle.includes("w")) {
    const delta = clamp(horizontal, crop.u_width - 1, crop.u_width - MINIMUM_EXTENT);
    next.u_start = wrapPanoramaCoordinate(crop.u_start + delta);
    next.u_width = crop.u_width - delta;
  } else if (handle.includes("e")) next.u_width = clamp(crop.u_width + horizontal, MINIMUM_EXTENT, 1);
  if (handle.includes("n")) {
    const delta = clamp(vertical, -crop.v_start, crop.v_height - MINIMUM_EXTENT);
    next.v_start = crop.v_start + delta;
    next.v_height = crop.v_height - delta;
  } else if (handle.includes("s")) next.v_height = clamp(crop.v_height + vertical, MINIMUM_EXTENT, 1 - crop.v_start);
  if (next.u_width === 1) next.u_start = 0;
  return next;
}
