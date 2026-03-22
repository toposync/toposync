import type { ThemeDefinition } from "@toposync/plugin-api";

const THEME_STORAGE_KEY = "toposync.theme";
const STYLE_ELEMENT_ID = "toposync-theme-overrides";
const TRANSPARENCY_STORAGE_KEY = "toposync.transparency";
const ACCENT_STORAGE_KEY = "toposync.accentIntensity";
const VIEWPORT3D_STORAGE_KEY = "toposync.viewport3dBackground";

export const BUILTIN_THEME_IDS = ["topo-day", "topo-night"] as const;
export type BuiltinThemeId = (typeof BUILTIN_THEME_IDS)[number];

export const TRANSPARENCY_LEVELS = ["normal", "high", "reduced"] as const;
export type TransparencyLevel = (typeof TRANSPARENCY_LEVELS)[number];

export const ACCENT_INTENSITIES = ["subtle", "normal", "vivid"] as const;
export type AccentIntensity = (typeof ACCENT_INTENSITIES)[number];

export const VIEWPORT3D_BACKGROUNDS = ["paper", "pure", "night"] as const;
export type Viewport3DBackground = (typeof VIEWPORT3D_BACKGROUNDS)[number];

export type UserVisualPreferences = {
  transparency: TransparencyLevel;
  accentIntensity: AccentIntensity;
  viewport3dBackground: Viewport3DBackground;
};

function safeGetStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSetStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // ignore
  }
}

function normalizeByList<T extends readonly string[]>(value: string | null, allowed: T, fallback: T[number]): T[number] {
  if (!value) return fallback;
  return (allowed as readonly string[]).includes(value) ? (value as T[number]) : fallback;
}

export function isBuiltinThemeId(value: string): value is BuiltinThemeId {
  return (BUILTIN_THEME_IDS as readonly string[]).includes(value);
}

export function loadThemeId(): string {
  const stored = safeGetStorage(THEME_STORAGE_KEY);
  if (!stored || stored === "default") return "topo-day";
  if (stored === "com.toposync.theme.neon_blue") return "topo-day";
  return stored;
}

export function saveThemeId(themeId: string): void {
  safeSetStorage(THEME_STORAGE_KEY, themeId);
}

export function loadTransparencyLevel(): TransparencyLevel {
  return normalizeByList(safeGetStorage(TRANSPARENCY_STORAGE_KEY), TRANSPARENCY_LEVELS, "normal");
}

export function saveTransparencyLevel(value: TransparencyLevel): void {
  safeSetStorage(TRANSPARENCY_STORAGE_KEY, value);
}

export function loadAccentIntensity(): AccentIntensity {
  return normalizeByList(safeGetStorage(ACCENT_STORAGE_KEY), ACCENT_INTENSITIES, "normal");
}

export function saveAccentIntensity(value: AccentIntensity): void {
  safeSetStorage(ACCENT_STORAGE_KEY, value);
}

export function loadViewport3DBackground(): Viewport3DBackground {
  return normalizeByList(safeGetStorage(VIEWPORT3D_STORAGE_KEY), VIEWPORT3D_BACKGROUNDS, "paper");
}

export function saveViewport3DBackground(value: Viewport3DBackground): void {
  safeSetStorage(VIEWPORT3D_STORAGE_KEY, value);
}

export function applyUserVisualPreferences(preferences: UserVisualPreferences): void {
  if (typeof document === "undefined") return;
  const el = document.documentElement;
  el.dataset.toposyncTransparency = preferences.transparency;
  el.dataset.toposyncAccent = preferences.accentIntensity;
  el.dataset.toposyncViewport3d = preferences.viewport3dBackground;
}

function normalizeVarName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "";
  return trimmed.startsWith("--") ? trimmed : `--${trimmed}`;
}

export function buildThemeCss(theme: ThemeDefinition): string {
  const vars = theme.vars ?? {};
  const lines: string[] = [];
  for (const [rawKey, rawValue] of Object.entries(vars)) {
    const key = normalizeVarName(rawKey);
    const value = String(rawValue ?? "").trim();
    if (!key || !value) continue;
    lines.push(`  ${key}: ${value};`);
  }

  const css = String(theme.css ?? "").trim();

  if (lines.length === 0 && !css) return "";

  const rootBlock = lines.length ? `:root {\n${lines.join("\n")}\n}\n` : "";
  return `${rootBlock}${css ? `${css}\n` : ""}`.trim() + "\n";
}

export function applyTheme(baseThemeId: BuiltinThemeId, overridesTheme: ThemeDefinition | null): void {
  if (typeof document === "undefined") return;

  const el = document.documentElement;
  el.dataset.toposyncBaseTheme = baseThemeId;
  el.dataset.toposyncTheme = overridesTheme?.id ?? baseThemeId;

  let styleEl = document.getElementById(STYLE_ELEMENT_ID) as HTMLStyleElement | null;
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = STYLE_ELEMENT_ID;
    document.head.appendChild(styleEl);
  }

  styleEl.textContent = overridesTheme ? buildThemeCss(overridesTheme) : "";
}
