import React, { useEffect, useMemo, useState } from "react";

import type { CompositionElement, CompositionElementPatch, HostI18n, TopoSyncHost } from "@toposync/plugin-api";

import { domainFromEntityId, isToggleDomain } from "../domain";
import { readHomeAssistantItemRefs, readRecord, readString } from "../parsing";
import { fetchHomeAssistantRegistry, fetchHomeAssistantStates } from "../api/home_assistant_api";
import { getHomeAssistantLiveState, subscribeToHomeAssistantLive, watchHomeAssistantLiveStates } from "../live_states";
import type { HomeAssistantRegistryResponse } from "../types";

type HomeAssistantActionProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  close: () => void;
  api: TopoSyncHost["api"];
  i18n: HostI18n;
};

export function HomeAssistantAction({
  element,
  update,
  close,
  api,
  i18n,
}: HomeAssistantActionProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const serverId = readString(props.server_id).trim();
  const items = useMemo(() => readHomeAssistantItemRefs(props.items), [props.items]);
  const primaryEntityId = readString(props.primary_entity_id).trim();

  const [registry, setRegistry] = useState<HomeAssistantRegistryResponse | null>(null);
  const [states, setStates] = useState<Record<string, any>>({});
  const [busyEntityId, setBusyEntityId] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const selectedEntityIds = useMemo(() => {
    const out = new Set<string>();
    for (const item of items) {
      if (item.kind === "entity") out.add(item.id);
      if (item.kind === "device" && registry?.device_entities?.[item.id]) {
        for (const entityId of registry.device_entities[item.id] ?? []) out.add(entityId);
      }
    }
    return [...out];
  }, [items, registry]);

  useEffect(() => {
    if (!serverId) return;
    const hasDevices = items.some((i) => i.kind === "device");
    if (!hasDevices) {
      setRegistry(null);
      return;
    }
    let cancelled = false;
    fetchHomeAssistantRegistry(serverId)
      .then((data) => {
        if (!cancelled) setRegistry(data);
      })
      .catch((e) => {
        if (!cancelled) setErrorMessage(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [items, serverId]);

  useEffect(() => {
    if (!serverId) return;
    let cancelled = false;
    setErrorMessage(null);
    fetchHomeAssistantStates(serverId, selectedEntityIds)
      .then((data) => {
        if (!cancelled) setStates(data);
      })
      .catch((e) => {
        if (!cancelled) setErrorMessage(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [serverId, selectedEntityIds.join("|")]);

  useEffect(() => {
    if (!serverId || selectedEntityIds.length === 0) return;
    const unwatch = watchHomeAssistantLiveStates(serverId, selectedEntityIds);
    const unsubscribe = subscribeToHomeAssistantLive(serverId, () => {
      setStates((prev) => {
        const next = { ...prev };
        for (const entityId of selectedEntityIds) {
          const live = getHomeAssistantLiveState(serverId, entityId);
          if (live?.state) next[entityId] = { ...(next[entityId] ?? {}), entity_id: entityId, state: live.state, attributes: live.attributes };
        }
        return next;
      });
    });
    return () => {
      unwatch();
      unsubscribe();
    };
  }, [serverId, selectedEntityIds.join("|")]);

  async function toggle(entityId: string) {
    if (!serverId) return;
    setBusyEntityId(entityId);
    setErrorMessage(null);
    try {
      const res = await api.emitEvent("home_assistant.primary_action_requested", {
        server_id: serverId,
        entity_id: entityId,
      });
      const state = (res as any)?.result?.state;
      if (typeof state === "string") {
        setStates((prev) => ({
          ...prev,
          [entityId]: { ...(prev[entityId] ?? {}), entity_id: entityId, state },
        }));
        if (entityId === primaryEntityId) update({ props: { primary_state: state } });
      }
    } catch (e) {
      setErrorMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyEntityId(null);
    }
  }

  const entityRows = useMemo(() => {
    return selectedEntityIds.map((entityId) => {
      const st = states[entityId] ?? null;
      const state = typeof st?.state === "string" ? st.state : null;
      const domain = domainFromEntityId(entityId);
      const canToggle = isToggleDomain(domain);
      const label =
        readString(st?.attributes?.friendly_name).trim() ||
        items.find((i) => i.kind === "entity" && i.id === entityId)?.name ||
        entityId;

      return { entityId, label, state, canToggle };
    });
  }, [items, selectedEntityIds, states]);

  return (
    <div>
      {!serverId ? (
        <div className="card">
          <div className="cardBody">{t("ext.home_assistant.editor.no_servers")}</div>
        </div>
      ) : null}

      {items.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.home_assistant.action.no_items")}</div>
        </div>
      ) : (
        <div className="choiceList">
          {entityRows.map((row) => (
            <div className="card" key={row.entityId}>
              <div className="cardHeaderRow">
                <div style={{ minWidth: 0 }}>
                  <div className="cardTitle" style={{ marginBottom: 2 }}>
                    {row.label}
                  </div>
                  <div className="cardMeta" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                    {row.entityId}
                    {row.state ? ` • ${row.state}` : ""}
                  </div>
                </div>
                <button
                  className="iconButton iconButtonPrimary"
                  type="button"
                  disabled={!row.canToggle || busyEntityId === row.entityId}
                  aria-label={t("ext.home_assistant.action.toggle")}
                  onClick={() => toggle(row.entityId)}
                >
                  <i
                    className={["fa-solid", busyEntityId === row.entityId ? "fa-spinner" : "fa-power-off"].join(" ")}
                    aria-hidden="true"
                  />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {errorMessage ? (
        <>
          <div className="sectionDivider" />
          <div className="cardBody" style={{ color: "rgba(252,165,165,0.92)" }}>
            {errorMessage}
          </div>
        </>
      ) : null}

      <div className="sectionDivider" />
      <div className="rowWrap">
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}

