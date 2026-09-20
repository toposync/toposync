import type { CameraGroundLens, CameraRayGroundCalibratedView } from "./types";

export type LensChoice = "identity" | "brown4" | "brown5" | "brown8" | "fisheye4";
export type GroundImageSize = { width: number; height: number };

// Dimensions come only from a decoded image, never camera metadata or a default.
export function groundImageResolutionStatus(
  image: GroundImageSize | null, calibration: GroundImageSize,
): "unavailable" | "mismatch" | "match" {
  if (!image || ![image.width, image.height].every((value) => Number.isSafeInteger(value) && value >= 2)) return "unavailable";
  return image.width === calibration.width && image.height === calibration.height ? "match" : "mismatch";
}
export type GroundLensDraft = {
  choice: LensChoice;
  width: string;
  height: string;
  fx: string;
  fy: string;
  cx: string;
  cy: string;
  coefficients: string[];
};

export const coefficientNames = (choice: LensChoice): string[] => choice === "identity" ? []
  : choice === "fisheye4" ? ["k1", "k2", "k3", "k4"]
    : ["k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"].slice(0, Number(choice.slice(5)));

export function groundLensDraft(view: CameraRayGroundCalibratedView): GroundLensDraft {
  const { lens, source_geometry: geometry } = view.projection_model;
  const measured = lens.type !== "identity_rectilinear_v1";
  return {
    choice: !measured ? "identity" : lens.type === "fisheye_kb4_v1" ? "fisheye4"
      : `brown${lens.coefficients.length}` as LensChoice,
    width: String(geometry.width), height: String(geometry.height),
    fx: measured ? String(lens.fx) : "", fy: measured ? String(lens.fy) : "",
    cx: measured ? String(lens.cx) : "", cy: measured ? String(lens.cy) : "",
    coefficients: measured ? lens.coefficients.map(String) : [],
  };
}

function finiteDecimal(value: string): number | null {
  const normalized = value.trim().replace(",", ".");
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(normalized)) return null;
  const result = Number(normalized);
  return Number.isFinite(result) ? result : null;
}

export function validateGroundLensDraft(draft: GroundLensDraft): {
  invalidFields: string[];
  value?: { lens: CameraGroundLens; width: number; height: number };
} {
  const invalidFields: string[] = [];
  const dimensions = { width: finiteDecimal(draft.width), height: finiteDecimal(draft.height) };
  for (const [key, value] of Object.entries(dimensions)) {
    // Pixel-edge normalization divides by dimension minus one.
    if (value === null || !Number.isSafeInteger(value) || value < 2) invalidFields.push(key);
  }
  if (!["identity", "brown4", "brown5", "brown8", "fisheye4"].includes(draft.choice)) invalidFields.push("choice");
  let lens: CameraGroundLens = { type: "identity_rectilinear_v1" };
  if (draft.choice !== "identity") {
    const intrinsics = { fx: finiteDecimal(draft.fx), fy: finiteDecimal(draft.fy), cx: finiteDecimal(draft.cx), cy: finiteDecimal(draft.cy) };
    for (const [key, value] of Object.entries(intrinsics)) {
      if (value === null || ((key === "fx" || key === "fy") && value <= 0)) invalidFields.push(key);
    }
    const names = coefficientNames(draft.choice);
    if (draft.coefficients.length !== names.length) invalidFields.push("coefficients");
    const coefficients = names.map((name, index) => {
      const value = finiteDecimal(draft.coefficients[index] ?? "");
      if (value === null) invalidFields.push(name);
      return value ?? 0; // Never exposed when validation fails.
    });
    lens = { type: draft.choice === "fisheye4" ? "fisheye_kb4_v1" : "rectilinear_brown_v1",
      fx: intrinsics.fx!, fy: intrinsics.fy!, cx: intrinsics.cx!, cy: intrinsics.cy!, coefficients };
  }
  return invalidFields.length ? { invalidFields } : {
    invalidFields, value: { lens, width: dimensions.width!, height: dimensions.height! },
  };
}

export function applyGroundLensDraft(view: CameraRayGroundCalibratedView, draft: GroundLensDraft): CameraRayGroundCalibratedView | null {
  const parsed = validateGroundLensDraft(draft).value;
  if (!parsed) return null;
  return {
    ...view,
    projection_model: { ...view.projection_model, lens: parsed.lens,
      source_geometry: { ...view.projection_model.source_geometry, width: parsed.width, height: parsed.height } },
    // The old digest and both fit/check metrics describe a different optical model.
    projection_quality: { status: "incomplete", estimated: false },
  };
}
