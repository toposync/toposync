import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { HostI18n, HostUi, NavigableViewportController } from "@toposync/plugin-api";
import { prepareObjectImage } from "./imagePreparation";

export type ObjectContour = [number, number][];
type PreparedContour = { points: ObjectContour; prepared: Awaited<ReturnType<typeof prepareObjectImage>> };

export function editObjectContour(file: File, ui: HostUi, t: HostI18n["t"], signal: AbortSignal, initial?: ObjectContour): Promise<PreparedContour | null> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container), url = URL.createObjectURL(file);
    let settled = false;
    function cleanup() { signal.removeEventListener("abort", abort); root.unmount(); container.remove(); URL.revokeObjectURL(url); }
    function finish(result: PreparedContour | null) { if (!settled) { settled = true; cleanup(); resolve(result); } }
    function abort() { if (!settled) { settled = true; cleanup(); reject(signal.reason ?? new DOMException("Cancelled", "AbortError")); } }
    signal.addEventListener("abort", abort, { once: true });
    root.render(<ContourEditor file={file} url={url} ui={ui} t={t} signal={signal} initial={initial} finish={finish} />);
  });
}

function ContourEditor({ file, url, ui, t, signal, initial, finish }: {
  file: File; url: string; ui: HostUi; t: HostI18n["t"]; signal: AbortSignal; initial?: ObjectContour; finish: (result: PreparedContour | null) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), heading = useRef<HTMLHeadingElement>(null);
  const viewport = useRef<HTMLDivElement>(null), navigation = useRef<NavigableViewportController | null>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [points, setPoints] = useState<ObjectContour>(initial ?? []);
  const [cursor, setCursor] = useState({ x: .5, y: .5 });
  const [scale, setScale] = useState(1), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    dialog.current!.showModal(); heading.current?.focus();
    const image = new Image(); image.src = url;
    let active = true;
    void image.decode().then(() => { if (active) setSize({ width: image.naturalWidth, height: image.naturalHeight }); }).catch(() => { if (active) setError(t("ext.models.error.invalid_prepared_image")); });
    return () => { active = false; request.current?.abort(); };
  }, [url, t]);
  function change(next: ObjectContour) { setPoints(next); setError(""); }
  function add(point: { x: number; y: number }) {
    if (busy || points.length >= 64 || point.x < 0 || point.y < 0 || point.x > 1 || point.y > 1) return;
    if (points.some(([x, y]) => x === point.x && y === point.y)) return;
    setCursor(point); change([...points, [point.x, point.y]]);
  }
  function keyboard(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget || !size || busy) return;
    if (event.key === "Enter") { event.preventDefault(); add(cursor); return; }
    if (event.key === "Backspace" || event.key === "Delete") { event.preventDefault(); change(points.slice(0, -1)); return; }
    const direction = ({ ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] } as Record<string, number[]>)[event.key];
    if (!direction) return;
    event.preventDefault();
    const step = event.shiftKey ? 50 : event.altKey ? 1 : 5;
    const next = { x: Math.max(0, Math.min(1, cursor.x + direction[0] * step / (size.width - 1))), y: Math.max(0, Math.min(1, cursor.y + direction[1] * step / (size.height - 1))) };
    setCursor(next);
    const position = { x: next.x * (size.width - 1), y: next.y * (size.height - 1) };
    const screen = navigation.current?.contentToScreen(position), element = viewport.current;
    if (screen && element && (screen.x < 16 || screen.y < 16 || screen.x > element.clientWidth - 16 || screen.y > element.clientHeight - 16)) navigation.current?.centerOn(position);
  }
  async function apply() {
    if (busy || points.length < 3) return;
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError("");
    try {
      const prepared = await prepareObjectImage(file, controller.signal, points);
      signal.throwIfAborted(); controller.signal.throwIfAborted();
      finish({ points, prepared });
    } catch (failure) {
      if (signal.aborted || controller.signal.aborted) return;
      const code = failure instanceof Error ? failure.message : "";
      setError(t(`ext.models.error.${["invalid_foreground_polygon", "isolated_object_image_required", "generation_image_too_large", "local_generation_busy"].includes(code) ? code : "invalid_prepared_image"}`));
      setBusy(false);
    }
  }
  const button: React.CSSProperties = { minWidth: 44, minHeight: 44 };
  return <dialog ref={dialog} aria-label={t("ext.models.contour.title")} onCancel={event => { event.preventDefault(); finish(null); }} style={{ display: "flex", flexDirection: "column", width: "min(900px,calc(100vw - 28px))", maxHeight: "90dvh", overflow: "hidden", boxSizing: "border-box", padding: 24, border: "1px solid var(--color-border-strong,#64748b)", borderRadius: 18, background: "var(--color-surface-solid,#fff)", color: "var(--color-text-primary,#101418)" }}>
    <h2 ref={heading} tabIndex={-1} style={{ fontSize: 18, margin: 0, lineHeight: 1.4, flexShrink: 0 }}>{t("ext.models.contour.title")}</h2>
    <div tabIndex={0} role="region" aria-label={t("ext.models.contour.tools")} style={{ minHeight: 0, overflow: "auto", marginTop: 12 }}>
      <p style={{ margin: "0 0 12px", lineHeight: 1.5 }}>{t("ext.models.contour.hint")}</p>
      <div role="group" aria-label={t("ext.models.contour.tools")} style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        <button type="button" className="iconButton" style={button} disabled={!size || busy} aria-label={t("ext.models.contour.zoom_out")} onClick={() => navigation.current?.zoomBy(1 / 1.5)}>−</button>
        <button type="button" className="iconButton" style={button} disabled={!size || busy} aria-label={t("ext.models.contour.zoom_in")} onClick={() => navigation.current?.zoomBy(1.5)}>+</button>
        <button type="button" className="chipButton" style={button} disabled={!size || busy} onClick={() => navigation.current?.fit()}>{t("ext.models.contour.fit")}</button>
        <button type="button" className="chipButton" style={button} disabled={!points.length || busy} onClick={() => change(points.slice(0, -1))}>{t("ext.models.contour.undo")}</button>
        <button type="button" className="chipButton" style={button} disabled={!points.length || busy} onClick={() => change([])}>{t("ext.models.contour.clear")}</button>
      </div>
      {size && <ui.NavigableViewport contentSize={size} contentKey={url} controllerRef={navigation} viewportRef={viewport} label={t("ext.models.contour.canvas")} style={{ height: "clamp(120px,28dvh,240px)", background: "#e2e8f0", borderRadius: 12 }} onViewChange={view => setScale(view.scale)} onContentClick={point => add({ x: point.x / (size.width - 1), y: point.y / (size.height - 1) })} onKeyDown={keyboard}>
        <img src={url} alt="" draggable={false} style={{ width: size.width, height: size.height, display: "block", pointerEvents: "none" }} />
        <svg aria-hidden="true" width={size.width} height={size.height} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          <polygon points={points.map(([x, y]) => `${x * (size.width - 1)},${y * (size.height - 1)}`).join(" ")} fill="rgba(15,118,110,.16)" stroke="#0f766e" strokeWidth={3 / scale} />
          {points.map(([x, y], index) => <circle key={index} cx={x * (size.width - 1)} cy={y * (size.height - 1)} r={5 / scale} fill="#fff" stroke="#0f766e" strokeWidth={2 / scale} />)}
          <path d={`M${cursor.x * (size.width - 1) - 10 / scale},${cursor.y * (size.height - 1)}h${20 / scale} M${cursor.x * (size.width - 1)},${cursor.y * (size.height - 1) - 10 / scale}v${20 / scale}`} stroke="#fff" strokeWidth={4 / scale} />
          <path d={`M${cursor.x * (size.width - 1) - 10 / scale},${cursor.y * (size.height - 1)}h${20 / scale} M${cursor.x * (size.width - 1)},${cursor.y * (size.height - 1) - 10 / scale}v${20 / scale}`} stroke="#111827" strokeWidth={2 / scale} />
        </svg>
      </ui.NavigableViewport>}
      <p role="status" style={{ margin: "12px 0 0", lineHeight: 1.5 }}>{busy ? t("ext.models.editor.preparing") : error || t("ext.models.contour.points", { count: points.length })}</p>
      <details style={{ marginTop: 12 }}><summary>{t("ext.models.contour.keyboard")}</summary><p style={{ lineHeight: 1.5 }}>{t("ext.models.contour.keyboard_hint")}</p></details>
    </div>
    <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "flex-end", gap: 12, flexShrink: 0, marginTop: 16, paddingTop: 16, borderTop: "1px solid var(--color-border-subtle,#64748b)" }}>
      <button type="button" className="chipButton" style={button} onClick={() => finish(null)}>{t("ext.models.contour.back")}</button>
      <button type="button" className="pillButton pillButtonPrimary" style={button} disabled={!size || busy || points.length < 3} onClick={() => void apply()}>{t("ext.models.contour.apply")}</button>
    </div>
  </dialog>;
}
