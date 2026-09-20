import React, { useId } from "react";
import type { CameraContextsResponse } from "../../../../../util/api";
import { i18n } from "../../../../../util/i18n";
import { PipelinesNumberInput } from "../PipelinesNumberInput";
import "./HumanMotionPanels.css";

type Update = (updater: (config: Record<string, unknown>) => Record<string, unknown>) => void;
type Props = { config: Record<string, unknown>; onUpdateConfig: Update; showAdvanced: boolean };
const prefix = "core.ui.pipelines.panels.human_motion.";

function NumberField({ label, value, minimum, maximum, step = .05, onChange }: {
  label: string; value: number; minimum: number; maximum: number; step?: number; onChange: (value: number) => void;
}): React.ReactElement {
  const id = useId();
  const { t } = i18n.useI18n();
  const invalid = !Number.isFinite(value) || value < minimum || value > maximum;
  return <label className="pipelinesLabel" htmlFor={id}>
    <span>{label}</span>
    <PipelinesNumberInput className="pipelinesInput" id={id} value={value} min={minimum} max={maximum} step={step}
      aria-invalid={invalid} aria-describedby={invalid ? `${id}-error` : undefined} onChange={onChange} />
    {invalid && <span id={`${id}-error`} className="pipelinesInlineError">{t(`${prefix}range`, { minimum, maximum })}</span>}
  </label>;
}

export function PersonGroundConfigCard({ config, onUpdateConfig, showAdvanced, contexts }: Props & {
  contexts: CameraContextsResponse | null;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const mapping = config.mapping && typeof config.mapping === "object" ? config.mapping as Record<string, unknown> : {};
  const set = (name: string, value: unknown) => onUpdateConfig(previous => ({ ...previous, [name]: value }));
  const minimum = Number(config.height_min_meters ?? .8);
  const maximum = Number(config.height_max_meters ?? 2.2);
  return <div className="pipelinesOperatorConfigCard pipelinesHumanMotionCard">
    <p className="pipelinesStepHint">{t(`${prefix}ground_help`)}</p>
    <label className="pipelinesCheckboxRow"><span>{t(`${prefix}enabled`)}</span>
      <input type="checkbox" checked={config.enabled !== false} onChange={event => set("enabled", event.target.checked)} />
    </label>
    <label className="pipelinesLabel"><span>{t(`${prefix}composition`)}</span>
      <select className="pipelinesSelect" value={String(mapping.composition_id ?? "")} onChange={event => onUpdateConfig(previous => ({
        ...previous, mapping: { ...(previous.mapping as Record<string, unknown> ?? {}), composition_id: event.target.value },
      }))}>
        <option value="">{t(`${prefix}composition_from_camera`)}</option>
        {(contexts?.compositions ?? []).map(composition => <option key={composition.id} value={composition.id}>{composition.name}</option>)}
        {mapping.composition_id && !contexts?.compositions.some(item => item.id === mapping.composition_id)
          ? <option value={String(mapping.composition_id)}>{t(`${prefix}saved_composition`, { id: String(mapping.composition_id) })}</option> : null}
      </select>
    </label>
    <p className="pipelinesStepHint">{t(`${prefix}metric_help`)}</p>
    <label className="pipelinesCheckboxRow"><span>{t(`${prefix}complete_feet`)}</span>
      <input type="checkbox" checked={config.complete_hidden_feet === true} onChange={event => set("complete_hidden_feet", event.target.checked)} />
    </label>
    <p className="pipelinesStepHint">{t(`${prefix}completion_help`)}</p>
    <NumberField label={t(`${prefix}height_min`)} value={minimum} minimum={.4} maximum={2.5} onChange={value => set("height_min_meters", value)} />
    <NumberField label={t(`${prefix}height_max`)} value={maximum} minimum={.4} maximum={2.5} onChange={value => set("height_max_meters", value)} />
    {minimum > maximum && <p className="pipelinesInlineError" role="status">{t(`${prefix}height_order`)}</p>}
    <p className="pipelinesStepHint">{t(`${prefix}height_help`)}</p>
    {showAdvanced && <>
      <NumberField label={t(`${prefix}history`)} value={Number(config.maximum_history_seconds ?? .8)} minimum={.05} maximum={3} onChange={value => set("maximum_history_seconds", value)} />
      <NumberField label={t(`${prefix}score`)} value={Number(config.minimum_model_score ?? .65)} minimum={0} maximum={1} onChange={value => set("minimum_model_score", value)} />
      <NumberField label={t(`${prefix}frame_age_ms`)} value={Number(config.maximum_frame_age_ms ?? 750)} minimum={1} maximum={5000} step={50} onChange={value => set("maximum_frame_age_ms", value)} />
    </>}
  </div>;
}

export function GestureConfigCard({ config, onUpdateConfig, showAdvanced }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const set = (name: string, value: unknown) => onUpdateConfig(previous => ({ ...previous, [name]: value }));
  return <div className="pipelinesOperatorConfigCard pipelinesHumanMotionCard">
    <p className="pipelinesStepHint">{t(`${prefix}gesture_help`)}</p>
    <label className="pipelinesCheckboxRow"><span>{t(`${prefix}enabled`)}</span>
      <input type="checkbox" checked={config.enabled !== false} onChange={event => set("enabled", event.target.checked)} />
    </label>
    <label className="pipelinesLabel"><span>{t(`${prefix}output`)}</span>
      <select className="pipelinesSelect" value={String(config.output_mode ?? "annotate")} onChange={event => set("output_mode", event.target.value)}>
        <option value="annotate">{t(`${prefix}annotate`)}</option>
        <option value="events">{t(`${prefix}events`)}</option>
      </select>
    </label>
    <p className="pipelinesStepHint">{t(`${prefix}no_physical_actions`)}</p>
    <NumberField label={t(`${prefix}minimum_duration`)} value={Number(config.minimum_duration_seconds ?? .4)} minimum={.05} maximum={10} onChange={value => set("minimum_duration_seconds", value)} />
    <NumberField label={t(`${prefix}release`)} value={Number(config.release_seconds ?? .3)} minimum={0} maximum={10} onChange={value => set("release_seconds", value)} />
    {showAdvanced && <>
      <NumberField label={t(`${prefix}cooldown`)} value={Number(config.cooldown_seconds ?? 1)} minimum={0} maximum={60} onChange={value => set("cooldown_seconds", value)} />
      <NumberField label={t(`${prefix}gap`)} value={Number(config.maximum_gap_seconds ?? 1)} minimum={.1} maximum={30} onChange={value => set("maximum_gap_seconds", value)} />
      <NumberField label={t(`${prefix}score`)} value={Number(config.minimum_landmark_score ?? .6)} minimum={0} maximum={1} onChange={value => set("minimum_landmark_score", value)} />
      <NumberField label={t(`${prefix}frame_age_seconds`)} value={Number(config.maximum_frame_age_seconds ?? 2)} minimum={.1} maximum={30} onChange={value => set("maximum_frame_age_seconds", value)} />
      <NumberField label={t(`${prefix}wave_window`)} value={Number(config.wave_window_seconds ?? 2)} minimum={.3} maximum={10} onChange={value => set("wave_window_seconds", value)} />
      <NumberField label={t(`${prefix}wave_reversals`)} value={Number(config.wave_minimum_reversals ?? 2)} minimum={2} maximum={6} step={1} onChange={value => set("wave_minimum_reversals", value)} />
    </>}
  </div>;
}

export function PointingConfigCard({ config, onUpdateConfig, showAdvanced }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const set = (name: string, value: unknown) => onUpdateConfig(previous => ({ ...previous, [name]: value }));
  return <div className="pipelinesOperatorConfigCard pipelinesHumanMotionCard">
    <p className="pipelinesStepHint">{t(`${prefix}pointing_help`)}</p>
    <label className="pipelinesCheckboxRow"><span>{t(`${prefix}enabled`)}</span>
      <input type="checkbox" checked={config.enabled !== false} onChange={event => set("enabled", event.target.checked)} />
    </label>
    <label className="pipelinesLabel"><span>{t(`${prefix}pointing_arm`)}</span>
      <select className="pipelinesSelect" value={String(config.arm ?? "auto")} onChange={event => set("arm", event.target.value)}>
        <option value="auto">{t(`${prefix}arm_auto`)}</option>
        <option value="left">{t(`${prefix}arm_left`)}</option>
        <option value="right">{t(`${prefix}arm_right`)}</option>
      </select>
    </label>
    <NumberField label={t(`${prefix}pointing_distance`)} value={Number(config.maximum_distance_meters ?? 20)} minimum={.1} maximum={100} step={1} onChange={value => set("maximum_distance_meters", value)} />
    <p className="pipelinesStepHint">{t(`${prefix}pointing_limits`)}</p>
    {showAdvanced && <>
      <NumberField label={t(`${prefix}pointing_cone`)} value={Number(config.cone_half_angle_degrees ?? 12)} minimum={5} maximum={30} step={1} onChange={value => set("cone_half_angle_degrees", value)} />
      <NumberField label={t(`${prefix}reprojection`)} value={Number(config.maximum_reprojection_error ?? .02)} minimum={.001} maximum={.03} step={.001} onChange={value => set("maximum_reprojection_error", value)} />
      <NumberField label={t(`${prefix}score`)} value={Number(config.minimum_model_score ?? .65)} minimum={0} maximum={1} onChange={value => set("minimum_model_score", value)} />
      <NumberField label={t(`${prefix}frame_age_ms`)} value={Number(config.maximum_frame_age_ms ?? 750)} minimum={1} maximum={5000} step={50} onChange={value => set("maximum_frame_age_ms", value)} />
    </>}
  </div>;
}
