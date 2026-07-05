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

async function parseHttpError(response, fallback) {
  try {
    const payload = await response.json();
    const detail = payload && typeof payload === "object" ? payload.detail : null;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (detail && typeof detail === "object" && typeof detail.error === "string" && detail.error.trim()) {
      return detail.error.trim();
    }
  } catch {
    try {
      const text = String(await response.text()).trim();
      if (text) return text;
    } catch {
      // ignore
    }
  }
  return fallback || `HTTP ${response.status}`;
}

async function requestJson(input, init) {
  const response = await fetch(resolveToposyncUrl(input), init);
  if (!response.ok) {
    const error = new Error(await parseHttpError(response));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function requestVoid(input, init) {
  const response = await fetch(resolveToposyncUrl(input), init);
  if (!response.ok) {
    const error = new Error(await parseHttpError(response));
    error.status = response.status;
    throw error;
  }
}

async function requestForm(input, form, init) {
  const response = await fetch(resolveToposyncUrl(input), { ...init, method: init?.method || "POST", body: form });
  if (!response.ok) {
    const error = new Error(await parseHttpError(response));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

module.exports = {
  getToposyncBasePath,
  requestForm,
  requestJson,
  requestVoid,
  resolveToposyncUrl,
};
