export function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const cleaned = hex.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(cleaned)) return null;
  const r = Number.parseInt(cleaned.slice(0, 2), 16);
  const g = Number.parseInt(cleaned.slice(2, 4), 16);
  const b = Number.parseInt(cleaned.slice(4, 6), 16);
  return { r, g, b };
}

export function rgbaFromHex(hex: string, alpha: number): string {
  const rgb = hexToRgb(hex);
  const a = Math.max(0, Math.min(1, alpha));
  if (!rgb) return `rgba(99,102,241,${a})`;
  return `rgba(${rgb.r},${rgb.g},${rgb.b},${a})`;
}

function rgbToHex(rgb: { r: number; g: number; b: number }): string {
  const toHex = (value: number) => Math.round(Math.max(0, Math.min(255, value))).toString(16).padStart(2, "0");
  return `#${toHex(rgb.r)}${toHex(rgb.g)}${toHex(rgb.b)}`;
}

export function mixHex(hex: string, targetHex: string, amount: number): string {
  const source = hexToRgb(hex);
  const target = hexToRgb(targetHex);
  if (!source || !target) return hex;
  const t = Math.max(0, Math.min(1, amount));
  return rgbToHex({
    r: source.r + (target.r - source.r) * t,
    g: source.g + (target.g - source.g) * t,
    b: source.b + (target.b - source.b) * t,
  });
}

export function shadeHex(hex: string, amount: number): string {
  return amount >= 0 ? mixHex(hex, "#ffffff", amount) : mixHex(hex, "#000000", Math.abs(amount));
}
