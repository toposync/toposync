import type { ThemeDefinition } from "@toposync/plugin-api";

const STORAGE_KEY = "toposync.theme";
const STYLE_ELEMENT_ID = "toposync-theme-overrides";

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

export function loadThemeId(): string {
  return safeGetStorage(STORAGE_KEY) || "default";
}

export function saveThemeId(themeId: string): void {
  safeSetStorage(STORAGE_KEY, themeId);
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

export function applyTheme(theme: ThemeDefinition | null): void {
  if (typeof document === "undefined") return;

  const el = document.documentElement;
  el.dataset.toposyncTheme = theme?.id ?? "default";

  let styleEl = document.getElementById(STYLE_ELEMENT_ID) as HTMLStyleElement | null;
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = STYLE_ELEMENT_ID;
    document.head.appendChild(styleEl);
  }

  styleEl.textContent = theme ? buildThemeCss(theme) : "";
}

