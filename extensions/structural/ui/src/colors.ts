function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
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

