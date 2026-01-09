import {
  HOME_ASSISTANT_LIVE_DEBUG_STORAGE_KEY,
  HOME_ASSISTANT_RECONNECT_INITIAL_DELAY_MS,
  HOME_ASSISTANT_RECONNECT_MAX_DELAY_MS,
  HOME_ASSISTANT_REST_REFRESH_DELAY_MS,
  HOME_ASSISTANT_STREAM_MAX_ENTITY_IDS,
  HOME_ASSISTANT_STREAM_REFRESH_DELAY_MS,
} from "./constants";
import { fetchHomeAssistantStates } from "./api/home_assistant_api";
import type { HomeAssistantLiveState } from "./types";

type HomeAssistantLiveServerStream = {
  counts: Map<string, number>;
  wanted: Set<string>;
  states: Map<string, HomeAssistantLiveState>;
  listeners: Set<() => void>;
  eventSource: EventSource | null;
  refreshTimer: number | null;
  restRefreshTimer: number | null;
  reconnectTimer: number | null;
  reconnectDelayMs: number;
  lastUrl: string;
};

const liveServers = new Map<string, HomeAssistantLiveServerStream>();

const liveDebugEnabled = (() => {
  try {
    return typeof window !== "undefined" && window.localStorage?.getItem(HOME_ASSISTANT_LIVE_DEBUG_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
})();

function getLiveServerStream(serverId: string): HomeAssistantLiveServerStream {
  const existing = liveServers.get(serverId);
  if (existing) return existing;
  const created: HomeAssistantLiveServerStream = {
    counts: new Map(),
    wanted: new Set(),
    states: new Map(),
    listeners: new Set(),
    eventSource: null,
    refreshTimer: null,
    restRefreshTimer: null,
    reconnectTimer: null,
    reconnectDelayMs: HOME_ASSISTANT_RECONNECT_INITIAL_DELAY_MS,
    lastUrl: "",
  };
  liveServers.set(serverId, created);
  return created;
}

function notifyLiveServer(serverId: string): void {
  const stream = liveServers.get(serverId);
  if (!stream) return;
  for (const listener of stream.listeners) {
    try {
      listener();
    } catch {
      // ignore
    }
  }
  try {
    window.dispatchEvent(new CustomEvent("toposync:invalidate"));
  } catch {
    // ignore
  }
}

function buildStreamUrl(serverId: string, entityIds: Set<string>): string {
  const ids = Array.from(entityIds).slice(0, HOME_ASSISTANT_STREAM_MAX_ENTITY_IDS);
  return `/api/home_assistant/${encodeURIComponent(serverId)}/stream?entity_ids=${encodeURIComponent(ids.join(","))}`;
}

function scheduleRestRefresh(serverId: string): void {
  const stream = liveServers.get(serverId);
  if (!stream) return;
  if (stream.restRefreshTimer) return;
  stream.restRefreshTimer = window.setTimeout(() => {
    stream.restRefreshTimer = null;
    const ids = Array.from(stream.wanted);
    if (ids.length === 0) return;
    fetchHomeAssistantStates(serverId, ids)
      .then((data) => {
        let changed = false;
        for (const [entityId, state] of Object.entries(data)) {
          if (!state || typeof state !== "object") continue;
          stream.states.set(entityId, state as HomeAssistantLiveState);
          changed = true;
        }
        if (changed) notifyLiveServer(serverId);
      })
      .catch(() => {
        // ignore
      });
  }, HOME_ASSISTANT_REST_REFRESH_DELAY_MS);
}

function scheduleLiveReconnect(serverId: string): void {
  const stream = liveServers.get(serverId);
  if (!stream) return;
  if (stream.reconnectTimer) return;
  const delay = stream.reconnectDelayMs;
  stream.reconnectTimer = window.setTimeout(() => {
    stream.reconnectTimer = null;
    openLiveStream(serverId);
  }, delay);
  stream.reconnectDelayMs = Math.min(stream.reconnectDelayMs * 2, HOME_ASSISTANT_RECONNECT_MAX_DELAY_MS);
}

function openLiveStream(serverId: string): void {
  const stream = liveServers.get(serverId);
  if (!stream) return;

  if (stream.wanted.size === 0) {
    if (liveDebugEnabled) console.log("[Home Assistant live] closing stream (no wanted entities)", serverId);
    try {
      stream.eventSource?.close();
    } catch {
      // ignore
    }
    stream.eventSource = null;
    stream.lastUrl = "";
    return;
  }

  const url = buildStreamUrl(serverId, stream.wanted);
  if (!url) return;
  if (url === stream.lastUrl && stream.eventSource) return;
  stream.lastUrl = url;

  try {
    stream.eventSource?.close();
  } catch {
    // ignore
  }

  scheduleRestRefresh(serverId);

  if (liveDebugEnabled) console.log("[Home Assistant live] opening stream", { serverId, url, wanted: Array.from(stream.wanted) });
  const eventSource = new EventSource(url);
  stream.eventSource = eventSource;

  eventSource.onopen = () => {
    if (liveDebugEnabled) console.log("[Home Assistant live] stream open", { serverId, url });
  };

  eventSource.onerror = () => {
    if (stream.eventSource !== eventSource) return;
    if (liveDebugEnabled) console.log("[Home Assistant live] stream error; scheduling reconnect", { serverId, url });
    try {
      eventSource.close();
    } catch {
      // ignore
    }
    stream.eventSource = null;
    stream.lastUrl = "";
    scheduleRestRefresh(serverId);
    scheduleLiveReconnect(serverId);
  };

  eventSource.addEventListener("snapshot", (evt) => {
    try {
      const data = JSON.parse((evt as MessageEvent).data);
      if (!data || typeof data !== "object") return;
      for (const [entityId, state] of Object.entries(data as Record<string, any>)) {
        if (state && typeof state === "object") stream.states.set(entityId, state as HomeAssistantLiveState);
      }
      stream.reconnectDelayMs = HOME_ASSISTANT_RECONNECT_INITIAL_DELAY_MS;
      notifyLiveServer(serverId);
    } catch {
      // ignore
    }
  });

  eventSource.addEventListener("state_changed", (evt) => {
    try {
      const data = JSON.parse((evt as MessageEvent).data);
      const entityId = typeof data?.entity_id === "string" ? data.entity_id : "";
      if (!entityId) return;
      const state = data?.state;
      if (state && typeof state === "object") stream.states.set(entityId, state as HomeAssistantLiveState);
      notifyLiveServer(serverId);
    } catch {
      // ignore
    }
  });
}

function scheduleLiveStreamRefresh(serverId: string): void {
  const stream = liveServers.get(serverId);
  if (!stream) return;
  if (stream.refreshTimer) return;
  stream.refreshTimer = window.setTimeout(() => {
    stream.refreshTimer = null;
    openLiveStream(serverId);
  }, HOME_ASSISTANT_STREAM_REFRESH_DELAY_MS);
}

export function watchHomeAssistantLiveStates(serverId: string, entityIds: string[]): () => void {
  const ids = entityIds.map((s) => s.trim()).filter(Boolean);
  if (!serverId || ids.length === 0) return () => {};

  const stream = getLiveServerStream(serverId);
  for (const id of ids) {
    const nextCount = (stream.counts.get(id) ?? 0) + 1;
    stream.counts.set(id, nextCount);
    stream.wanted.add(id);
  }
  scheduleLiveStreamRefresh(serverId);
  scheduleRestRefresh(serverId);

  return () => {
    const current = liveServers.get(serverId);
    if (!current) return;
    for (const id of ids) {
      const next = (current.counts.get(id) ?? 0) - 1;
      if (next <= 0) {
        current.counts.delete(id);
        current.wanted.delete(id);
        current.states.delete(id);
      } else {
        current.counts.set(id, next);
      }
    }
    scheduleLiveStreamRefresh(serverId);
  };
}

export function subscribeToHomeAssistantLive(serverId: string, listener: () => void): () => void {
  if (!serverId) return () => {};
  const stream = getLiveServerStream(serverId);
  stream.listeners.add(listener);
  return () => {
    stream.listeners.delete(listener);
  };
}

export function getHomeAssistantLiveState(serverId: string, entityId: string): HomeAssistantLiveState | null {
  if (!serverId || !entityId) return null;
  const stream = liveServers.get(serverId);
  if (!stream) return null;
  return stream.states.get(entityId) ?? null;
}

export function setHomeAssistantLiveState(serverId: string, entityId: string, patch: Partial<HomeAssistantLiveState>): void {
  if (!serverId || !entityId) return;
  const stream = getLiveServerStream(serverId);
  const prev = stream.states.get(entityId) ?? { entity_id: entityId };
  stream.states.set(entityId, { ...prev, entity_id: entityId, ...patch });
  notifyLiveServer(serverId);
}

