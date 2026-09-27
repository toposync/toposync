import React, { useEffect, useRef, useState } from "react";
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import { Vector2 } from "three";
import type { CompositionElement, ElementType, Viewport2DContext } from "@toposync/plugin-api";
import { localToGeographic, readGeoreference, type Georeference } from "../util/georeference";
import { Modal } from "./Modal";
import { i18n } from "../util/i18n";

const initial: Georeference = { version: 1, latitude: 0, longitude: 0, rotationDegrees: 0, scale: 1, axes: "x-east-z-south", units: "metres" };
// Configuração do provedor separada da geometria da composição.
const tiles = { url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' };
export function GeoreferenceEditor({ value, elements, elementTypesById, onSave, onClose }: {
  value: unknown; elements: CompositionElement[]; elementTypesById: Record<string, ElementType>;
  onSave: (value: Georeference, expected: unknown) => Promise<void>; onClose: () => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const expected = useRef(value);
  const [reference, setReference] = useState(() => readGeoreference(value) ?? initial);
  const [background, setBackground] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const map = useRef<L.Map | null>(null);
  const anchor = useRef<L.Marker | null>(null);
  useEffect(() => {
    if (!container.current) return;
    const instance = L.map(container.current, { attributionControl: true }).setView([reference.latitude, reference.longitude], 19);
    map.current = instance;
    L.control.scale({ imperial: false }).addTo(instance);
    instance.on("click", (event: L.LeafletMouseEvent) => setReference(previous => ({ ...previous, latitude: Math.max(-85, Math.min(85, event.latlng.lat)), longitude: event.latlng.wrap().lng })));
    const origin = L.marker([reference.latitude, reference.longitude], { draggable: true, keyboard: true, title: t("core.map.drag"), icon: L.divIcon({ html: "+", className: "", iconSize: [24, 24], iconAnchor: [12, 12] }) }).addTo(instance);
    const handle = origin.getElement();
    if (handle) Object.assign(handle.style, { fontSize: "28px", lineHeight: "24px", textAlign: "center", color: "#ef4444", fontWeight: "bold", textShadow: "0 0 3px white" });
    origin.on("dragend", () => { const position = origin.getLatLng().wrap(); setReference(previous => ({ ...previous, latitude: Math.max(-85, Math.min(85, position.lat)), longitude: position.lng })); });
    anchor.current = origin;
    return () => { instance.remove(); map.current = null; anchor.current = null; };
  }, []);
  useEffect(() => { anchor.current?.setLatLng([reference.latitude, reference.longitude]); }, [reference]);
  useEffect(() => {
    if (!map.current || !background) return;
    const layer = L.tileLayer(tiles.url, { attribution: tiles.attribution, maxZoom: 19, updateWhenIdle: true, keepBuffer: 0 });
    layer.on("tileerror", () => setError("core.map.offline"));
    layer.addTo(map.current);
    return () => { layer.remove(); };
  }, [background]);
  useEffect(() => {
    const instance = map.current;
    if (!instance || !canvas.current) return;
    function draw() {
      const output = canvas.current;
      if (!output || !instance) return;
      const context = output.getContext("2d");
      if (!context) return;
      const { x: width, y: height } = instance.getSize();
      const dpr = window.devicePixelRatio || 1;
      output.width = width * dpr; output.height = height * dpr;
      output.style.width = `${width}px`; output.style.height = `${height}px`;
      context.scale(dpr, dpr);
      const origin = instance.latLngToContainerPoint([reference.latitude, reference.longitude]);
      const east = localToGeographic({ x: 1, z: 0 }, { ...reference, rotationDegrees: 0 });
      const scale = instance.project(east).distanceTo(instance.project([reference.latitude, reference.longitude]));
      context.translate(origin.x, origin.y);
      context.rotate(reference.rotationDegrees * Math.PI / 180);
      const viewport: Viewport2DContext = { canvas: output, width, height, dpr, scale,
        worldToScreen: point => new Vector2(point.x * scale, point.z * scale),
        screenToWorld: point => ({ x: point.x / scale, z: point.y / scale }) };
      const rank = (element: CompositionElement) => {
        const group = elementTypesById[element.type]?.layerGroup;
        return group === "background" ? (element.props.mode === "tracing" ? 0.5 : -1) : group === "areas" ? 0 : group === "walls" ? 1 : group === "measurements" ? 3 : 2;
      };
      for (const element of [...elements].sort((a, b) => rank(a) - rank(b))) {
        context.save();
        const renderer = elementTypesById[element.type]?.render2D;
        if (renderer) renderer({ ctx: context, element, elements, viewport });
        else { context.fillStyle = "#1673c9"; context.beginPath(); context.arc(element.position.x * scale, element.position.z * scale, 5, 0, Math.PI * 2); context.fill(); }
        context.restore();
      }
    }
    draw();
    instance.on("move zoom resize", draw);
    const output = canvas.current;
    output.addEventListener("toposync:invalidate", draw);
    return () => { instance.off("move zoom resize", draw); output.removeEventListener("toposync:invalidate", draw); };
  }, [reference, elements, elementTypesById]);
  return <Modal open title={t("core.map.title")} onClose={onClose} panelStyle={{ width: "min(960px, 96vw)" }}>
    <p>{t("core.map.intro")}</p>
    <div style={{ position: "relative", height: "min(52vh, 480px)", minHeight: 220 }}>
      <div ref={container} aria-label={t("core.map.aria")} style={{ height: "100%", background: "#e4e8ec" }} />
      <canvas ref={canvas} aria-hidden="true" style={{ position: "absolute", inset: 0, pointerEvents: "none", zIndex: 450 }} />
    </div>
    <label style={{ display: "block", margin: "12px 0" }}><input type="checkbox" checked={background} onChange={event => setBackground(event.target.checked)} /> {t("core.map.background")}</label>
    <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
      <label>{t("core.map.orientation")}: {reference.rotationDegrees}°<input aria-label={t("core.map.orientation_aria")} type="range" min={-180} max={180} value={reference.rotationDegrees} onChange={event => setReference({ ...reference, rotationDegrees: Number(event.target.value) })} /></label>
      <label>{t("core.map.scale")}: {reference.scale.toFixed(2)}×<input aria-label={t("core.map.scale_aria")} type="range" min={0.1} max={5} step={0.01} value={reference.scale} onChange={event => setReference({ ...reference, scale: Number(event.target.value) })} /></label>
    </div>
    <details><summary>{t("core.map.advanced")}</summary>
      <p>{t("core.map.precision")}</p>
      <label>{t("core.map.latitude")}<input aria-label={t("core.map.latitude")} type="number" min={-85} max={85} step="any" value={reference.latitude} onChange={event => { const latitude = Number(event.target.value); if (Number.isFinite(latitude) && Math.abs(latitude) <= 85) setReference({ ...reference, latitude }); }} /></label>
      <label>{t("core.map.longitude")}<input aria-label={t("core.map.longitude")} type="number" min={-180} max={180} step="any" value={reference.longitude} onChange={event => { const longitude = Number(event.target.value); if (Number.isFinite(longitude) && Math.abs(longitude) <= 180) setReference({ ...reference, longitude }); }} /></label>
      <button type="button" onClick={() => map.current?.setView([reference.latitude, reference.longitude], 19)}>{t("core.map.anchor")}</button>
    </details>
    {error && <p role="alert">{t(error)}</p>}
    <button className="primaryButton" disabled={saving} type="button" onClick={async () => { setSaving(true); setError(""); try { await onSave(reference, expected.current); onClose(); } catch { setError("core.map.save_failed"); } finally { setSaving(false); } }}>{saving ? t("core.map.saving") : t("core.map.save")}</button>
  </Modal>;
}
