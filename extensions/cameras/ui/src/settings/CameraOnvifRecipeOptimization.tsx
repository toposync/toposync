import React, { useEffect, useId, useState } from "react";
import Select from "react-select";
import type { HostI18n } from "@toposync/plugin-api";
import type { CameraConfig, CameraOnvifCapabilities, CameraOnvifOptimization, CameraPipelinePreset } from "../types";
import { fetchCameraOnvifCapabilities, observeCameraOnvifEvents } from "../api/camerasApi";

type Translate = ReturnType<HostI18n["useI18n"]>["t"];

export function CameraOnvifRecipeOptimization({ camera, sourceId, preset, processingServerId, value, onChange, onPendingChange, t, disabled, automaticWindow = false }: {
  camera: Omit<CameraConfig, "clock_domain">; sourceId: string; preset: CameraPipelinePreset; processingServerId: string;
  onPendingChange?: (pending: boolean) => void;
  value?: CameraOnvifOptimization; onChange: (value?: CameraOnvifOptimization, profileToken?: string) => void; t: Translate; disabled: boolean;
  automaticWindow?: boolean;
}): React.ReactElement {
  const selectId = useId();
  const [error, setError] = useState(false);
  const [gap, setGap] = useState(false);
  const [observationError, setObservationError] = useState("");
  const [catalog, setCatalog] = useState<CameraOnvifCapabilities | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selection, setSelection] = useState("");
  const [selectedSignal, setSelectedSignal] = useState<CameraOnvifCapabilities["observed_states"][number] | null>(null);
  const [holdSeconds, setHoldSeconds] = useState("30");
  const holdDuration = Number(holdSeconds.replace(",", "."));
  const validDuration = Boolean(holdSeconds.trim()) && Number.isFinite(holdDuration) && holdDuration >= 0 && holdDuration <= 300;
  useEffect(() => {
    setEnabled(automaticWindow); setGap(false); setObservationError(""); setSelectedSignal(null);
    setSelection(automaticWindow && value ? JSON.stringify([value.qualified_topic, value.item_name, Object.entries(value.source).sort(), Object.entries(value.key).sort()]) : "");
    setHoldSeconds(String(automaticWindow ? value?.hold_seconds ?? 30 : 30));
    if (!automaticWindow) onChange(undefined);
    onPendingChange?.(automaticWindow);
  }, [camera.id, sourceId, preset, processingServerId, automaticWindow]);
  useEffect(() => {
    if (preset !== "people_simple" || camera.control.type !== "onvif" || processingServerId !== "local") return;
    const controller = new AbortController();
    setLoading(true); setError(false);
    fetchCameraOnvifCapabilities(camera.id, refresh > 0, controller.signal)
      .then(result => { if (!controller.signal.aborted) setCatalog(result); })
      .catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [camera.id, preset, camera.control.type, processingServerId, refresh]);
  useEffect(() => {
    if (!enabled || preset !== "people_simple" || camera.control.type !== "onvif" || processingServerId !== "local") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let sequence = 0;
    async function poll(): Promise<void> {
      try {
        const observation = await observeCameraOnvifEvents(camera.id, sequence, controller.signal);
        if (controller.signal.aborted) return;
        sequence = observation.last_sequence;
        setGap(observation.gap || !observation.available); setObservationError(observation.error || "");
        setCatalog(previous => previous ? { ...previous, observed_states: observation.states } : previous);
      } catch {
        if (controller.signal.aborted) return;
        setGap(true); setObservationError("");
        setCatalog(previous => previous ? { ...previous, observed_states: [] } : previous);
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 1500);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [enabled, camera.id, sourceId, preset, camera.control.type, processingServerId]);
  const source = camera.sources.find(item => item.id === sourceId);
  const profile = catalog?.profiles.find(item => item.token === (catalog.source_profiles?.[sourceId] || source?.origin.profile_token));
  const candidates = ["onvif_profile", "rtsp"].includes(source?.origin.type || "") && profile ? (catalog?.observed_states ?? []).filter(state => {
    const matching = Object.entries({ VideoSourceToken: profile.video_source_token, VideoSourceConfigurationToken: profile.video_source_configuration_token,
      ...(state.topic === "VideoSource/MotionAlarm" ? { Source: profile.video_source_token } : {}),
    })
      .filter(([name]) => name in state.source);
    return state.known && state.available && matching.length > 0 && matching.every(([name, token]) => token && state.source[name] === token)
      && state.qualified_topic.startsWith("{http://www.onvif.org/ver10/topics}")
      && ["RuleEngine/CellMotionDetector/Motion", "VideoSource/MotionAlarm", "RuleEngine/PeopleDetector/People", "RuleEngine/PeopleDetector/Person"].includes(state.topic)
      && catalog?.events.some(event => event.qualified_topic === state.qualified_topic && event.item_name === state.item_name && event.is_boolean && event.is_property);
  }) : [];
  const identity = (item: typeof candidates[number]): string => JSON.stringify([item.qualified_topic, item.item_name, Object.entries(item.source).sort(), Object.entries(item.key).sort()]);
  const currentSelection = candidates.find(item => identity(item) === selection);
  const selected = currentSelection ?? selectedSignal;
  const selectionAvailable = Boolean(currentSelection) && !gap && !loading && !error;
  useEffect(() => {
    if (enabled) onPendingChange?.(!selectionAvailable || !value?.coverage_confirmed || !validDuration);
  }, [enabled, selectionAvailable, value?.coverage_confirmed, validDuration, onPendingChange]);
  if (preset !== "people_simple") return <p className="settingsStatusMuted" style={{ whiteSpace: "normal" }}>{t(
    preset === "audio_anomaly" ? "ext.cameras.onvif.audio" : preset === "doorbell_visitor" ? "ext.cameras.onvif.visitor" : "ext.cameras.onvif.continuity",
  )}</p>;
  if (processingServerId !== "local") return <p className="settingsStatusMuted">{t("ext.cameras.onvif.local")}</p>;
  if (camera.control.type !== "onvif") return <p className="settingsStatusMuted">{t("ext.cameras.onvif.manual")}</p>;
  return <div className="field" style={{ overflowWrap: "anywhere" }}>
    {!automaticWindow && <label className="row"><input type="checkbox" checked={enabled} disabled={disabled || loading || !catalog} onChange={event => {
      setEnabled(event.target.checked); setGap(false); setObservationError(""); setSelection(""); setSelectedSignal(null); onChange(undefined); onPendingChange?.(event.target.checked);
    }} />{t("ext.cameras.onvif.optimize")}</label>}
    {loading ? <p role="status">{t("ext.cameras.onvif.loading")}</p> : null}
    {error ? <p role="alert">{t("ext.cameras.onvif.error")}</p> : null}
    <button className="chipButton" type="button" disabled={disabled || loading} onClick={() => { setRefresh(count => count + 1); onPendingChange?.(enabled); }}>{t("ext.cameras.onvif.refresh")}</button>
    {enabled ? <>
      <p className="settingsDescription">{t(automaticWindow ? "ext.cameras.automatic.native.help" : "ext.cameras.onvif.optimize_help")}</p>
      {automaticWindow && <p>{t("ext.cameras.automatic.native.profile")}: {profile?.token || "—"}</p>}
      <p role="status" className="settingsDescription">{t("ext.cameras.onvif.recipe_observing")}</p>
      {!profile ? <p role="status">{t("ext.cameras.onvif.profile_required")}</p> : null}
      {gap ? <p role="status">{t("ext.cameras.onvif.gap")}{observationError ? ` ${observationError}` : ""}</p> : null}
      {profile && !gap && !candidates.length ? <p role="status">{t(catalog?.observed_states.some(state => state.known && state.available) ? "ext.cameras.onvif.incompatible_observation" : "ext.cameras.onvif.needs_observation")}</p> : null}
      <div className="field"><label className="label" htmlFor={selectId}>{t("ext.cameras.onvif.signal")}</label>
        <Select inputId={selectId} options={candidates} value={selected ?? null} isDisabled={disabled || gap}
          getOptionValue={identity} getOptionLabel={item => `${item.label || item.item_name} · ${item.topic} · ${JSON.stringify(item.source)} · ${JSON.stringify(item.key)}`}
          placeholder="—" noOptionsMessage={() => t("ext.cameras.onvif.no_compatible_events")}
          formatOptionLabel={item => <div style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}><div>{item.label || item.item_name}</div><small>{item.topic} · {JSON.stringify(item.source)} · {JSON.stringify(item.key)}</small></div>}
          styles={{
            control: base => ({ ...base, backgroundColor: "var(--color-surface-solid)", borderColor: "var(--color-border-subtle)" }),
            menu: base => ({ ...base, backgroundColor: "var(--color-surface-solid)", zIndex: 50 }),
            singleValue: base => ({ ...base, color: "var(--color-text-primary)" }),
            input: base => ({ ...base, color: "var(--color-text-primary)" }),
            option: (base, state) => ({ ...base, color: "var(--color-text-primary)", backgroundColor: state.isFocused || state.isSelected ? "var(--color-surface-frost-strong)" : "transparent" }),
          }}
          onChange={item => { setSelection(item ? identity(item) : ""); setSelectedSignal(item); onChange(undefined); onPendingChange?.(true); }} />
      </div>
      <details><summary>{t("ext.cameras.onvif.window_settings")}</summary><label className="field">
        <span>{t("ext.cameras.onvif.hold_seconds")}</span>
        <input type="text" inputMode="decimal" maxLength={10} value={holdSeconds} aria-invalid={!validDuration} disabled={disabled} onChange={event => {
          const draft = event.target.value;
          setHoldSeconds(draft);
          const duration = Number(draft.replace(",", "."));
          if (draft.trim() && Number.isFinite(duration) && duration >= 0 && duration <= 300 && value) onChange({ ...value, hold_seconds: duration, coverage_confirmed: automaticWindow ? false : value.coverage_confirmed }, profile?.token);
        }} />
      </label><p className="settingsDescription">{t(automaticWindow ? "ext.cameras.automatic.native.window" : "ext.cameras.onvif.window_help")}</p></details>
      {selected && selectionAvailable ? <label className="row" style={{ alignItems: "flex-start" }}><input type="checkbox" checked={Boolean(value?.coverage_confirmed)} disabled={disabled || !validDuration} onChange={event => { onPendingChange?.(!event.target.checked); onChange(event.target.checked ? {
        topic: selected.topic, qualified_topic: selected.qualified_topic, item_name: selected.item_name,
        source: selected.source, key: selected.key, coverage_confirmed: true, hold_seconds: holdDuration,
      } : undefined, profile?.token); }} /><span>{t(automaticWindow ? "ext.cameras.automatic.native.qualify" : "ext.cameras.onvif.qualify")}</span></label> : null}
    </> : null}
  </div>;
}
