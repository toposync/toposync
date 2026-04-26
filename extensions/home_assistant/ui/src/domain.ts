import type { AirflowMode } from "./airflow";

import { BOOLEAN_STATE_DOMAINS, PRIMARY_TOGGLE_DOMAINS } from "./constants";
import type { HomeAssistantLiveState, HomeAssistantSpecialView, HomeAssistantViewMode } from "./types";
import { readString } from "./parsing";

export function isHomeAssistantViewMode(value: unknown): value is HomeAssistantViewMode {
  return value === "floor" || value === "ceiling" || value === "wall";
}

export function readHomeAssistantViewMode(value: unknown): HomeAssistantViewMode {
  return isHomeAssistantViewMode(value) ? value : "floor";
}

export function isHomeAssistantSpecialView(value: unknown): value is HomeAssistantSpecialView {
  return value === "none" || value === "lamp" || value === "airflow" || value === "model" || value === "ceiling_fan";
}

export function readHomeAssistantSpecialView(value: unknown): HomeAssistantSpecialView {
  return isHomeAssistantSpecialView(value) ? value : "none";
}

type ClimateFlow = {
  active: boolean;
  mode: AirflowMode;
  factor: number;
  sig: string;
};

export function climateFlowFromLiveState(live: HomeAssistantLiveState | null, fallbackStateRaw: string): ClimateFlow {
  const state = readString(live?.state ?? fallbackStateRaw).trim().toLowerCase();
  const attrs = live?.attributes && typeof live.attributes === "object" ? (live.attributes as Record<string, any>) : null;
  const action = attrs ? readString(attrs.hvac_action).trim().toLowerCase() : "";

  if (!state || state === "unknown" || state === "unavailable") {
    return { active: false, mode: "off", factor: 0, sig: `${state}|${action}` };
  }

  if (state === "off" || action === "off") {
    return { active: false, mode: "off", factor: 0, sig: `${state}|${action}` };
  }

  if (action === "idle") {
    const inferredMode: AirflowMode =
      state.includes("heat") ? "heat" : state.includes("cool") || state === "dry" ? "cool" : "neutral";
    return { active: true, mode: inferredMode, factor: 0.22, sig: `${state}|${action}` };
  }

  if (action.includes("heat")) return { active: true, mode: "heat", factor: 1.0, sig: `${state}|${action}` };
  if (action.includes("cool") || action.includes("dry"))
    return { active: true, mode: "cool", factor: 1.0, sig: `${state}|${action}` };
  if (action.includes("fan")) return { active: true, mode: "neutral", factor: 0.75, sig: `${state}|${action}` };

  if (state.includes("heat")) return { active: true, mode: "heat", factor: 0.85, sig: `${state}|${action}` };
  if (state.includes("cool") || state === "dry") return { active: true, mode: "cool", factor: 0.85, sig: `${state}|${action}` };
  if (state === "fan_only") return { active: true, mode: "neutral", factor: 0.65, sig: `${state}|${action}` };

  return { active: true, mode: "neutral", factor: 0.75, sig: `${state}|${action}` };
}

export function domainFromEntityId(entityId: string): string {
  const idx = entityId.indexOf(".");
  if (idx <= 0) return "";
  return entityId.slice(0, idx);
}

export function suggestIconForDomain(domain: string): string {
  const d = domain.toLowerCase();
  if (d === "light") return "lightbulb";
  if (d === "switch") return "toggle-on";
  if (d === "fan") return "fan";
  if (d === "climate") return "thermometer-half";
  if (d === "lock") return "lock";
  if (d === "cover") return "window-maximize";
  if (d === "camera") return "video";
  if (d === "media_player") return "tv";
  return "house";
}

export function isToggleDomain(domain: string): boolean {
  return PRIMARY_TOGGLE_DOMAINS.has(domain.toLowerCase());
}

export function isBooleanStateDomain(domain: string): boolean {
  return BOOLEAN_STATE_DOMAINS.has(domain.toLowerCase());
}

export function boolStateForDomain(domain: string, rawState: string): boolean | null {
  const d = domain.toLowerCase();
  const s = rawState.trim().toLowerCase();
  if (!s || s === "unknown" || s === "unavailable") return null;

  if (d === "lock") return s === "locked";
  if (d === "cover") return s === "closed" || s === "closing";
  if (d === "climate") return s !== "off";

  return s === "on";
}
