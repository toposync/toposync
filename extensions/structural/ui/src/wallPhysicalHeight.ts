/** Physical measurement only. Missing values never inherit the visual wall height. */
export function readPhysicalHeightMeters(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : null;
}

export function parsePhysicalHeightInput(input: string):
  | { valid: true; value: number | null }
  | { valid: false } {
  const text = input.trim();
  if (!text) return { valid: true, value: null };
  // Plain decimal metres, accepting the decimal separator used in both locales.
  // The geometric contract requires > 0 and finite; no building height is assumed.
  if (!/^(?:\d+(?:[.,]\d*)?|[.,]\d+)$/.test(text)) return { valid: false };
  const value = readPhysicalHeightMeters(Number(text.replace(",", ".")));
  return value === null ? { valid: false } : { valid: true, value };
}
