import React, { useEffect, useState } from "react";
import { createCameraPipelinePreset } from "../api/camerasApi";
import { SubModal } from "../ui/SubModal";
import type { CameraPipelinePresetModal } from "./CameraPipelinePresetModal";

export function CameraObservationPresetModal({
  open, preset, camera, activeSourceId, mappedCompositions, processingServers, i18n, onClose, onCreated,
}: React.ComponentProps<typeof CameraPipelinePresetModal>): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const sources = camera.sources.filter((source) => source.enabled && source.kind === "video");
  const [sourceId, setSourceId] = useState("");
  const [name, setName] = useState("");
  const [areaId, setAreaId] = useState("");
  const [serverId, setServerId] = useState("local");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setSourceId(sources.find((source) => source.id === activeSourceId)?.id || sources[0]?.id || "");
    setName("");
    setAreaId("");
    setServerId("local");
    setError(null);
  }, [open, preset, camera.id, activeSourceId]);
  const area = preset === "area_presence" || preset === "area_entry" || preset === "area_group_entry";
  if (!open || (!area && preset !== "cat_litter_use" && preset !== "cat_drinking")) return null;
  const selectedArea = mappedCompositions.flatMap(composition => (composition.areas ?? []).map(item => ({...item, compositionId: composition.id}))).find(item => JSON.stringify([item.compositionId, item.id]) === areaId);
  const key = area ? `ext.cameras.area.${preset === "area_presence" ? "presence" : preset === "area_group_entry" ? "group" : "entry"}` : `ext.cameras.cat.${preset === "cat_litter_use" ? "litter" : "water"}`;
  async function save(): Promise<void> {
    if (saving || !sourceId || !preset || (area && !selectedArea)) return;
    setSaving(true);
    setError(null);
    try {
      const result = await createCameraPipelinePreset(camera.id, {
        preset, source_id: sourceId, pipeline_name: name.trim(), enabled: false,
        processing_server_id: serverId, area_id: selectedArea?.id ?? "", composition_id: selectedArea?.compositionId ?? "",
      });
      onCreated(result.pipeline_name);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }
  return <SubModal open={open} title={t(`${key}.title`)} onClose={() => { if (!saving) onClose(); }}
    panelStyle={{ width: "min(540px, calc(100vw - 28px))" }}>
    <p>{t(`${key}.description`)}</p>
    <p>{t("ext.cameras.cat.preview")}</p>
    {sources.length > 1 ? <div className="field">
      <label className="label" htmlFor="cat-recipe-source">{t("ext.cameras.cat.source")}</label>
      <select id="cat-recipe-source" className="input" value={sourceId} onChange={(event) => setSourceId(event.target.value)} disabled={saving}>
        {sources.map((source) => <option key={source.id} value={source.id}>{source.name || source.id}</option>)}
      </select>
    </div> : null}
    {area ? <label className="field"><span className="label">{t("ext.cameras.area.required")}</span>
      <select className="input" value={areaId} onChange={event => setAreaId(event.target.value)} disabled={saving}>
        <option value="">{t("ext.cameras.area.select")}</option>
        {mappedCompositions.map(composition => <optgroup key={composition.id} label={composition.name || composition.id}>
          {(composition.areas ?? []).filter(item => item.vertices_count >= 3).map(item => <option key={item.id} value={JSON.stringify([composition.id, item.id])}>{item.name || item.id}</option>)}
        </optgroup>)}
      </select>
    </label> : <p className="settingsStatusMuted" style={{ whiteSpace: "normal", overflow: "visible" }}>{t("ext.cameras.cat.profile")}</p>}
    <details><summary>{t("ext.cameras.cat.advanced")}</summary>
      <label className="field"><span className="label">{t("ext.cameras.pipeline_preset.processing_server", {}, "Processing server")}</span>
        <select className="input" value={serverId} onChange={event => setServerId(event.target.value)} disabled={saving}>
          {processingServers.map(server => <option key={server.id} value={server.id}>{server.name || server.id}</option>)}
        </select>
      </label>
      <div className="field"><label className="label" htmlFor="cat-recipe-name">{t("ext.cameras.cat.name")}</label>
        <input id="cat-recipe-name" className="input" value={name} onChange={(event) => setName(event.target.value)} disabled={saving} />
      </div>
    </details>
    <p className="settingsStatusMuted" style={{ whiteSpace: "normal", overflow: "visible" }}>{t("ext.cameras.cat.paused")}</p>
    {error ? <p role="alert" className="errorText">{error}</p> : null}
    <button className="primaryButton" type="button" disabled={saving || !sourceId || (area && !selectedArea)} onClick={() => void save()}>
      {t(saving ? "ext.cameras.cat.saving" : "ext.cameras.cat.save")}
    </button>
  </SubModal>;
}
