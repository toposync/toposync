import React, { useEffect, useState } from "react";
import type { AttentionCameraCatalogItem, AttentionProfile } from "../types";

type Props = {
  t: (key: string) => string;
  cameras: AttentionCameraCatalogItem[];
  profile: AttentionProfile;
  occupied: ReadonlySet<string>;
  onChange: (profile: AttentionProfile) => void;
  sharedObserver: boolean;
  globalConfigure: boolean;
};

export function PanoramaAttentionFields({ t, cameras, profile, occupied, onChange, sharedObserver, globalConfigure }: Props): React.ReactElement {
  const [region, setRegion] = useState((profile.attention_region ?? []).map(point => `${point.x} ${point.z}`).join("\n"));
  const camera = cameras.find(item => item.id === profile.camera_id);
  const mappings = (camera?.panorama_mappings ?? []).filter(item => item.source_id === profile.source_id);
  useEffect(() => {
    setRegion((profile.attention_region ?? []).map(point => `${point.x} ${point.z}`).join("\n"));
  }, [profile.id]);
  const clearContext = { panorama_element_id: "", composition_id: "", home_target: null, attention_region: [] };
  return <section>
    <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.panorama.title")}</h3>
    <p className="ptzAttentionMuted">{t("ext.ptz_attention.panorama.help")}</p>
    <div className="ptzAttentionFields">
      <label className="ptzAttentionField"><span>{t("ext.ptz_attention.operator.camera")}</span>
        <select className="input" value={profile.camera_id} onChange={event => {
          onChange({ ...profile, ...clearContext, camera_id: event.target.value, ptz_device_id: event.target.value, source_id: "", same_head_observer_acknowledged: false });
          setRegion("");
        }}>
          <option value="">-</option>
          {cameras.filter(item => globalConfigure || item.permissions.configure).map(item => <option key={item.id} value={item.id} disabled={!item.enabled || !item.actuator_id || occupied.has(item.id)}>{item.name || item.id}</option>)}
        </select>
      </label>
      <label className="ptzAttentionField"><span>{t("ext.ptz_attention.operator.source")}</span>
        <select className="input" value={profile.source_id} onChange={event => {
          onChange({ ...profile, ...clearContext, source_id: event.target.value }); setRegion("");
        }}>
          <option value="">-</option>
          {(camera?.sources ?? []).filter(item => item.enabled && item.kind === "video" && item.has_ptz).map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
        </select>
      </label>
      <label className="ptzAttentionField"><span>{t("ext.ptz_attention.panorama.mapping")}</span>
        <select className="input" value={profile.panorama_element_id || ""} onChange={event => {
          const mapping = mappings.find(item => item.element_id === event.target.value);
          onChange({ ...profile, ...clearContext, panorama_element_id: mapping?.element_id || "", composition_id: mapping?.composition_id || "" }); setRegion("");
        }}>
          <option value="">-</option>
          {mappings.map(item => <option key={item.element_id} value={item.element_id} disabled={!item.ready}>{item.composition_name} · {item.element_name || camera?.name}{item.ready ? "" : ` · ${t("ext.ptz_attention.panorama.review")}`}</option>)}
        </select>
      </label>
      {(["x", "z"] as const).map(axis => <label className="ptzAttentionField" key={axis}>
        <span>{t("ext.ptz_attention.panorama.home")} {axis.toUpperCase()}</span>
        <input className="input" type="number" step="any" value={profile.home_target?.[axis] ?? ""} onChange={event => onChange({ ...profile, home_target: event.target.value === "" ? null : { x: profile.home_target?.x ?? 0, z: profile.home_target?.z ?? 0, [axis]: Number(event.target.value) } })} />
      </label>)}
    </div>
    <label className="ptzAttentionField"><span>{t("ext.ptz_attention.panorama.region")}</span>
      <textarea className="input" rows={5} value={region} placeholder={"0 0\n10 0\n10 10\n0 10"} onChange={event => {
        setRegion(event.target.value);
        const points = event.target.value.trim().split(/\n/).map(line => line.trim().split(/[;\s]+/).map(Number));
        const valid = points.every(point => point.length === 2 && point.every(Number.isFinite));
        onChange({ ...profile, attention_region: valid ? points.map(([x, z]) => ({ x, z })) : [] });
      }} />
      <span className="ptzAttentionMuted">{t("ext.ptz_attention.panorama.region_help")}</span>
    </label>
    {sharedObserver ? <label className="ptzAttentionField"><span><input type="checkbox" checked={profile.same_head_observer_acknowledged}
      onChange={event => onChange({ ...profile, same_head_observer_acknowledged: event.target.checked })} /> {t("ext.ptz_attention.wizard.camera.shared_ack")}</span></label> : null}
  </section>;
}
