import React, { useMemo, useRef, useState } from "react";
import type { CompositionElement, ElementType, Notification, Notification2DOverlay, NotificationRenderer } from "@toposync/plugin-api";
import { readGeoreference, type Georeference } from "../util/georeference";
import { Modal } from "./Modal";
import { i18n } from "../util/i18n";
import { GeographicMap } from "./geographic/GeographicMap";

const initial: Georeference = { version: 1, latitude: 0, longitude: 0, rotationDegrees: 0, scale: 1, axes: "x-east-z-south", units: "metres" };
export function GeoreferenceEditor({ activeNotification, notificationRenderers, compositionId, value, elements, elementTypesById, onSave, onClose }: {
  activeNotification: Notification | null; notificationRenderers: NotificationRenderer[]; compositionId: string;
  value: unknown; elements: CompositionElement[]; elementTypesById: Record<string, ElementType>;
  onSave: (value: Georeference, expected: unknown) => Promise<void>; onClose: () => void;
}): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const expected = useRef(value);
  const [reference, setReference] = useState(() => readGeoreference(value) ?? initial);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const eventPin = useMemo(() => {
    if (!activeNotification) return null;
    const renderer = notificationRenderers.find(item => item.type === activeNotification.type);
    let overlay: Notification2DOverlay | null | undefined;
    try {
      overlay = renderer?.create2DOverlay?.({ compositionId, elements }, activeNotification, { openImage: () => undefined });
      const pin = overlay?.pin();
      return pin && Number.isFinite(pin.x) && Number.isFinite(pin.z) && Math.hypot(pin.x, pin.z) * reference.scale <= 20000 ? pin : null;
    } catch { return null; }
    finally { overlay?.dispose?.(); }
  }, [activeNotification, notificationRenderers, compositionId, elements, reference.scale]);
  const compositions = useMemo(() => [{ id: compositionId, name: t("core.map.title"), elements, meta: { georeference: reference } }], [compositionId, elements, reference, t]);
  return <Modal open title={t("core.map.title")} onClose={onClose} panelStyle={{ width: "min(960px, 96vw)" }} bodyClassName="georeferenceEditor">
    <p>{t("core.map.intro")}</p>
    <div className="georeferenceMap">
      <GeographicMap compositions={compositions} activeCompositionId={compositionId} elementTypesById={elementTypesById}
        notifications={activeNotification ? [activeNotification] : []} notificationRenderers={notificationRenderers}
        onAnchorChange={point => setReference(previous => ({ ...previous, ...point }))} />
    </div>
    {activeNotification && <p role="status">{eventPin ? t("core.map.event_recorded") : t("core.map.event_unlocated")} {activeNotification.title}{activeNotification.createdAt && Number.isFinite(Date.parse(activeNotification.createdAt)) && <> · <time dateTime={activeNotification.createdAt}>{new Date(activeNotification.createdAt).toLocaleString(locale)}</time></>}</p>}
    <div className="georeferenceControls">
      <label className="field"><span>{t("core.map.orientation")}: {reference.rotationDegrees}°</span><input aria-label={t("core.map.orientation_aria")} type="range" min={-180} max={180} value={reference.rotationDegrees} onChange={event => setReference({ ...reference, rotationDegrees: Number(event.target.value) })} /></label>
      <label className="field"><span>{t("core.map.scale")}: {reference.scale.toFixed(2)}×</span><input aria-label={t("core.map.scale_aria")} type="range" min={0.1} max={5} step={0.01} value={reference.scale} onChange={event => setReference({ ...reference, scale: Number(event.target.value) })} /></label>
    </div>
    <details className="georeferenceAdvanced"><summary>{t("core.map.advanced")}</summary>
      <p>{t("core.map.precision")}</p>
      <div className="georeferenceControls">
        <label className="field"><span>{t("core.map.latitude")}</span><input className="input" aria-label={t("core.map.latitude")} type="number" min={-85} max={85} step="any" value={reference.latitude} onChange={event => { const latitude = Number(event.target.value); if (Number.isFinite(latitude) && Math.abs(latitude) <= 85) setReference({ ...reference, latitude }); }} /></label>
        <label className="field"><span>{t("core.map.longitude")}</span><input className="input" aria-label={t("core.map.longitude")} type="number" min={-180} max={180} step="any" value={reference.longitude} onChange={event => { const longitude = Number(event.target.value); if (Number.isFinite(longitude) && Math.abs(longitude) <= 180) setReference({ ...reference, longitude }); }} /></label>
      </div>
    </details>
    {error && <p role="alert">{t(error)}</p>}
    <div className="georeferenceActions">
      <button className="chipButton" type="button" onClick={onClose}>{t("core.actions.cancel")}</button>
      <button className="primaryButton" disabled={saving} type="button" onClick={async () => { setSaving(true); setError(""); try { await onSave(reference, expected.current); onClose(); } catch { setError("core.map.save_failed"); } finally { setSaving(false); } }}>{saving ? t("core.map.saving") : t("core.map.save")}</button>
    </div>
  </Modal>;
}
