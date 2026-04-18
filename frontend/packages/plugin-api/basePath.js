"use strict";

function normalizeBasePath(value) {
  const raw = String(value || "").trim();
  if (!raw) return "/";
  const prefixed = raw.startsWith("/") ? raw : `/${raw}`;
  return prefixed.replace(/\/+$/, "") || "/";
}

function getToposyncBasePath() {
  if (typeof window === "undefined") return "/";
  return normalizeBasePath(window.__TOPOSYNC_PUBLIC_BASE_PATH__);
}

function prefixWithBase(path) {
  const basePath = getToposyncBasePath();
  if (!path.startsWith("/")) return path;
  if (basePath === "/") return path;
  if (path === basePath || path.startsWith(`${basePath}/`)) return path;
  return `${basePath}${path}`;
}

function resolveToposyncUrl(input) {
  const value = String(input || "");
  if (!value) return value;
  if (typeof window === "undefined") return value;
  if (/^(data:|blob:|mailto:|tel:|#)/i.test(value)) return value;
  if (value.startsWith("//")) return value;
  if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
    try {
      const parsed = new URL(value, window.location.href);
      if (parsed.origin !== window.location.origin) return value;
      return prefixWithBase(`${parsed.pathname}${parsed.search}${parsed.hash}`);
    } catch {
      return value;
    }
  }
  if (value.startsWith("/")) return prefixWithBase(value);
  return value;
}

module.exports = {
  getToposyncBasePath,
  resolveToposyncUrl,
};
