import React, { useEffect, useRef, useState } from "react";
import type { PipelineOperatorPanel } from "@toposync/plugin-api";
import { fetchCamerasIndex } from "../api/camerasApi";
import type { CamerasIndex, CameraOnvifOptimization } from "../types";
import { CameraOnvifRecipeOptimization } from "../settings/CameraOnvifRecipeOptimization";

type Props = Parameters<PipelineOperatorPanel["render"]>[0];
type Binding = { source_id: string; profile_token: string; signal: CameraOnvifOptimization };

export function AutomaticNativeObservationPanel({ config, sources = [], observationCategories = [], replaceConfig, onValidityChange, i18n }: Props) {
  const { t } = i18n.useI18n();
  const enabled = config.enabled === true;
  const bindings = (config.sources || {}) as Record<string, Binding>;
  const [index, setIndex] = useState<CamerasIndex | null>(null);
  const [error, setError] = useState(false);
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const validity = useRef(onValidityChange);
  validity.current = onValidityChange;
  const categories = JSON.stringify([...observationCategories].sort());
  const qualifiedCategories = JSON.stringify([...(config.categories as string[] || [])].sort());
  const matchingCategories = categories === qualifiedCategories;
  const sourceIds = JSON.stringify(sources.map(source => source.camera_id).sort());
  useEffect(() => {
    if (enabled && Object.keys(bindings).some(id => !sources.some(source => source.camera_id === id))) {
      replaceConfig({ ...config, sources: Object.fromEntries(Object.entries(bindings).filter(([id]) => sources.some(source => source.camera_id === id))) });
    }
  }, [enabled, sourceIds]);
  useEffect(() => {
    if (!enabled) return;
    const abort = new AbortController();
    setIndex(null); setError(false); setPending({});
    fetchCamerasIndex(abort.signal).then(result => { if (!abort.signal.aborted) setIndex(result); })
      .catch(() => { if (!abort.signal.aborted) setError(true); });
    return () => abort.abort();
  }, [enabled]);
  const valid = !enabled || Boolean(index && matchingCategories && sources.length && sources.every(source => {
    const binding = bindings[source.camera_id];
    return binding?.source_id === source.source_id && Boolean(binding.profile_token)
      && binding.signal.coverage_confirmed && pending[source.camera_id] === false;
  }));
  useEffect(() => { validity.current?.(valid); }, [valid]);
  useEffect(() => () => validity.current?.(true), []);
  return <div style={{ display: "grid", gap: 12 }}>
    <label className="field"><span>{t("ext.cameras.automatic.native.mode")}</span><select className="input" value={enabled ? "native" : "continuous"} onChange={event => replaceConfig({ enabled: event.target.value === "native", sources: {}, categories: observationCategories, fallback_to_video: false })}>
      <option value="continuous">{t("ext.cameras.automatic.native.continuous")}</option>
      <option value="native">{t("ext.cameras.automatic.native.qualified")}</option>
    </select></label>
    {enabled && <>
      <label><input type="checkbox" checked={config.fallback_to_video === true} onChange={event => replaceConfig({ ...config, fallback_to_video: event.target.checked })} /> {t("ext.cameras.automatic.native.fallback")}</label>
      {!matchingCategories && <p role="status">{t("ext.cameras.automatic.native.categories_changed")}</p>}
      {error ? <p role="alert">{t("ext.cameras.onvif.error")}</p> : !index ? <p role="status">{t("ext.cameras.onvif.loading")}</p> : sources.map(source => {
        const camera = index.cameras.find(item => item.id === source.camera_id);
        const binding = bindings[source.camera_id];
        const value = matchingCategories && binding?.source_id === source.source_id ? binding.signal : undefined;
        return <fieldset key={`${source.camera_id}/${source.source_id}/${categories}`}><legend>{source.camera_name} · {source.source_name}</legend>
          {camera ? <CameraOnvifRecipeOptimization automaticWindow camera={{ ...camera, enabled: true, control: camera.control || { type: "none" }, sources: camera.sources || [] }} sourceId={source.source_id} preset="people_simple" processingServerId="local" value={value} disabled={false} t={t}
            onPendingChange={next => setPending(previous => previous[source.camera_id] === next ? previous : { ...previous, [source.camera_id]: next })}
            onChange={(signal, profileToken) => {
              const next = matchingCategories ? { ...bindings } : {};
              if (signal && profileToken) next[source.camera_id] = { source_id: source.source_id, profile_token: profileToken, signal };
              else delete next[source.camera_id];
              replaceConfig({ ...config, categories: observationCategories, sources: next });
            }} /> : <p role="status">{t("ext.cameras.onvif.profile_required")}</p>}
        </fieldset>;
      })}
    </>}
  </div>;
}
