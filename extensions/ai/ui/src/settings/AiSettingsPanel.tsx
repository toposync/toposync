import React, { useEffect, useMemo, useState } from "react";

import type { SettingsPanel, ToposyncHost } from "@toposync/plugin-api";

import {
  DEFAULT_OLLAMA_HOST,
  DEFAULT_OLLAMA_MODEL,
  DEFAULT_OLLAMA_PROVIDER_ID,
  DEFAULT_PROFILE_ID,
  AI_EXTENSION_ID,
} from "../constants";
import {
  fetchAiCatalog,
  fetchAiSettingsDefaults,
  fetchAiUsage,
  fetchOllamaModels,
  pullOllamaModel,
  testAiProvider,
} from "../api/aiApi";
import type {
  AiExtensionSettings,
  AiLimitSettings,
  AiModelCatalogEntry,
  AiProfileConfig,
  AiProviderConfig,
  AiProviderKind,
  OllamaModel,
  ProviderTestResponse,
  UsageSnapshot,
} from "../types";

type Props = {
  i18n: ToposyncHost["i18n"];
  settings: Record<string, unknown>;
  updateSettings: (patch: Record<string, unknown>) => void;
};

type Section = "ollama" | "profiles" | "providers" | "limits";

const PROVIDER_KINDS: AiProviderKind[] = ["openai", "anthropic", "google", "litellm"];

const HARD_DEFAULTS: AiExtensionSettings = {
  default_profile_id: DEFAULT_PROFILE_ID,
  providers: [
    {
      id: DEFAULT_OLLAMA_PROVIDER_ID,
      name: "Ollama local",
      kind: "ollama",
      host: DEFAULT_OLLAMA_HOST,
      api_key: "",
      enabled: true,
      local: true,
      allow_image_upload: false,
    },
  ],
  profiles: [
    {
      id: DEFAULT_PROFILE_ID,
      name: "Qwen3-VL 30B local",
      provider_id: DEFAULT_OLLAMA_PROVIDER_ID,
      model: DEFAULT_OLLAMA_MODEL,
      fallback_profile_ids: ["local_qwen3_vl_lighter"],
      capabilities: ["vision", "structured_json", "bbox", "boolean_filter"],
      timeout_seconds: 60,
      max_image_side_px: 1280,
      jpeg_quality: 85,
      temperature: 0,
      enabled: true,
    },
    {
      id: "local_qwen3_vl_lighter",
      name: "Qwen3-VL 8B local",
      provider_id: DEFAULT_OLLAMA_PROVIDER_ID,
      model: "qwen3-vl:8b",
      fallback_profile_ids: [],
      capabilities: ["vision", "structured_json", "bbox", "boolean_filter"],
      timeout_seconds: 60,
      max_image_side_px: 1280,
      jpeg_quality: 85,
      temperature: 0,
      enabled: true,
    },
  ],
  limits: {
    max_concurrency: 1,
    requests_per_minute: 20,
    requests_per_hour: 300,
    requests_per_day: 2000,
    requests_per_month: null,
  },
  model_catalog_version: "builtin-2026-05-02",
};

export function createAiSettingsPanel(): SettingsPanel {
  return {
    id: AI_EXTENSION_ID,
    icon: "brain",
    name: { key: "ext.ai.settings.name", fallback: "AI" },
    description: { key: "ext.ai.settings.desc" },
    render: ({ i18n, settings, updateSettings }) => (
      <AiSettingsPanelContent i18n={i18n} settings={settings} updateSettings={updateSettings} />
    ),
  };
}

function AiSettingsPanelContent({ i18n, settings, updateSettings }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [backendDefaults, setBackendDefaults] = useState<AiExtensionSettings | null>(null);
  const [catalog, setCatalog] = useState<AiModelCatalogEntry[]>([]);
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [ollamaModels, setOllamaModels] = useState<OllamaModel[]>([]);
  const [ollamaLoading, setOllamaLoading] = useState(false);
  const [ollamaError, setOllamaError] = useState<string | null>(null);
  const [providerTest, setProviderTest] = useState<Record<string, ProviderTestResponse | { error: string }>>({});
  const [busyProviderId, setBusyProviderId] = useState<string | null>(null);
  const [pullingModel, setPullingModel] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<Section>("ollama");
  const [activeProfileId, setActiveProfileId] = useState<string>(DEFAULT_PROFILE_ID);
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});

  useEffect(() => {
    const controller = new AbortController();
    void fetchAiSettingsDefaults(controller.signal)
      .then(setBackendDefaults)
      .catch(() => setBackendDefaults(null));
    void fetchAiCatalog(controller.signal)
      .then((response) => setCatalog(response.models ?? []))
      .catch(() => setCatalog([]));
    void fetchAiUsage(controller.signal)
      .then(setUsage)
      .catch(() => setUsage(null));
    return () => controller.abort();
  }, []);

  const normalized = useMemo(() => normalizeSettings(settings, backendDefaults), [settings, backendDefaults]);
  const providers = normalized.providers;
  const profiles = normalized.profiles;
  const ollamaProvider = providers.find((provider) => provider.kind === "ollama") ?? HARD_DEFAULTS.providers[0];
  const activeProfile = profiles.find((profile) => profile.id === activeProfileId) ?? profiles[0];
  const installedModelRefs = useMemo(() => {
    const refs = new Set<string>();
    for (const model of ollamaModels) {
      for (const value of [model.name, model.model]) {
        const ref = normalizeModelRef(value);
        if (ref) refs.add(ref);
      }
    }
    return refs;
  }, [ollamaModels]);

  useEffect(() => {
    if (activeProfileId && profiles.some((profile) => profile.id === activeProfileId)) return;
    setActiveProfileId(profiles[0]?.id ?? DEFAULT_PROFILE_ID);
  }, [activeProfileId, profiles]);

  useEffect(() => {
    void refreshOllamaModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ollamaProvider.host]);

  function patch(next: Partial<AiExtensionSettings>): void {
    updateSettings(next as unknown as Record<string, unknown>);
  }

  function updateProvider(providerId: string, patchValue: Partial<AiProviderConfig>): void {
    patch({
      providers: providers.map((provider) =>
        provider.id === providerId ? normalizeProvider({ ...provider, ...patchValue }) : provider,
      ),
    });
  }

  function updateProfile(profileId: string, patchValue: Partial<AiProfileConfig>): void {
    patch({
      profiles: profiles.map((profile) =>
        profile.id === profileId ? normalizeProfile({ ...profile, ...patchValue }) : profile,
      ),
    });
  }

  function updateLimits(patchValue: Partial<AiLimitSettings>): void {
    patch({ limits: normalizeLimits({ ...normalized.limits, ...patchValue }) });
  }

  async function refreshOllamaModels(): Promise<void> {
    setOllamaLoading(true);
    setOllamaError(null);
    try {
      const response = await fetchOllamaModels(ollamaProvider.host);
      setOllamaModels(response.models ?? []);
    } catch (error) {
      setOllamaModels([]);
      setOllamaError(error instanceof Error ? error.message : String(error));
    }
    setOllamaLoading(false);
  }

  async function testProvider(provider: AiProviderConfig, profile?: AiProfileConfig): Promise<void> {
    setBusyProviderId(provider.id);
    try {
      const response = await testAiProvider({
        provider,
        profile_id: profile?.id,
        model: profile?.model,
      });
      setProviderTest((prev) => ({ ...prev, [provider.id]: response }));
      if (provider.kind === "ollama" && Array.isArray(response.models)) {
        setOllamaModels(response.models);
      }
    } catch (error) {
      setProviderTest((prev) => ({
        ...prev,
        [provider.id]: { error: error instanceof Error ? error.message : String(error) },
      }));
    }
    setBusyProviderId(null);
  }

  async function pullRecommendedModel(model: string): Promise<void> {
    setPullingModel(model);
    try {
      await pullOllamaModel(model, ollamaProvider.host);
      await refreshOllamaModels();
    } catch (error) {
      setOllamaError(error instanceof Error ? error.message : String(error));
    }
    setPullingModel(null);
  }

  function addCloudProvider(kind: AiProviderKind): void {
    const id = createId(kind);
    const provider = normalizeProvider({
      id,
      name: providerKindLabel(kind),
      kind,
      host: "",
      api_key: "",
      enabled: true,
      local: false,
      allow_image_upload: false,
    });
    const profile = normalizeProfile({
      id: createId(`${kind}_profile`),
      name: t("ext.ai.settings.cloud_profile_name", { provider: providerKindLabel(kind) }, `${providerKindLabel(kind)} vision`),
      provider_id: id,
      model: defaultCloudModel(kind),
      fallback_profile_ids: [],
      capabilities: ["vision", "structured_json", "bbox", "boolean_filter"],
      timeout_seconds: 60,
      max_image_side_px: 1280,
      jpeg_quality: 85,
      temperature: 0,
      enabled: true,
    });
    patch({ providers: [...providers, provider], profiles: [...profiles, profile] });
    setActiveProfileId(profile.id);
    setActiveSection("providers");
  }

  function deleteProvider(providerId: string): void {
    const remainingProviders = providers.filter((provider) => provider.id !== providerId);
    const remainingProfiles = profiles.filter((profile) => profile.provider_id !== providerId);
    const defaultProfileId =
      normalized.default_profile_id && remainingProfiles.some((profile) => profile.id === normalized.default_profile_id)
        ? normalized.default_profile_id
        : remainingProfiles[0]?.id ?? DEFAULT_PROFILE_ID;
    patch({ providers: remainingProviders, profiles: remainingProfiles, default_profile_id: defaultProfileId });
  }

  function addProfile(): void {
    const profile = normalizeProfile({
      ...HARD_DEFAULTS.profiles[0],
      id: createId("profile"),
      name: t("ext.ai.settings.new_profile_name", {}, "New AI profile"),
      provider_id: ollamaProvider.id,
      model: DEFAULT_OLLAMA_MODEL,
      fallback_profile_ids: [],
    });
    patch({ profiles: [...profiles, profile] });
    setActiveProfileId(profile.id);
    setActiveSection("profiles");
  }

  function deleteProfile(profileId: string): void {
    const remainingProfiles = profiles
      .filter((profile) => profile.id !== profileId)
      .map((profile) => ({
        ...profile,
        fallback_profile_ids: profile.fallback_profile_ids.filter((id) => id !== profileId),
      }));
    const defaultProfileId =
      normalized.default_profile_id === profileId
        ? remainingProfiles[0]?.id ?? DEFAULT_PROFILE_ID
        : normalized.default_profile_id;
    patch({ profiles: remainingProfiles, default_profile_id: defaultProfileId });
    setActiveProfileId(remainingProfiles[0]?.id ?? DEFAULT_PROFILE_ID);
  }

  const cloudProviders = providers.filter((provider) => provider.kind !== "ollama");
  const qwenInstalled = hasInstalledModel(installedModelRefs, DEFAULT_OLLAMA_MODEL);

  return (
    <div>
      <div className="settingsDetailHeader" style={{ alignItems: "flex-start", gap: 16 }}>
        <div>
          <div className="modalSectionTitle">{t("ext.ai.settings.title", {}, "IA")}</div>
          <div className="cardMeta">
            {qwenInstalled ? t("ext.ai.settings.installed", {}, "Instalado") : t("ext.ai.settings.not_installed", {}, "Não instalado")} ·{" "}
            {DEFAULT_OLLAMA_MODEL}
          </div>
        </div>
        <div className="settingsTabBar" style={{ justifyContent: "flex-end" }}>
          {(["ollama", "profiles", "providers", "limits"] as Section[]).map((section) => (
            <button
              key={section}
              type="button"
              className={["settingsTab", activeSection === section ? "isSelected" : ""].filter(Boolean).join(" ")}
              onClick={() => setActiveSection(section)}
            >
              {sectionLabel(section, t)}
            </button>
          ))}
        </div>
      </div>

      <div className="sectionDivider" />

      {activeSection === "ollama" ? (
        <OllamaSection
          t={t}
          provider={ollamaProvider}
          models={ollamaModels}
          catalog={catalog}
          loading={ollamaLoading}
          error={ollamaError}
          installedModelRefs={installedModelRefs}
          pullingModel={pullingModel}
          onProviderChange={(patchValue) => updateProvider(ollamaProvider.id, patchValue)}
          onRefresh={() => void refreshOllamaModels()}
          onTest={() => void testProvider(ollamaProvider, activeProfile)}
          onPull={(model) => void pullRecommendedModel(model)}
        />
      ) : null}

      {activeSection === "profiles" ? (
        <ProfilesSection
          t={t}
          settings={normalized}
          profiles={profiles}
          providers={providers}
          activeProfile={activeProfile}
          usage={usage}
          onDefaultProfileChange={(profileId) => patch({ default_profile_id: profileId })}
          onSelectProfile={setActiveProfileId}
          onProfileChange={(patchValue) => activeProfile && updateProfile(activeProfile.id, patchValue)}
          onAddProfile={addProfile}
          onDeleteProfile={() => activeProfile && deleteProfile(activeProfile.id)}
        />
      ) : null}

      {activeSection === "providers" ? (
        <ProvidersSection
          t={t}
          providers={cloudProviders}
          showKeys={showKeys}
          busyProviderId={busyProviderId}
          providerTest={providerTest}
          onAdd={addCloudProvider}
          onDelete={deleteProvider}
          onProviderChange={updateProvider}
          onToggleKey={(providerId) => setShowKeys((prev) => ({ ...prev, [providerId]: !prev[providerId] }))}
          onTest={(provider) => void testProvider(provider, profiles.find((profile) => profile.provider_id === provider.id))}
        />
      ) : null}

      {activeSection === "limits" ? (
        <LimitsSection t={t} limits={normalized.limits} usage={usage} onChange={updateLimits} />
      ) : null}
    </div>
  );
}

function OllamaSection({
  t,
  provider,
  models,
  catalog,
  loading,
  error,
  installedModelRefs,
  pullingModel,
  onProviderChange,
  onRefresh,
  onTest,
  onPull,
}: {
  t: ToposyncHost["i18n"]["t"];
  provider: AiProviderConfig;
  models: OllamaModel[];
  catalog: AiModelCatalogEntry[];
  loading: boolean;
  error: string | null;
  installedModelRefs: Set<string>;
  pullingModel: string | null;
  onProviderChange: (patch: Partial<AiProviderConfig>) => void;
  onRefresh: () => void;
  onTest: () => void;
  onPull: (model: string) => void;
}): React.ReactElement {
  const recommendations = catalog.filter((item) => item.provider === "ollama");

  return (
    <div>
      <div className="card">
        <div className="cardBody">
          <div className="field">
            <div className="label">{t("ext.ai.settings.host", {}, "Host")}</div>
            <input
              className="input"
              value={provider.host}
              onChange={(event) => onProviderChange({ host: event.target.value.slice(0, 256) })}
            />
          </div>
          <div className="rowWrap" style={{ gap: 8 }}>
            <button className="chipButton" type="button" onClick={onTest}>
              <i className="fa-solid fa-plug" aria-hidden="true" /> {t("ext.ai.settings.test", {}, "Testar")}
            </button>
            <button className="chipButton" type="button" onClick={onRefresh} disabled={loading}>
              <i className="fa-solid fa-rotate-right" aria-hidden="true" /> {t("ext.ai.settings.refresh", {}, "Atualizar")}
            </button>
          </div>
          {error ? <div className="errorText" style={{ marginTop: 10 }}>{error}</div> : null}
          <div className="settingsStatusMuted" style={{ marginTop: 10 }}>
            {t("ext.ai.settings.local_privacy", {}, "Perfil local. As imagens ficam na rede local.")}
          </div>
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="modalSectionTitle" style={{ marginBottom: 10 }}>
        {t("ext.ai.settings.catalog", {}, "Modelos recomendados")}
      </div>
      <div className="settingsList">
        {recommendations.map((entry) => {
          const installed = hasInstalledModel(installedModelRefs, entry.model, entry.name);
          const pulling = pullingModel === entry.model;
          return (
            <div key={entry.id} className="choiceItem">
              <div className="settingsListItemRow">
                <div className="settingsListItemMain">
                  <div className="settingsListItemTitle">{entry.name || entry.model}</div>
                  <div className="settingsListItemMeta">
                    {entry.model} · {entry.estimated_size || "-"} ·{" "}
                    {installed ? t("ext.ai.settings.installed", {}, "Instalado") : t("ext.ai.settings.not_installed", {}, "Não instalado")}
                  </div>
                </div>
                <button
                  className="chipButton"
                  type="button"
                  onClick={() => onPull(entry.model)}
                  disabled={installed || pulling}
                >
                  <i className={installed ? "fa-solid fa-check" : "fa-solid fa-download"} aria-hidden="true" />{" "}
                  {installed
                    ? t("ext.ai.settings.installed", {}, "Instalado")
                    : pulling
                      ? t("ext.ai.settings.pulling", {}, "Baixando...")
                      : t("ext.ai.settings.pull", {}, "Baixar")}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="sectionDivider" />

      <div className="modalSectionTitle" style={{ marginBottom: 10 }}>
        {t("ext.ai.settings.model_list", {}, "Modelos instalados")}
      </div>
      {loading ? <div className="settingsStatusMuted">{t("ext.ai.settings.loading", {}, "Carregando...")}</div> : null}
      <div className="settingsList">
        {models.map((model) => {
          const name = String(model.name || model.model || "").trim();
          if (!name) return null;
          return (
            <div key={name} className="choiceItem">
              <div className="settingsListItemTitle">{name}</div>
              <div className="settingsListItemMeta">
                {model.details?.parameter_size || "-"} · {model.details?.quantization_level || "-"} · {formatBytes(model.size)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ProfilesSection({
  t,
  settings,
  profiles,
  providers,
  activeProfile,
  usage,
  onDefaultProfileChange,
  onSelectProfile,
  onProfileChange,
  onAddProfile,
  onDeleteProfile,
}: {
  t: ToposyncHost["i18n"]["t"];
  settings: AiExtensionSettings;
  profiles: AiProfileConfig[];
  providers: AiProviderConfig[];
  activeProfile?: AiProfileConfig;
  usage: UsageSnapshot | null;
  onDefaultProfileChange: (profileId: string) => void;
  onSelectProfile: (profileId: string) => void;
  onProfileChange: (patch: Partial<AiProfileConfig>) => void;
  onAddProfile: () => void;
  onDeleteProfile: () => void;
}): React.ReactElement {
  return (
    <div className="settingsSplit">
      <div className="settingsSplitSidebar">
        <div className="settingsSplitToolbar">
          <select
            className="input"
            value={settings.default_profile_id}
            onChange={(event) => onDefaultProfileChange(event.target.value)}
          >
            {profiles.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profileLabel(profile)}
              </option>
            ))}
          </select>
          <button className="iconButton iconButtonPrimary" type="button" onClick={onAddProfile}>
            <i className="fa-solid fa-plus" aria-hidden="true" />
          </button>
        </div>
        <div className="settingsList" style={{ marginTop: 10 }}>
          {profiles.map((profile) => (
            <button
              key={profile.id}
              type="button"
              className={["choiceItem", profile.id === activeProfile?.id ? "isSelected" : ""].filter(Boolean).join(" ")}
              onClick={() => onSelectProfile(profile.id)}
            >
              <div className="settingsListItemTitle">{profileLabel(profile)}</div>
              <div className="settingsListItemMeta">{profile.model || "-"}</div>
            </button>
          ))}
        </div>
      </div>

      <div className="settingsSplitMain">
        {activeProfile ? (
          <div className="settingsDetail">
            <div className="settingsDetailHeader">
              <div>
                <div className="modalSectionTitle">{profileLabel(activeProfile)}</div>
                <div className="cardMeta">
                  {providerLabel(providers.find((provider) => provider.id === activeProfile.provider_id))}
                </div>
              </div>
              <button className="iconButton iconButtonDanger" type="button" onClick={onDeleteProfile}>
                <i className="fa-solid fa-trash" aria-hidden="true" />
              </button>
            </div>
            <div className="sectionDivider" />
            <div className="card">
              <div className="cardBody">
                <div className="field">
                  <div className="label">{t("ext.ai.settings.name_field", {}, "Nome")}</div>
                  <input className="input" value={activeProfile.name} onChange={(e) => onProfileChange({ name: e.target.value.slice(0, 80) })} />
                </div>
                <div className="field">
                  <div className="label">{t("ext.ai.settings.provider", {}, "Provedor")}</div>
                  <select className="input" value={activeProfile.provider_id} onChange={(e) => onProfileChange({ provider_id: e.target.value })}>
                    {providers.map((provider) => (
                      <option key={provider.id} value={provider.id}>
                        {providerLabel(provider)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <div className="label">{t("ext.ai.settings.model", {}, "Modelo")}</div>
                  <input className="input" value={activeProfile.model} onChange={(e) => onProfileChange({ model: e.target.value.slice(0, 160) })} />
                </div>
                <Checkbox
                  label={t("ext.ai.settings.enabled", {}, "Ativo")}
                  checked={activeProfile.enabled}
                  onChange={(enabled) => onProfileChange({ enabled })}
                />
              </div>
            </div>

            <div className="sectionDivider" />

            <div className="card">
              <div className="cardBody">
                <div className="modalSectionTitle" style={{ marginBottom: 10 }}>
                  {t("ext.ai.settings.fallback", {}, "Fallback")}
                </div>
                <div className="settingsList">
                  {profiles
                    .filter((profile) => profile.id !== activeProfile.id)
                    .map((profile) => (
                      <label key={profile.id} className="choiceItem">
                        <div className="settingsListItemRow">
                          <input
                            type="checkbox"
                            checked={activeProfile.fallback_profile_ids.includes(profile.id)}
                            onChange={(event) => {
                              const selected = new Set(activeProfile.fallback_profile_ids);
                              if (event.target.checked) selected.add(profile.id);
                              else selected.delete(profile.id);
                              onProfileChange({ fallback_profile_ids: Array.from(selected) });
                            }}
                          />
                          <div className="settingsListItemMain">
                            <div className="settingsListItemTitle">{profileLabel(profile)}</div>
                            <div className="settingsListItemMeta">{profile.model}</div>
                          </div>
                        </div>
                      </label>
                    ))}
                </div>
              </div>
            </div>

            <div className="sectionDivider" />

            <div className="card">
              <div className="cardBody">
                <div className="rowWrap" style={{ gap: 12 }}>
                  <NumberField label={t("ext.ai.settings.timeout", {}, "Timeout")} value={activeProfile.timeout_seconds} min={1} max={600} onChange={(value) => onProfileChange({ timeout_seconds: value })} />
                  <NumberField label={t("ext.ai.settings.image_side", {}, "Lado da imagem")} value={activeProfile.max_image_side_px} min={128} max={8192} onChange={(value) => onProfileChange({ max_image_side_px: value })} />
                  <NumberField label={t("ext.ai.settings.jpeg_quality", {}, "Qualidade JPEG")} value={activeProfile.jpeg_quality} min={30} max={100} onChange={(value) => onProfileChange({ jpeg_quality: value })} />
                  <NumberField label={t("ext.ai.settings.temperature", {}, "Temperatura")} value={activeProfile.temperature} min={0} max={2} step={0.1} onChange={(value) => onProfileChange({ temperature: value })} />
                </div>
              </div>
            </div>

            {usage?.profiles?.[activeProfile.id] ? (
              <>
                <div className="sectionDivider" />
                <div className="card">
                  <div className="cardBody">
                    <div className="modalSectionTitle" style={{ marginBottom: 10 }}>
                      {t("ext.ai.settings.usage", {}, "Uso")}
                    </div>
                    <div className="rowWrap" style={{ gap: 12 }}>
                      {Object.entries(usage.profiles[activeProfile.id]).map(([period, count]) => (
                        <div key={period} className="cardMeta">
                          {period}: {count}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ProvidersSection({
  t,
  providers,
  showKeys,
  busyProviderId,
  providerTest,
  onAdd,
  onDelete,
  onProviderChange,
  onToggleKey,
  onTest,
}: {
  t: ToposyncHost["i18n"]["t"];
  providers: AiProviderConfig[];
  showKeys: Record<string, boolean>;
  busyProviderId: string | null;
  providerTest: Record<string, ProviderTestResponse | { error: string }>;
  onAdd: (kind: AiProviderKind) => void;
  onDelete: (providerId: string) => void;
  onProviderChange: (providerId: string, patch: Partial<AiProviderConfig>) => void;
  onToggleKey: (providerId: string) => void;
  onTest: (provider: AiProviderConfig) => void;
}): React.ReactElement {
  return (
    <div>
      <div className="rowWrap" style={{ gap: 8, marginBottom: 12 }}>
        {PROVIDER_KINDS.map((kind) => (
          <button key={kind} className="chipButton" type="button" onClick={() => onAdd(kind)}>
            <i className="fa-solid fa-plus" aria-hidden="true" /> {providerKindLabel(kind)}
          </button>
        ))}
      </div>
      {providers.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.ai.settings.empty_cloud", {}, "Nenhum provedor cloud configurado.")}</div>
        </div>
      ) : null}
      <div className="settingsList">
        {providers.map((provider) => {
          const test = providerTest[provider.id];
          return (
            <div key={provider.id} className="choiceItem">
              <div className="settingsDetailHeader" style={{ marginBottom: 12 }}>
                <div>
                  <div className="settingsListItemTitle">{providerLabel(provider)}</div>
                  <div className="settingsListItemMeta">{provider.kind}</div>
                </div>
                <div className="rowWrap" style={{ gap: 8 }}>
                  <button className="chipButton" type="button" onClick={() => onTest(provider)} disabled={busyProviderId === provider.id}>
                    <i className="fa-solid fa-plug" aria-hidden="true" /> {t("ext.ai.settings.test", {}, "Testar")}
                  </button>
                  <button className="iconButton iconButtonDanger" type="button" onClick={() => onDelete(provider.id)}>
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                  </button>
                </div>
              </div>
              <div className="field">
                <div className="label">{t("ext.ai.settings.name_field", {}, "Nome")}</div>
                <input className="input" value={provider.name} onChange={(e) => onProviderChange(provider.id, { name: e.target.value.slice(0, 80) })} />
              </div>
              <div className="field">
                <div className="label">{t("ext.ai.settings.api_key", {}, "Chave de API")}</div>
                <div className="row" style={{ gap: 8 }}>
                  <input
                    className="input"
                    style={{ flex: 1, minWidth: 0 }}
                    type={showKeys[provider.id] ? "text" : "password"}
                    value={provider.api_key}
                    onChange={(e) => onProviderChange(provider.id, { api_key: e.target.value.slice(0, 1024) })}
                  />
                  <button className="iconButton" type="button" onClick={() => onToggleKey(provider.id)}>
                    <i className={["fa-solid", showKeys[provider.id] ? "fa-eye-slash" : "fa-eye"].join(" ")} aria-hidden="true" />
                  </button>
                </div>
              </div>
              <Checkbox label={t("ext.ai.settings.enabled", {}, "Ativo")} checked={provider.enabled} onChange={(enabled) => onProviderChange(provider.id, { enabled })} />
              <Checkbox
                label={t("ext.ai.settings.allow_upload", {}, "Permitir envio de imagem")}
                checked={provider.allow_image_upload}
                onChange={(allow_image_upload) => onProviderChange(provider.id, { allow_image_upload })}
              />
              {test ? (
                <div className={isTestOk(test) ? "settingsStatusMuted" : "errorText"} style={{ marginTop: 8 }}>
                  {isTestOk(test) ? t("ext.ai.settings.ok", {}, "OK") : test.error || t("ext.ai.settings.error", {}, "Erro")}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LimitsSection({
  t,
  limits,
  usage,
  onChange,
}: {
  t: ToposyncHost["i18n"]["t"];
  limits: AiLimitSettings;
  usage: UsageSnapshot | null;
  onChange: (patch: Partial<AiLimitSettings>) => void;
}): React.ReactElement {
  return (
    <div>
      <div className="card">
        <div className="cardBody">
          <div className="rowWrap" style={{ gap: 12 }}>
            <NumberField label={t("ext.ai.settings.concurrency", {}, "Concorrência")} value={limits.max_concurrency} min={1} max={32} onChange={(value) => onChange({ max_concurrency: value })} />
            <NullableNumberField label={t("ext.ai.settings.per_minute", {}, "Por minuto")} value={limits.requests_per_minute} onChange={(value) => onChange({ requests_per_minute: value })} />
            <NullableNumberField label={t("ext.ai.settings.per_hour", {}, "Por hora")} value={limits.requests_per_hour} onChange={(value) => onChange({ requests_per_hour: value })} />
            <NullableNumberField label={t("ext.ai.settings.per_day", {}, "Por dia")} value={limits.requests_per_day} onChange={(value) => onChange({ requests_per_day: value })} />
            <NullableNumberField label={t("ext.ai.settings.per_month", {}, "Por mês")} value={limits.requests_per_month} onChange={(value) => onChange({ requests_per_month: value })} />
          </div>
          <div className="settingsStatusMuted" style={{ marginTop: 10 }}>
            {t("ext.ai.settings.limit_empty", {}, "Vazio significa sem limite.")}
          </div>
        </div>
      </div>
      {usage ? (
        <>
          <div className="sectionDivider" />
          <div className="card">
            <div className="cardBody">
              <div className="modalSectionTitle" style={{ marginBottom: 10 }}>
                {t("ext.ai.settings.usage", {}, "Uso")}
              </div>
              <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(usage.profiles, null, 2)}</pre>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}

function Checkbox({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }): React.ReactElement {
  return (
    <label className="row" style={{ gap: 8, marginTop: 8 }}>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

function NumberField({
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
}): React.ReactElement {
  return (
    <div className="field" style={{ minWidth: 140, flex: "1 1 140px" }}>
      <div className="label">{label}</div>
      <input
        className="input"
        type="number"
        min={min}
        max={max}
        step={step}
        value={Number.isFinite(value) ? value : min}
        onChange={(event) => onChange(clamp(Number(event.target.value), min, max))}
      />
    </div>
  );
}

function NullableNumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
}): React.ReactElement {
  return (
    <div className="field" style={{ minWidth: 130, flex: "1 1 130px" }}>
      <div className="label">{label}</div>
      <input
        className="input"
        type="number"
        min={1}
        value={value ?? ""}
        onChange={(event) => {
          const raw = event.target.value.trim();
          onChange(raw ? Math.max(1, Number(raw) || 1) : null);
        }}
      />
    </div>
  );
}

function normalizeSettings(raw: Record<string, unknown>, defaults: AiExtensionSettings | null): AiExtensionSettings {
  const base = defaults ?? HARD_DEFAULTS;
  const providers = normalizeProviders(raw.providers).length ? normalizeProviders(raw.providers) : base.providers.map(normalizeProvider);
  const profiles = normalizeProfiles(raw.profiles).length ? normalizeProfiles(raw.profiles) : base.profiles.map(normalizeProfile);
  const default_profile_id = asString(raw.default_profile_id, base.default_profile_id) || profiles[0]?.id || DEFAULT_PROFILE_ID;
  return {
    default_profile_id,
    providers: ensureOllamaProvider(providers),
    profiles: ensureDefaultProfiles(profiles),
    limits: normalizeLimits(isRecord(raw.limits) ? raw.limits : base.limits),
    model_catalog_version: asString(raw.model_catalog_version, base.model_catalog_version),
  };
}

function normalizeProviders(value: unknown): AiProviderConfig[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map(normalizeProvider);
}

function normalizeProvider(value: Record<string, unknown>): AiProviderConfig {
  const kind = normalizeProviderKind(value.kind);
  return {
    id: asString(value.id, createId(kind || "provider")),
    name: asString(value.name, kind === "ollama" ? "Ollama local" : providerKindLabel(kind)),
    kind,
    host: asString(value.host, kind === "ollama" ? DEFAULT_OLLAMA_HOST : ""),
    api_key: asString(value.api_key, ""),
    enabled: asBoolean(value.enabled, true),
    local: kind === "ollama" ? true : asBoolean(value.local, false),
    allow_image_upload: kind === "ollama" ? false : asBoolean(value.allow_image_upload, false),
  };
}

function normalizeProfiles(value: unknown): AiProfileConfig[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map(normalizeProfile);
}

function normalizeProfile(value: Record<string, unknown>): AiProfileConfig {
  return {
    id: asString(value.id, createId("profile")),
    name: asString(value.name, "AI profile"),
    provider_id: asString(value.provider_id, DEFAULT_OLLAMA_PROVIDER_ID),
    model: asString(value.model, DEFAULT_OLLAMA_MODEL),
    fallback_profile_ids: Array.isArray(value.fallback_profile_ids) ? value.fallback_profile_ids.map((item) => asString(item, "")).filter(Boolean) : [],
    capabilities: Array.isArray(value.capabilities)
      ? value.capabilities.map((item) => asString(item, "")).filter(Boolean)
      : ["vision", "structured_json", "bbox", "boolean_filter"],
    timeout_seconds: clamp(asNumber(value.timeout_seconds, 60), 1, 600),
    max_image_side_px: clamp(asNumber(value.max_image_side_px, 1280), 128, 8192),
    jpeg_quality: clamp(asNumber(value.jpeg_quality, 85), 30, 100),
    temperature: clamp(asNumber(value.temperature, 0), 0, 2),
    enabled: asBoolean(value.enabled, true),
  };
}

function normalizeLimits(value: Record<string, unknown>): AiLimitSettings {
  return {
    max_concurrency: clamp(asNumber(value.max_concurrency, 1), 1, 32),
    requests_per_minute: nullableNumber(value.requests_per_minute, 20),
    requests_per_hour: nullableNumber(value.requests_per_hour, 300),
    requests_per_day: nullableNumber(value.requests_per_day, 2000),
    requests_per_month: nullableNumber(value.requests_per_month, null),
  };
}

function ensureOllamaProvider(providers: AiProviderConfig[]): AiProviderConfig[] {
  if (providers.some((provider) => provider.kind === "ollama")) return providers;
  return [...HARD_DEFAULTS.providers, ...providers];
}

function ensureDefaultProfiles(profiles: AiProfileConfig[]): AiProfileConfig[] {
  if (profiles.length) return profiles;
  return HARD_DEFAULTS.profiles.map(normalizeProfile);
}

function sectionLabel(section: Section, t: ToposyncHost["i18n"]["t"]): string {
  if (section === "ollama") return t("ext.ai.settings.ollama", {}, "Ollama");
  if (section === "profiles") return t("ext.ai.settings.profiles", {}, "Perfis");
  if (section === "providers") return t("ext.ai.settings.providers", {}, "Provedores");
  return t("ext.ai.settings.limits", {}, "Limites");
}

function providerKindLabel(kind: AiProviderKind): string {
  if (kind === "openai") return "OpenAI";
  if (kind === "anthropic") return "Anthropic";
  if (kind === "google") return "Google";
  if (kind === "litellm") return "LiteLLM";
  return "Ollama";
}

function providerLabel(provider?: AiProviderConfig): string {
  if (!provider) return "-";
  return provider.name.trim() || providerKindLabel(provider.kind);
}

function profileLabel(profile: AiProfileConfig): string {
  return profile.name.trim() || profile.model.trim() || profile.id;
}

function hasInstalledModel(installedModelRefs: Set<string>, ...values: unknown[]): boolean {
  return values.some((value) => {
    const ref = normalizeModelRef(value);
    return Boolean(ref) && installedModelRefs.has(ref);
  });
}

function normalizeModelRef(value: unknown): string {
  return asString(value, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function normalizeProviderKind(value: unknown): AiProviderKind {
  const text = asString(value, "ollama");
  if (text === "openai" || text === "anthropic" || text === "google" || text === "litellm" || text === "ollama") return text;
  return "ollama";
}

function defaultCloudModel(kind: AiProviderKind): string {
  if (kind === "openai") return "gpt-4o-mini";
  if (kind === "anthropic") return "claude-3-5-sonnet-latest";
  if (kind === "google") return "gemini-2.0-flash";
  return "";
}

function asString(value: unknown, fallback: string): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
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

function nullableNumber(value: unknown, fallback: number | null): number | null {
  if (value === null || value === "") return null;
  const parsed = asNumber(value, Number.NaN);
  if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
  return Math.round(parsed);
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

function createId(prefix: string): string {
  const safePrefix = prefix.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "ai";
  return `${safePrefix}_${Date.now().toString(36)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function isTestOk(value: ProviderTestResponse | { error: string }): value is ProviderTestResponse {
  return Boolean((value as ProviderTestResponse).ok);
}

function formatBytes(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let idx = 0;
  while (n >= 1024 && idx < units.length - 1) {
    n /= 1024;
    idx += 1;
  }
  return `${n.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
}
