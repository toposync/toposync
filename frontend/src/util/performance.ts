function shouldLogPerformanceMarks(): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (window.localStorage.getItem("toposync.performanceDebug") === "1") return true;
  } catch {
    // ignore
  }
  const host = window.location.hostname;
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

export function markToposyncPerformance(name: string, detail?: Record<string, unknown>): void {
  const fullName = `toposync:${name}`;
  try {
    if (typeof performance !== "undefined" && typeof performance.mark === "function") {
      performance.mark(fullName, detail ? { detail } : undefined);
    }
  } catch {
    // ignore
  }

  if (!shouldLogPerformanceMarks()) return;
  const payload = detail ? { ...detail } : undefined;
  // eslint-disable-next-line no-console
  console.debug(`[perf] ${fullName}`, payload ?? "");
}
