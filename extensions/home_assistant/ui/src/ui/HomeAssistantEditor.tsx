import React, { useEffect, useMemo, useRef, useState } from "react";

import Select from "react-select";
import type { GroupBase, StylesConfig } from "react-select";

import type { CompositionElement, CompositionElementPatch, HostI18n } from "@toposync/plugin-api";

import {
  BUILT_IN_FONT_AWESOME_SOLID_SVG_BY_NAME,
  getFontAwesomeIconFamiliesCache,
  isFontAwesomeSolidIconAvailable,
  loadFontAwesomeIconFamilies,
  normalizeFontAwesomeSvgName,
  sanitizeFontAwesomeIconName,
} from "../fontAwesome";
import {
  domainFromEntityId,
  isToggleDomain,
  readHomeAssistantSpecialView,
  readHomeAssistantViewMode,
  suggestIconForDomain,
} from "../domain";
import { DEFAULT_AIRFLOW_INTENSITY, DEFAULT_LAMP_COLOR, DEFAULT_LAMP_INTENSITY, AIRFLOW_COMPATIBLE_DOMAINS, LAMP_COMPATIBLE_DOMAINS } from "../constants";
import { readAirflowIntensity, readHexColor, readHomeAssistantItemRefs, readLampIntensity, readRecord, readString, itemValue } from "../parsing";
import { fetchHomeAssistantRegistry, fetchHomeAssistantServers } from "../api/homeAssistantApi";
import type { FontAwesomeIconFamilies, HomeAssistantItemOption, HomeAssistantItemRef, HomeAssistantRegistryResponse, HomeAssistantServerPublic } from "../types";

type HomeAssistantEditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

export function HomeAssistantEditor({
  element,
  update,
  remove,
  close,
  i18n,
}: HomeAssistantEditorProps): React.ReactElement {
  const { t } = i18n.useI18n();

  const props = readRecord(element.props);
  const serverId = readString(props.server_id).trim();
  const icon = sanitizeFontAwesomeIconName(readString(props.icon, "house")) || "house";
  const viewMode = readHomeAssistantViewMode(props.view_mode);
  const specialView = readHomeAssistantSpecialView(props.special_view);
  const primaryEntityId = readString(props.primary_entity_id).trim();
  const lampIntensityValue = readLampIntensity(props.lamp_intensity);
  const lampColorValue = readHexColor(props.lamp_color, DEFAULT_LAMP_COLOR);
  const airflowIntensityValue = readAirflowIntensity(props.airflow_intensity);
  const items = useMemo(() => readHomeAssistantItemRefs(props.items), [props.items]);

  const [servers, setServers] = useState<HomeAssistantServerPublic[]>([]);
  const [registry, setRegistry] = useState<HomeAssistantRegistryResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [loadingRegistry, setLoadingRegistry] = useState(false);

  const [isIconPickerOpen, setIsIconPickerOpen] = useState(false);
  const [iconSearch, setIconSearch] = useState("");
  const [iconFamilies, setIconFamilies] = useState<FontAwesomeIconFamilies | null>(getFontAwesomeIconFamiliesCache());
  const [iconLoadError, setIconLoadError] = useState<string | null>(null);
  const [iconLoading, setIconLoading] = useState(false);
  const iconSearchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchHomeAssistantServers()
      .then((data) => {
        if (!cancelled) setServers(data);
      })
      .catch((e) => {
        if (!cancelled) setErrorMessage(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!isIconPickerOpen) return;

    let cancelled = false;
    setIconLoadError(null);

    const cached = getFontAwesomeIconFamiliesCache();
    if (cached) {
      setIconFamilies(cached);
      return;
    }

    setIconLoading(true);
    loadFontAwesomeIconFamilies()
      .then((data) => {
        if (!cancelled) setIconFamilies(data);
      })
      .catch((e) => {
        if (!cancelled) setIconLoadError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setIconLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [isIconPickerOpen]);

  useEffect(() => {
    if (!isIconPickerOpen) return;
    const id = window.setTimeout(() => iconSearchRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [isIconPickerOpen]);

  useEffect(() => {
    if (serverId) return;
    if (servers.length === 1) update({ props: { server_id: servers[0].id } });
  }, [serverId, servers, update]);

  const canLamp = useMemo(() => {
    if (items.length !== 1) return false;
    if (!primaryEntityId) return false;
    const domain = domainFromEntityId(primaryEntityId).toLowerCase();
    return LAMP_COMPATIBLE_DOMAINS.has(domain);
  }, [items.length, primaryEntityId]);

  const canAirflow = useMemo(() => {
    if (items.length !== 1) return false;
    if (!primaryEntityId) return false;
    const domain = domainFromEntityId(primaryEntityId).toLowerCase();
    return AIRFLOW_COMPATIBLE_DOMAINS.has(domain);
  }, [items.length, primaryEntityId]);

  useEffect(() => {
    if (specialView === "lamp" && !canLamp) update({ props: { special_view: "none" } });
    if (specialView === "airflow" && !canAirflow) update({ props: { special_view: "none" } });
  }, [canAirflow, canLamp, specialView, update]);

  useEffect(() => {
    if (!serverId) {
      setRegistry(null);
      return;
    }
    let cancelled = false;
    setLoadingRegistry(true);
    setErrorMessage(null);
    fetchHomeAssistantRegistry(serverId)
      .then((data) => {
        if (!cancelled) setRegistry(data);
      })
      .catch((e) => {
        if (!cancelled) setErrorMessage(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoadingRegistry(false);
      });
    return () => {
      cancelled = true;
    };
  }, [serverId]);

  const options = useMemo(() => {
    const entities: HomeAssistantItemOption[] = (registry?.entities ?? []).map((entity) => ({
      value: itemValue("entity", entity.entity_id),
      label: entity.name || entity.entity_id,
      kind: "entity",
      id: entity.entity_id,
      meta: { subLabel: entity.entity_id, icon: entity.icon, domain: entity.domain, deviceId: entity.device_id },
    }));
    const devices: HomeAssistantItemOption[] = (registry?.devices ?? []).map((device) => ({
      value: itemValue("device", device.id),
      label: device.name || device.id,
      kind: "device",
      id: device.id,
      meta: { subLabel: device.id },
    }));
    const groups: Array<GroupBase<HomeAssistantItemOption>> = [];
    if (entities.length > 0) groups.push({ label: t("ext.home_assistant.editor.group_entities"), options: entities });
    if (devices.length > 0) groups.push({ label: t("ext.home_assistant.editor.group_devices"), options: devices });
    return groups;
  }, [registry, t]);

  const optionByValue = useMemo(() => {
    const out: Record<string, HomeAssistantItemOption> = {};
    for (const group of options) for (const opt of group.options) out[opt.value] = opt;
    return out;
  }, [options]);

  const selectedOptions = useMemo(() => {
    return items.map(
      (ref) =>
        optionByValue[itemValue(ref.kind, ref.id)] ?? {
          value: itemValue(ref.kind, ref.id),
          label: ref.name || ref.id,
          kind: ref.kind,
          id: ref.id,
        },
    );
  }, [items, optionByValue]);

  const selectStyles: StylesConfig<HomeAssistantItemOption, true, GroupBase<HomeAssistantItemOption>> = useMemo(
    () => ({
      control: (base, state) => ({
        ...base,
        minHeight: 36,
        borderRadius: 12,
        borderColor: state.isFocused ? "rgba(251,191,36,0.45)" : "rgba(255,255,255,0.10)",
        backgroundColor: "rgba(0,0,0,0.20)",
        boxShadow: "none",
      }),
      input: (base) => ({ ...base, color: "rgba(230,232,242,0.92)" }),
      multiValue: (base) => ({
        ...base,
        borderRadius: 999,
        backgroundColor: "rgba(255,255,255,0.08)",
        border: "1px solid rgba(255,255,255,0.10)",
      }),
      multiValueLabel: (base) => ({ ...base, color: "rgba(230,232,242,0.92)", fontWeight: 650 }),
      multiValueRemove: (base) => ({ ...base, color: "rgba(230,232,242,0.78)" }),
      menu: (base) => ({
        ...base,
        backgroundColor: "rgba(14,18,30,0.96)",
        border: "1px solid rgba(255,255,255,0.10)",
        borderRadius: 12,
        overflow: "hidden",
      }),
      option: (base, state) => ({
        ...base,
        backgroundColor: state.isFocused ? "rgba(255,255,255,0.08)" : "transparent",
        color: "rgba(230,232,242,0.92)",
      }),
      groupHeading: (base) => ({
        ...base,
        color: "rgba(230,232,242,0.70)",
        fontSize: 12,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
      }),
      singleValue: (base) => ({ ...base, color: "rgba(230,232,242,0.92)" }),
      placeholder: (base) => ({ ...base, color: "rgba(230,232,242,0.55)" }),
      menuPortal: (base) => ({ ...base, zIndex: 9999 }),
    }),
    [],
  );

  const portalTarget = typeof document !== "undefined" ? document.body : undefined;

  const iconPickerPreview = useMemo(() => {
    if (!iconFamilies) return icon;
    return isFontAwesomeSolidIconAvailable(icon) ? icon : "house";
  }, [icon, iconFamilies]);

  const iconPickerResults = useMemo(() => {
    if (!iconFamilies) return [];
    const query = iconSearch.trim().toLowerCase();

    const suggested = [
      iconPickerPreview,
      "house",
      "bell",
      "lightbulb",
      "toggle-on",
      "fan",
      "temperature-half",
      "lock",
      "video",
      "tv",
      "wifi",
      "plug",
      "power-off",
      "snowflake",
      "sun",
      "door-open",
      "camera",
    ];

    if (!query) {
      const out: string[] = [];
      const seen = new Set<string>();
      for (const name of suggested) {
        const key = normalizeFontAwesomeSvgName(name);
        if (!key || seen.has(key)) continue;
        if (!iconFamilies[key]?.svgs?.classic?.solid && !BUILT_IN_FONT_AWESOME_SOLID_SVG_BY_NAME[key]) continue;
        seen.add(key);
        out.push(key);
      }
      return out;
    }

    const matches: string[] = [];
    for (const [name, entry] of Object.entries(iconFamilies)) {
      if (!entry?.svgs?.classic?.solid) continue;
      if (name.includes(query)) {
        matches.push(name);
        continue;
      }
      const label = (entry.label ?? "").toLowerCase();
      if (label && label.includes(query)) {
        matches.push(name);
        continue;
      }
      const terms = entry.search?.terms ?? [];
      if (Array.isArray(terms) && terms.some((term) => String(term).toLowerCase().includes(query))) {
        matches.push(name);
      }
    }
    matches.sort();
    return matches.slice(0, 220);
  }, [iconFamilies, iconPickerPreview, iconSearch]);

  function setItemsFromOptions(next: readonly HomeAssistantItemOption[]) {
    const refs: HomeAssistantItemRef[] = next.map((opt) => ({
      kind: opt.kind,
      id: opt.id,
      name: opt.label,
      domain: opt.meta?.domain,
      icon: opt.meta?.icon,
      device_id: opt.meta?.deviceId,
    }));

    let nextPrimaryEntityId = "";
    if (refs.length === 1) {
      const one = refs[0];
      if (one.kind === "entity") {
        const domain = one.domain || domainFromEntityId(one.id);
        if (isToggleDomain(domain)) nextPrimaryEntityId = one.id;
      } else if (one.kind === "device" && registry?.device_entities?.[one.id]) {
        const candidates = registry.device_entities[one.id] ?? [];
        const best = candidates.find((entityId) => isToggleDomain(domainFromEntityId(entityId))) ?? "";
        if (best) nextPrimaryEntityId = best;
      }
    }

    const suggestedName =
      refs.length === 1 ? refs[0].name || refs[0].id : refs.length > 1 ? `Home Assistant (${refs.length})` : "";
    const suggestedIcon =
      refs.length === 1
        ? sanitizeFontAwesomeIconName(suggestIconForDomain(refs[0].domain || domainFromEntityId(refs[0].id)))
        : "";

    const patch: CompositionElementPatch = {
      props: { items: refs, primary_entity_id: nextPrimaryEntityId, primary_state: "" },
    };

    update(patch);

    if (!element.name && suggestedName) update({ name: suggestedName });
    const currentIcon = sanitizeFontAwesomeIconName(readString(readRecord(element.props).icon, "")) || "";
    if (!currentIcon && suggestedIcon) update({ props: { icon: suggestedIcon } });
  }

  return (
    <div>
      {errorMessage ? (
        <div className="card">
          <div className="cardBody" style={{ color: "rgba(252,165,165,0.92)" }}>
            {errorMessage}
          </div>
        </div>
      ) : null}

      {servers.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.home_assistant.editor.no_servers")}</div>
        </div>
      ) : (
        <>
          <div className="field">
            <div className="label">{t("core.element_editor.name")}</div>
            <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.server")}</div>
            <select
              className="input"
              value={serverId}
              onChange={(e) => update({ props: { server_id: e.target.value } })}
            >
              <option value="" />
              {servers.map((server) => (
                <option value={server.id} key={server.id}>
                  {server.name ? `${server.name} (${server.host})` : server.host}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.items")}</div>
            <Select<HomeAssistantItemOption, true, GroupBase<HomeAssistantItemOption>>
              isMulti
              isDisabled={!serverId || loadingRegistry}
              options={options}
              value={selectedOptions}
              placeholder={t("ext.home_assistant.editor.items_placeholder")}
              styles={selectStyles}
              menuPortalTarget={portalTarget}
              menuPosition="fixed"
              onChange={(next) => setItemsFromOptions(next ?? [])}
              formatOptionLabel={(opt) => (
                <div style={{ display: "flex", alignItems: "baseline", gap: 10, minWidth: 0 }}>
                  <div style={{ fontWeight: 650, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {opt.label}
                  </div>
                  {opt.meta?.subLabel ? (
                    <div style={{ opacity: 0.7, fontSize: 12, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {opt.meta.subLabel}
                    </div>
                  ) : null}
                </div>
              )}
            />
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.icon")}</div>
            <button
              className="chipButton"
              type="button"
              onClick={() => setIsIconPickerOpen((prev) => !prev)}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                <i
                  className={["fa-solid", `fa-${iconPickerPreview}`].join(" ")}
                  aria-hidden="true"
                  style={{ width: 18, textAlign: "center" }}
                />
                <span style={{ fontWeight: 650, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {iconPickerPreview}
                </span>
              </span>
              <i
                className={["fa-solid", isIconPickerOpen ? "fa-chevron-up" : "fa-chevron-down"].join(" ")}
                aria-hidden="true"
              />
            </button>

            {isIconPickerOpen ? (
              <div className="card" style={{ marginTop: 10 }}>
                <div className="cardBody">
                  <div className="row" style={{ gap: 10 }}>
                    <input
                      ref={iconSearchRef}
                      className="input"
                      style={{ flex: 1, minWidth: 0 }}
                      value={iconSearch}
                      onChange={(e) => setIconSearch(e.target.value.slice(0, 64))}
                      placeholder={t("ext.home_assistant.editor.icon_search")}
                    />
                    <button
                      className="iconButton"
                      type="button"
                      aria-label={t("core.actions.close")}
                      onClick={() => {
                        setIconSearch("");
                        setIsIconPickerOpen(false);
                      }}
                    >
                      <i className={["fa-solid", "fa-xmark"].join(" ")} aria-hidden="true" />
                    </button>
                  </div>

                  {iconLoading ? (
                    <div className="cardMeta" style={{ marginTop: 10 }}>
                      {t("ext.home_assistant.editor.icon_loading")}
                    </div>
                  ) : iconLoadError ? (
                    <div className="cardMeta" style={{ marginTop: 10, color: "rgba(252,165,165,0.92)" }}>
                      {iconLoadError}
                    </div>
                  ) : (
                    <>
                      <div className="cardMeta" style={{ marginTop: 10 }}>
                        {!iconSearch.trim()
                          ? t("ext.home_assistant.editor.icon_suggested")
                          : t("ext.home_assistant.editor.icon_results", { count: iconPickerResults.length })}
                      </div>

                      <div
                        style={{
                          marginTop: 10,
                          display: "grid",
                          gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
                          gap: 8,
                          maxHeight: 260,
                          overflow: "auto",
                          paddingRight: 4,
                        }}
                      >
                        {iconPickerResults.length === 0 ? (
                          <div className="cardMeta">{t("ext.home_assistant.editor.icon_no_results")}</div>
                        ) : (
                          iconPickerResults.map((name) => (
                            <button
                              key={name}
                              className="chipButton"
                              type="button"
                              onClick={() => {
                                update({ props: { icon: name } });
                                setIsIconPickerOpen(false);
                              }}
                              style={{
                                display: "flex",
                                justifyContent: "space-between",
                                gap: 10,
                                alignItems: "center",
                              }}
                            >
                              <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                                <i
                                  className={["fa-solid", `fa-${name}`].join(" ")}
                                  aria-hidden="true"
                                  style={{ width: 18, textAlign: "center" }}
                                />
                                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                                  {name}
                                </span>
                              </span>
                            </button>
                          ))
                        )}
                      </div>
                    </>
                  )}
                </div>
              </div>
            ) : null}

            {iconFamilies && !isFontAwesomeSolidIconAvailable(icon) ? (
              <div className="label" style={{ marginTop: 6, color: "rgba(252,165,165,0.92)" }}>
                {t("ext.home_assistant.editor.icon_not_found")}
              </div>
            ) : null}
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.view_mode")}</div>
            <select
              className="input"
              value={viewMode}
              disabled={specialView === "airflow"}
              onChange={(e) => update({ props: { view_mode: e.target.value } })}
            >
              <option value="floor">{t("ext.home_assistant.editor.view_mode.floor")}</option>
              <option value="ceiling">{t("ext.home_assistant.editor.view_mode.ceiling")}</option>
              <option value="wall">{t("ext.home_assistant.editor.view_mode.wall")}</option>
            </select>
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.special_view")}</div>
            <select
              className="input"
              value={specialView}
              onChange={(e) => {
                const next = readHomeAssistantSpecialView(e.target.value);
                if (next === "lamp") {
                  update({
                    props: {
                      special_view: next,
                      lamp_intensity: lampIntensityValue,
                      lamp_color: lampColorValue,
                    },
                  });
                } else if (next === "airflow") {
                  update({
                    props: {
                      special_view: next,
                      airflow_intensity: airflowIntensityValue,
                      view_mode: "wall",
                    },
                  });
                } else {
                  update({ props: { special_view: "none" } });
                }
              }}
            >
              <option value="none">{t("ext.home_assistant.editor.special_view.none")}</option>
              <option value="lamp" disabled={!canLamp}>
                {t("ext.home_assistant.editor.special_view.lamp")}
              </option>
              <option value="airflow" disabled={!canAirflow}>
                {t("ext.home_assistant.editor.special_view.airflow")}
              </option>
            </select>
            {!canLamp ? (
              <div className="label" style={{ marginTop: 6 }}>
                {t("ext.home_assistant.editor.special_view.hint_lamp")}
              </div>
            ) : null}
            {!canAirflow ? (
              <div className="label" style={{ marginTop: 6 }}>
                {t("ext.home_assistant.editor.special_view.hint_airflow")}
              </div>
            ) : null}
          </div>

          {specialView === "lamp" && canLamp ? (
            <div className="rowWrap">
              <div className="field" style={{ flex: 1, minWidth: 160 }}>
                <div className="label">{t("ext.home_assistant.editor.lamp_color")}</div>
                <input
                  className="input"
                  type="color"
                  value={lampColorValue}
                  onChange={(e) => update({ props: { lamp_color: readHexColor(e.target.value, DEFAULT_LAMP_COLOR) } })}
                />
              </div>
              <div className="field" style={{ flex: 1, minWidth: 180 }}>
                <div className="label">
                  {t("ext.home_assistant.editor.lamp_intensity")}: {lampIntensityValue.toFixed(2)}
                </div>
                <input
                  className="input"
                  type="range"
                  min={0.2}
                  max={3}
                  step={0.05}
                  value={lampIntensityValue}
                  onChange={(e) => update({ props: { lamp_intensity: Number(e.target.value) } })}
                />
              </div>
            </div>
          ) : null}

          {specialView === "airflow" && canAirflow ? (
            <div className="rowWrap">
              <div className="field" style={{ flex: 1, minWidth: 220 }}>
                <div className="label">
                  {t("ext.home_assistant.editor.airflow_intensity")}: {airflowIntensityValue.toFixed(2)}
                </div>
                <input
                  className="input"
                  type="range"
                  min={0.2}
                  max={3}
                  step={0.05}
                  value={airflowIntensityValue}
                  onChange={(e) => update({ props: { airflow_intensity: Number(e.target.value) } })}
                />
              </div>
            </div>
          ) : null}
        </>
      )}

      <div className="sectionDivider" />
      <div className="rowWrap">
        <button className="dangerButton" type="button" onClick={remove}>
          {t("core.actions.delete")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}
