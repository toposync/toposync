import React, { useEffect, useState } from "react";
import { requestJson, type HostI18n } from "@toposync/plugin-api";
import { fetchHomeAssistantRegistry, fetchHomeAssistantServers, fetchHomeAssistantStates } from "../api/homeAssistantApi";
import type { HomeAssistantRegistryEntity, HomeAssistantServerPublic } from "../types";

export function SpeechPanel({ i18n, config, updateConfig, showAdvanced }: {
  i18n: HostI18n; config: Record<string, unknown>; updateConfig: (patch: Record<string, unknown>) => void; showAdvanced: boolean;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const [servers, setServers] = useState<HomeAssistantServerPublic[]>([]);
  const [entities, setEntities] = useState<HomeAssistantRegistryEntity[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [supported, setSupported] = useState(false);
  const [serversLoaded, setServersLoaded] = useState(false);
  const serverId = String(config.server_id ?? "");
  useEffect(() => {
    let active = true;
    fetchHomeAssistantServers().then((value) => { if (active) setServers(value); }).catch(() => { if (active) setError(t("ext.home_assistant.speech.discovery_failed")); })
      .finally(() => { if (active) setServersLoaded(true); });
    return () => { active = false; };
  }, [t]);
  useEffect(() => {
    let active = true;
    setEntities([]); setSupported(false); setError("");
    if (!serverId) return;
    setLoading(true);
    Promise.all([
      fetchHomeAssistantRegistry(serverId),
      requestJson<{ service: string }[]>(`/api/home_assistant/${encodeURIComponent(serverId)}/services?domain=tts`),
    ]).then(async ([registry, services]) => {
      const candidates = registry.entities.filter((entity) => entity.entity_id.startsWith("tts.") || entity.entity_id.startsWith("media_player."));
      const states = await fetchHomeAssistantStates(serverId, candidates.map((entity) => entity.entity_id));
      if (!active) return;
      setEntities(candidates.filter((entity) => {
        const state = states[entity.entity_id];
        return state && state.state !== "unavailable" &&
          (entity.entity_id.startsWith("tts.") || (state.state !== "unknown" && (Number(state.attributes?.supported_features ?? 0) & 512) !== 0));
      }));
      setSupported(services.some((service) => service.service === "speak"));
    }).catch(() => { if (active) setError(t("ext.home_assistant.speech.discovery_failed")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [serverId, t]);
  const ready = supported && ["tts_entity_id", "media_player_entity_id"].every((key) => entities.some((entity) => entity.entity_id === config[key]));
  return <div>
    <p>{t("ext.home_assistant.speech.hint")}</p>
    {serversLoaded && !servers.length && !error ? <p role="status">{t("ext.home_assistant.speech.no_servers")}</p> : null}
    <label className="field"><span className="label">{t("ext.home_assistant.speech.server")}</span>
      <select className="input" value={serverId} onChange={(event) => updateConfig({ server_id: event.target.value, tts_entity_id: "", media_player_entity_id: "", enabled: false })}>
        <option value="">{t("ext.home_assistant.speech.choose")}</option>
        {servers.map((server) => <option key={server.id} value={server.id}>{server.name}</option>)}
      </select>
    </label>
    {loading ? <p role="status">{t("ext.home_assistant.speech.loading")}</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {serverId && !loading && !error && !supported ? <p role="status">{t("ext.home_assistant.speech.unavailable")}</p> : null}
    {([ ["tts_entity_id", "tts."], ["media_player_entity_id", "media_player."] ] as const).map(([key, prefix]) => <label className="field" key={key}>
      <span className="label">{t(`ext.home_assistant.speech.${key}`)}</span>
      <select className="input" disabled={!supported || loading} value={String(config[key] ?? "")} onChange={(event) => updateConfig({ [key]: event.target.value, enabled: false })}>
        <option value="">{t("ext.home_assistant.speech.choose")}</option>
        {entities.filter((entity) => entity.entity_id.startsWith(prefix)).map((entity) => <option key={entity.entity_id} value={entity.entity_id}>{entity.name || entity.entity_id}</option>)}
      </select>
    </label>)}
    <label className="field"><span className="label">{t("ext.home_assistant.speech.message")}</span>
      <textarea className="input" maxLength={1000} value={String(config.message ?? "")} placeholder={t("ext.home_assistant.speech.message_default")} onChange={(event) => updateConfig({ message: event.target.value })} />
    </label>
    <label><input type="checkbox" disabled={!ready} checked={config.enabled === true} onChange={(event) => updateConfig({ enabled: event.target.checked })} /> {t("ext.home_assistant.speech.enable")}</label>
    <p role="status">{t("ext.home_assistant.speech.unconfirmed")}</p>
    {showAdvanced ? <>
      <label className="field"><span className="label">{t("ext.home_assistant.speech.expiry")}</span>
        <input className="input" type="number" min={1} max={120} value={Number(config.max_age_seconds ?? 30)} onChange={(event) => { const value = Number(event.target.value); if (value >= 1 && value <= 120) updateConfig({ max_age_seconds: value }); }} />
      </label>
      <label><input type="checkbox" checked={config.interrupt_on_close === true} onChange={(event) => updateConfig({ interrupt_on_close: event.target.checked })} /> {t("ext.home_assistant.speech.interrupt")}</label>
      <p>{t("ext.home_assistant.speech.feedback")}</p>
    </> : null}
  </div>;
}
