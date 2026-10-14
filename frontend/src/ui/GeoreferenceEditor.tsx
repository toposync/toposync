import React, { useEffect, useMemo, useRef, useState } from "react";
import type { CompositionElement, ElementType, Notification, Notification2DOverlay, NotificationRenderer } from "@toposync/plugin-api";
import { readGeoreference, type Georeference } from "../util/georeference";
import { Modal } from "./Modal";
import { Icon } from "./Icon";
import { i18n } from "../util/i18n";
import { GeographicMap } from "./geographic/GeographicMap";

function NumericField({ value, min, max, label, onValue, onValidity }: { value: number; min: number; max: number; label: string; onValue: (value: number) => void; onValidity: (valid: boolean) => void }) {
  const [draft, setDraft] = useState(String(value));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => { setDraft(previous => previous !== "" && Number(previous) === value ? previous : String(value)); setInvalid(false); }, [value]);
  return <label className="field"><span>{label}</span><input className="input" aria-invalid={invalid} type="number" min={min} max={max} step="any" required value={draft}
    onChange={event => {
      const text = event.target.value;
      const valid = text !== "" && event.target.validity.valid && Number.isFinite(event.target.valueAsNumber);
      setDraft(text); setInvalid(!valid); onValidity(valid);
      if (valid) onValue(event.target.valueAsNumber);
    }} /></label>;
}

const initial: Georeference = { version: 1, latitude: 0, longitude: 0, rotationDegrees: 0, scale: 1, axes: "x-east-z-south", units: "metres" };
export function GeoreferenceEditor({ activeNotification, notificationRenderers, compositionId, value, elements, elementTypesById, onSave, onClose }: {
  activeNotification: Notification | null; notificationRenderers: NotificationRenderer[]; compositionId: string;
  value: unknown; elements: CompositionElement[]; elementTypesById: Record<string, ElementType>;
  onSave: (value: Georeference, expected: unknown) => Promise<void>; onClose: () => void;
}): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const expected = useRef(value);
  const original = useRef(readGeoreference(value) ?? initial);
  const [reference, setReference] = useState(original.current);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const pending = useRef(false);
  const [positioning, setPositioning] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [invalidFields, setInvalidFields] = useState<string[]>([]);
  const [formRevision, setFormRevision] = useState(0);
  const dirty = JSON.stringify(reference) !== JSON.stringify(original.current) || invalidFields.length > 0;
  const requestClose = () => { if (pending.current) return; if (dirty) setDiscardOpen(true); else onClose(); };
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
  const numericField = (key: "latitude" | "longitude" | "rotationDegrees" | "scale", label: string, min: number, max: number) => <NumericField key={`${key}-${formRevision}`}
    value={reference[key]} min={min} max={max} label={t(label)}
    onValue={number => setReference(previous => ({ ...previous, [key]: number }))}
    onValidity={valid => setInvalidFields(previous => valid ? previous.filter(item => item !== key) : [...new Set([...previous, key])])} />;
  return <Modal open title={t("core.map.position")} onClose={requestClose} panelStyle={{ width: "min(960px, 96vw)" }} bodyClassName="georeferenceEditor">
    <section className="georeferencePreview">
      <p className="muted">{t("core.map.intro")}</p>
      <div className="georeferenceMap">
        <GeographicMap editing compositions={compositions} activeCompositionId={compositionId} elementTypesById={elementTypesById}
          notifications={activeNotification ? [activeNotification] : []} notificationRenderers={notificationRenderers} activeNotificationId={activeNotification?.id}
          onAnchorChange={positioning && !saving ? point => setReference(previous => ({ ...previous, ...point })) : undefined} />
      </div>
      <label className="georeferencePositionMode"><input type="checkbox" checked={positioning} disabled={saving} onChange={event => setPositioning(event.target.checked)} /> {t("core.map.anchor_mode")}</label>
    </section>
    <form onSubmit={async event => {
      event.preventDefault();
      if (pending.current || invalidFields.length) return;
      pending.current = true; setSaving(true); setError("");
      try { await onSave(reference, expected.current); onClose(); }
      catch (reason) { setError([409, 412].includes((reason as { status?: number }).status ?? 0) ? "core.map.save_conflict" : "core.map.save_failed"); }
      finally { pending.current = false; setSaving(false); }
    }}>

      {positioning && <p role="status">{t("core.map.anchor_hint")}</p>}
      <fieldset disabled={saving} className="georeferenceFields">

        <div className="georeferenceControls">
          <div>{numericField("rotationDegrees", "core.map.orientation", -180, 180)}<input aria-label={t("core.map.orientation_aria")} type="range" min={-180} max={180} step={0.1} value={reference.rotationDegrees} onChange={event => { setInvalidFields(previous => previous.filter(key => key !== "rotationDegrees")); setReference({ ...reference, rotationDegrees: Number(event.target.value) }); }} /></div>
          <div>{numericField("scale", "core.map.scale", 0.1, 5)}<input aria-label={t("core.map.scale_aria")} type="range" min={0.1} max={5} step={0.01} value={reference.scale} onChange={event => { setInvalidFields(previous => previous.filter(key => key !== "scale")); setReference({ ...reference, scale: Number(event.target.value) }); }} /></div>
        </div>
        <details className="georeferenceAdvanced"><summary>{t("core.map.advanced")}</summary><p>{t("core.map.orientation_hint")}</p><p>{t("core.map.precision")}</p>      {activeNotification && <p role="status">{eventPin ? t("core.map.event_recorded") : t("core.map.event_unlocated")} {activeNotification.title}{activeNotification.createdAt && Number.isFinite(Date.parse(activeNotification.createdAt)) && <> · <time dateTime={activeNotification.createdAt}>{new Date(activeNotification.createdAt).toLocaleString(locale)}</time></>}</p>}<div className="georeferenceControls">
          {numericField("latitude", "core.map.latitude", -85, 85)}{numericField("longitude", "core.map.longitude", -180, 180)}
        </div></details>
      </fieldset>
      {invalidFields.length > 0 && <p className="errorText" role="alert">{t("core.map.invalid")}</p>}
      {error && <p className="errorText" role="alert">{t(error)}</p>}
      <div className="georeferenceActions">
        <button className="chipButton" title={t("core.map.undo")} type="button" disabled={saving || !dirty} onClick={() => { setReference(original.current); setInvalidFields([]); setFormRevision(previous => previous + 1); setError(""); }}><Icon name="rotate-left" /><span className="georeferenceUndoLabel">{t("core.map.undo")}</span></button>
        <button className="chipButton" disabled={saving} type="button" onClick={requestClose}>{t("core.actions.cancel")}</button>
        <button className="primaryButton" disabled={saving || invalidFields.length > 0} type="submit">{t(saving ? "core.map.saving" : "core.map.save")}</button>
      </div>
    </form>
    <Modal open={discardOpen} title={t("core.map.unsaved")} onClose={() => setDiscardOpen(false)}><div className="rowWrap">
      <button type="button" className="primaryButton" onClick={() => setDiscardOpen(false)}>{t("core.map.keep_editing")}</button>
      <button type="button" className="chipButton" onClick={onClose}>{t("core.map.discard")}</button>
    </div></Modal>
  </Modal>;
}
