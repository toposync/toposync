import React, { useEffect, useMemo, useState } from "react";
import type { HostI18n, PipelineOperatorPanel } from "@toposync/plugin-api";

import { DEFAULT_PROFILE_ID } from "../constants";
import { fetchAiSettings, fetchAiSettingsDefaults } from "../api/aiApi";
import type { AiExtensionSettings, AiProfileConfig } from "../types";

type PanelArgs = Parameters<PipelineOperatorPanel["render"]>[0];
type T = HostI18n["t"];

const DEFAULT_SMART_CROP = {
  profile_id: DEFAULT_PROFILE_ID,
  target_description: "",
  padding_ratio: 0.05,
  confidence_threshold: 0.35,
  detection_strategy: "highest_confidence",
  refresh_interval_seconds: 1800,
  refresh_on_ptz_idle: true,
  ptz_idle_debounce_seconds: 2,
  set_stream_frame: true,
  missing_policy: "drop",
};

const DEFAULT_CONDITION_FILTER = {
  profile_id: DEFAULT_PROFILE_ID,
  condition_description: "",
  confidence_threshold: 0.5,
  evaluation_interval_seconds: 5,
  max_concurrency: 1,
  concurrency_policy: "skip",
  reuse_last_decision_seconds: 10,
  failure_policy: "reuse_last",
};

export function createAiSmartCropOperatorPanel(): PipelineOperatorPanel {
  return {
    id: "com.toposync.ai.operator.smart_crop",
    operatorId: "ai.smart_crop",
    render: (args) => <AiSmartCropPanel {...args} />,
  };
}

export function createAiConditionFilterOperatorPanel(): PipelineOperatorPanel {
  return {
    id: "com.toposync.ai.operator.condition_filter",
    operatorId: "ai.condition_filter",
    render: (args) => <AiConditionFilterPanel {...args} />,
  };
}

function AiSmartCropPanel({ i18n, config, showAdvanced, updateConfig }: PanelArgs): React.ReactElement {
  const { t } = i18n.useI18n();
  const settings = useAiSettings();
  const profiles = settings?.profiles ?? [];
  const c = { ...DEFAULT_SMART_CROP, ...config };

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("ext.ai.operator.smart_crop.target", {}, "Objeto ou região")}</span>
        <textarea
          className="pipelinesTextArea"
          rows={3}
          value={asString(c.target_description)}
          placeholder={t("ext.ai.operator.smart_crop.target_placeholder", {}, "ex.: sofá, piscina, caixa de areia")}
          onChange={(event) => updateConfig({ target_description: event.target.value })}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("ext.ai.operator.profile", {}, "Perfil de IA")}</span>
        <ProfileSelect
          t={t}
          value={asString(c.profile_id) || DEFAULT_PROFILE_ID}
          profiles={profiles}
          onChange={(profile_id) => updateConfig({ profile_id })}
        />
      </label>

      <div className="rowWrap" style={{ gap: 12 }}>
        <label className="pipelinesLabel" style={{ flex: "1 1 220px" }}>
          <span>{t("ext.ai.operator.smart_crop.strategy", {}, "Quando encontrar vários")}</span>
          <select
            className="pipelinesSelect"
            value={asString(c.detection_strategy) || DEFAULT_SMART_CROP.detection_strategy}
            onChange={(event) => updateConfig({ detection_strategy: event.target.value })}
          >
            <option value="highest_confidence">{t("ext.ai.operator.smart_crop.strategy.highest", {}, "Maior confiança")}</option>
            <option value="union">{t("ext.ai.operator.smart_crop.strategy.union", {}, "Englobar todos")}</option>
            <option value="first">{t("ext.ai.operator.smart_crop.strategy.first", {}, "Primeiro")}</option>
          </select>
        </label>

        <NumberField
          label={t("ext.ai.operator.confidence", {}, "Confiança mínima")}
          min={0}
          max={1}
          step={0.05}
          value={asNumber(c.confidence_threshold, DEFAULT_SMART_CROP.confidence_threshold)}
          onChange={(confidence_threshold) => updateConfig({ confidence_threshold })}
        />
      </div>

      <div className="rowWrap" style={{ gap: 12 }}>
        <NumberField
          label={t("ext.ai.operator.smart_crop.padding", {}, "Margem")}
          min={0}
          max={1}
          step={0.01}
          value={asNumber(c.padding_ratio, DEFAULT_SMART_CROP.padding_ratio)}
          onChange={(padding_ratio) => updateConfig({ padding_ratio })}
        />
        <RefreshIntervalSelect
          t={t}
          value={asNumber(c.refresh_interval_seconds, DEFAULT_SMART_CROP.refresh_interval_seconds)}
          onChange={(refresh_interval_seconds) => updateConfig({ refresh_interval_seconds })}
        />
      </div>

      <Checkbox
        label={t("ext.ai.operator.smart_crop.ptz", {}, "Atualizar depois de movimento PTZ")}
        checked={asBoolean(c.refresh_on_ptz_idle, DEFAULT_SMART_CROP.refresh_on_ptz_idle)}
        onChange={(refresh_on_ptz_idle) => updateConfig({ refresh_on_ptz_idle })}
      />

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <div className="rowWrap" style={{ gap: 12 }}>
            <label className="pipelinesLabel" style={{ flex: "1 1 220px" }}>
              <span>{t("ext.ai.operator.smart_crop.missing_policy", {}, "Quando não encontrar")}</span>
              <select
                className="pipelinesSelect"
                value={asString(c.missing_policy) || DEFAULT_SMART_CROP.missing_policy}
                onChange={(event) => updateConfig({ missing_policy: event.target.value })}
              >
                <option value="drop">{t("ext.ai.operator.smart_crop.missing.drop", {}, "Drop packet")}</option>
                <option value="reuse_last">{t("ext.ai.operator.smart_crop.missing.reuse", {}, "Reuse last crop")}</option>
                <option value="pass_through">{t("ext.ai.operator.smart_crop.missing.pass", {}, "Pass through without crop")}</option>
              </select>
            </label>
            <NumberField
              label={t("ext.ai.operator.smart_crop.ptz_debounce", {}, "Debounce PTZ")}
              min={0}
              max={60}
              step={0.5}
              value={asNumber(c.ptz_idle_debounce_seconds, DEFAULT_SMART_CROP.ptz_idle_debounce_seconds)}
              onChange={(ptz_idle_debounce_seconds) => updateConfig({ ptz_idle_debounce_seconds })}
            />
          </div>
          <Checkbox
            label={t("ext.ai.operator.smart_crop.use_as_frame", {}, "Usar recorte como frame tratado")}
            checked={asBoolean(c.set_stream_frame, DEFAULT_SMART_CROP.set_stream_frame)}
            onChange={(set_stream_frame) => updateConfig({ set_stream_frame })}
          />
        </>
      ) : null}
    </div>
  );
}

function AiConditionFilterPanel({ i18n, config, showAdvanced, updateConfig }: PanelArgs): React.ReactElement {
  const { t } = i18n.useI18n();
  const settings = useAiSettings();
  const profiles = settings?.profiles ?? [];
  const c = { ...DEFAULT_CONDITION_FILTER, ...config };

  return (
    <div className="pipelinesOperatorConfigCard">
      <label className="pipelinesLabel">
        <span>{t("ext.ai.operator.condition.condition", {}, "Condição")}</span>
        <textarea
          className="pipelinesTextArea"
          rows={3}
          value={asString(c.condition_description)}
          placeholder={t("ext.ai.operator.condition.placeholder", {}, "ex.: alguém sentado no sofá")}
          onChange={(event) => updateConfig({ condition_description: event.target.value })}
        />
      </label>

      <label className="pipelinesLabel">
        <span>{t("ext.ai.operator.profile", {}, "Perfil de IA")}</span>
        <ProfileSelect
          t={t}
          value={asString(c.profile_id) || DEFAULT_PROFILE_ID}
          profiles={profiles}
          onChange={(profile_id) => updateConfig({ profile_id })}
        />
      </label>

      <div className="rowWrap" style={{ gap: 12 }}>
        <NumberField
          label={t("ext.ai.operator.confidence", {}, "Confiança mínima")}
          min={0}
          max={1}
          step={0.05}
          value={asNumber(c.confidence_threshold, DEFAULT_CONDITION_FILTER.confidence_threshold)}
          onChange={(confidence_threshold) => updateConfig({ confidence_threshold })}
        />
        <EvaluationIntervalSelect
          t={t}
          value={asNumber(c.evaluation_interval_seconds, DEFAULT_CONDITION_FILTER.evaluation_interval_seconds)}
          onChange={(evaluation_interval_seconds) => updateConfig({ evaluation_interval_seconds })}
        />
      </div>

      <div className="rowWrap" style={{ gap: 12 }}>
        <NumberField
          label={t("ext.ai.operator.condition.concurrency", {}, "Concorrência")}
          min={1}
          max={32}
          step={1}
          value={asNumber(c.max_concurrency, DEFAULT_CONDITION_FILTER.max_concurrency)}
          onChange={(max_concurrency) => updateConfig({ max_concurrency: Math.round(max_concurrency) })}
        />
        <label className="pipelinesLabel" style={{ flex: "1 1 220px" }}>
          <span>{t("ext.ai.operator.condition.concurrency_policy", {}, "Quando lotado")}</span>
          <select
            className="pipelinesSelect"
            value={asString(c.concurrency_policy) || DEFAULT_CONDITION_FILTER.concurrency_policy}
            onChange={(event) => updateConfig({ concurrency_policy: event.target.value })}
          >
            <option value="skip">{t("ext.ai.operator.condition.concurrency.skip", {}, "Skip frame")}</option>
            <option value="queue">{t("ext.ai.operator.condition.concurrency.queue", {}, "Queue")}</option>
            <option value="fallback">{t("ext.ai.operator.condition.concurrency.fallback", {}, "Use fallback")}</option>
          </select>
        </label>
      </div>

      {showAdvanced ? (
        <>
          <div className="sectionDivider" />
          <div className="rowWrap" style={{ gap: 12 }}>
            <NumberField
              label={t("ext.ai.operator.condition.reuse_last", {}, "Reusar decisão")}
              min={0}
              max={86400}
              step={1}
              value={asNumber(c.reuse_last_decision_seconds, DEFAULT_CONDITION_FILTER.reuse_last_decision_seconds)}
              onChange={(reuse_last_decision_seconds) => updateConfig({ reuse_last_decision_seconds })}
            />
            <label className="pipelinesLabel" style={{ flex: "1 1 220px" }}>
              <span>{t("ext.ai.operator.condition.failure_policy", {}, "Se falhar")}</span>
              <select
                className="pipelinesSelect"
                value={asString(c.failure_policy) || DEFAULT_CONDITION_FILTER.failure_policy}
                onChange={(event) => updateConfig({ failure_policy: event.target.value })}
              >
                <option value="reuse_last">{t("ext.ai.operator.condition.failure.reuse", {}, "Reuse last decision")}</option>
                <option value="drop">{t("ext.ai.operator.condition.failure.drop", {}, "Drop packet")}</option>
                <option value="pass_through">{t("ext.ai.operator.condition.failure.pass", {}, "Pass through")}</option>
              </select>
            </label>
          </div>
        </>
      ) : null}
    </div>
  );
}

function useAiSettings(): AiExtensionSettings | null {
  const [settings, setSettings] = useState<AiExtensionSettings | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetchAiSettings(controller.signal)
      .catch(() => fetchAiSettingsDefaults(controller.signal))
      .then(setSettings)
      .catch(() => setSettings(null));
    return () => controller.abort();
  }, []);

  return settings;
}

function ProfileSelect({
  t,
  value,
  profiles,
  onChange,
}: {
  t: T;
  value: string;
  profiles: AiProfileConfig[];
  onChange: (value: string) => void;
}): React.ReactElement {
  const options = useMemo(() => {
    if (profiles.length) return profiles;
    return [{ id: DEFAULT_PROFILE_ID, name: "Qwen3-VL 30B local", model: "qwen3-vl:30b" }] as AiProfileConfig[];
  }, [profiles]);

  return (
    <select className="pipelinesSelect" value={value} onChange={(event) => onChange(event.target.value)}>
      {options.map((profile) => (
        <option key={profile.id} value={profile.id}>
          {profile.name || profile.model || profile.id}
        </option>
      ))}
    </select>
  );
}

function RefreshIntervalSelect({
  t,
  value,
  onChange,
}: {
  t: T;
  value: number;
  onChange: (value: number) => void;
}): React.ReactElement {
  return (
    <label className="pipelinesLabel" style={{ flex: "1 1 180px" }}>
      <span>{t("ext.ai.operator.smart_crop.refresh", {}, "Atualizar recorte")}</span>
      <select className="pipelinesSelect" value={String(value)} onChange={(event) => onChange(Number(event.target.value))}>
        <option value="0">{t("ext.ai.operator.interval.every_packet", {}, "A cada imagem")}</option>
        <option value="300">{t("ext.ai.operator.interval.5m", {}, "A cada 5 min")}</option>
        <option value="900">{t("ext.ai.operator.interval.15m", {}, "A cada 15 min")}</option>
        <option value="1800">{t("ext.ai.operator.interval.30m", {}, "A cada 30 min")}</option>
        <option value="3600">{t("ext.ai.operator.interval.1h", {}, "A cada 1 h")}</option>
      </select>
    </label>
  );
}

function EvaluationIntervalSelect({
  t,
  value,
  onChange,
}: {
  t: T;
  value: number;
  onChange: (value: number) => void;
}): React.ReactElement {
  return (
    <label className="pipelinesLabel" style={{ flex: "1 1 180px" }}>
      <span>{t("ext.ai.operator.condition.interval", {}, "Avaliar no máximo")}</span>
      <select className="pipelinesSelect" value={String(value)} onChange={(event) => onChange(Number(event.target.value))}>
        <option value="0">{t("ext.ai.operator.interval.every_packet", {}, "A cada imagem")}</option>
        <option value="1">{t("ext.ai.operator.interval.1s", {}, "A cada 1 s")}</option>
        <option value="5">{t("ext.ai.operator.interval.5s", {}, "A cada 5 s")}</option>
        <option value="10">{t("ext.ai.operator.interval.10s", {}, "A cada 10 s")}</option>
        <option value="30">{t("ext.ai.operator.interval.30s", {}, "A cada 30 s")}</option>
        <option value="60">{t("ext.ai.operator.interval.1m", {}, "A cada 1 min")}</option>
      </select>
    </label>
  );
}

function NumberField({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
}): React.ReactElement {
  return (
    <label className="pipelinesLabel" style={{ flex: "1 1 150px" }}>
      <span>{label}</span>
      <input
        className="pipelinesInput"
        type="number"
        min={min}
        max={max}
        step={step}
        value={Number.isFinite(value) ? value : min}
        onChange={(event) => onChange(clamp(Number(event.target.value), min, max))}
      />
    </label>
  );
}

function Checkbox({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}): React.ReactElement {
  return (
    <label className="pipelinesLabel pipelinesCheckboxLabel">
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function asBoolean(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  return fallback;
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}
