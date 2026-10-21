import React, { useLayoutEffect, useRef, useState } from "react";
import { requestJson, resolveToposyncUrl, type HostUi, type ImageMaskEditorProps } from "@toposync/plugin-api";
import { replaceVisualRegion, visualSource, type VisualConfiguration } from "./visualState";

type Reference = { id: string; revision: number; image_url: string; geometry: { model_digest: string; projection: string } };
type Drawing = { reference: Reference; strokes: ImageMaskEditorProps["strokes"]; context: boolean };

export function VisualStateRegionSettings({ ui, value, pipeline, disabled, advanced, change, t }: {
  ui?: Pick<HostUi, "ImageMaskEditor">; value: VisualConfiguration; pipeline: unknown; disabled: boolean;
  advanced?: boolean; change: (value: VisualConfiguration) => void; t: (key: string) => string;
}) {
  const source = visualSource(pipeline), region = value.canonical_region;
  const [drawing, setDrawing] = useState<Drawing | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const identity = JSON.stringify([source, region]);
  const locked = disabled || value.classes.some(category => category.reference_ids.length > 0);
  useLayoutEffect(() => {
    setDrawing(null); setBusy(false); setError(false);
    return () => { operation.current?.abort(); operation.current = null; };
  }, [identity, locked]);
  const compatible = !region || (source?.camera_id === region.camera_id && source?.source_id === region.source_id);
  const MaskEditor = ui?.ImageMaskEditor;

  async function edit(context = false) {
    if (locked || !source || !MaskEditor) return;
    operation.current?.abort();
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setError(false);
    try {
      const reference = await requestJson<Reference>(`/api/cameras/cameras/${encodeURIComponent(source.camera_id)}/sources/${encodeURIComponent(source.source_id)}/visual-region-reference`, { signal: controller.signal });
      controller.signal.throwIfAborted();
      const same = compatible && region?.reference.artifact_id === reference.id
        && region.reference.revision === reference.revision && region.reference.geometry_digest === reference.geometry.model_digest;
      // A context mask must belong to the exact reference used by the target drawing.
      if (context && !same) { setError(true); return; }
      setDrawing({ reference, context, strokes: same ? (context ? region?.alignment_strokes ?? [] : region?.strokes ?? []) : [] });
    } catch { if (!controller.signal.aborted) setError(true); }
    finally { if (operation.current === controller) { operation.current = null; setBusy(false); } }
  }

  function apply(strokes: ImageMaskEditorProps["strokes"]) {
    if (!drawing || !source || locked) return;
    if (!drawing.context && !strokes.some(stroke => stroke.op === "paint")) { setError(true); return; }
    const reference = drawing.reference;
    const binding = { artifact_id: reference.id, revision: reference.revision, geometry_digest: reference.geometry.model_digest };
    const same = compatible && region?.reference.artifact_id === reference.id
      && region.reference.revision === reference.revision && region.reference.geometry_digest === reference.geometry.model_digest;
    const next = { ...(same ? region : {}), ...source, name: region?.name || t("canonical_region"), framing: "passive" as const,
      reference: binding, strokes: drawing.context ? region!.strokes : strokes,
      alignment_strokes: drawing.context ? strokes : same ? region?.alignment_strokes ?? [] : [] };
    change(replaceVisualRegion(value, next));
    setDrawing(null);
  }

  return <div className="visualRegionSettings">
    {region && <p>{t("canonical_selected")} · {region.name}</p>}
    {!compatible && <p role="alert">{t("canonical_source_changed")}</p>}
    <button type="button" className="chipButton" disabled={locked || busy || !source || !MaskEditor} onClick={() => void edit()}>
      {t(region ? "canonical_edit" : "canonical_choose")}
    </button>
    {region && <button type="button" className="chipButton" disabled={locked} onClick={() => change(replaceVisualRegion(value, null))}>{t("canonical_remove")}</button>}
    {!source && <p className="identityMuted">{t("canonical_source_required")}</p>}
    {!MaskEditor && <p className="identityMuted">{t("canonical_editor_missing")}</p>}
    <p className="identityMuted">{t("canonical_passive")}</p>
    {busy && <p role="status">{t("canonical_loading")} <button type="button" className="chipButton" onClick={() => {
      operation.current?.abort(); operation.current = null; setBusy(false);
    }}>{t("cancel")}</button></p>}
    {error && <p role="alert">{t("canonical_reference_unavailable")}</p>}
    {region && advanced && <details><summary>{t("canonical_alignment")}</summary>
      <p className="identityMuted">{t("canonical_alignment_help")}</p>
      <button type="button" className="chipButton" disabled={locked || busy || !compatible} onClick={() => void edit(true)}>{t("canonical_alignment_edit")}</button>
      {(region.alignment_strokes?.length ?? 0) > 0 && <button type="button" className="chipButton" disabled={locked} onClick={() =>
        change(replaceVisualRegion(value, { ...region, alignment_strokes: [] }))}>{t("canonical_alignment_auto")}</button>}
      {([['maximum_side', 256, 32, 512], ['alignment_margin_pixels', 8, 1, 128]] as const).map(([key, fallback, min, max]) =>
        <label className="identityField" key={`${key}:${region[key]}`}><span>{t(`canonical_${key}`)}</span>
          <input type="number" min={min} max={max} step={1} required disabled={locked} defaultValue={region[key] ?? fallback}
            onBlur={event => {
              if (!event.target.validity.valid) { event.target.value = String(region[key] ?? fallback); return; }
              const valueNumber = Number(event.target.value);
              if (valueNumber !== (region[key] ?? fallback)) change(replaceVisualRegion(value, { ...region, [key]: valueNumber }));
            }} />
        </label>)}
    </details>}
    {drawing && MaskEditor && <MaskEditor open imageUrl={resolveToposyncUrl(drawing.reference.image_url)}
      wrapHorizontal={drawing.reference.geometry.projection === "equirectangular"} title={t(drawing.context ? "canonical_alignment_edit" : "canonical_choose")}
      strokes={drawing.strokes} onApply={apply} onClose={() => setDrawing(null)} />}
  </div>;
}
