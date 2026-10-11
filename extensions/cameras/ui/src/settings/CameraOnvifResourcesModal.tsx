import React, { useEffect, useState } from "react";
import type { HostI18n } from "@toposync/plugin-api";
import type { CameraConfig, CameraOnvifCapabilities, CameraOnvifState, CameraOnvifTransport } from "../types";
import { fetchCameraOnvifCapabilities, observeCameraOnvifEvents } from "../api/camerasApi";
import { SubModal } from "../ui/SubModal";

type Translate = ReturnType<HostI18n["useI18n"]>["t"];

export function CameraOnvifResourcesModal({ camera, open, onClose, t }: {
  camera: CameraConfig; open: boolean; onClose: () => void; t: Translate;
}): React.ReactElement | null {
  const [catalog, setCatalog] = useState<CameraOnvifCapabilities | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [observing, setObserving] = useState(false);
  const [states, setStates] = useState<CameraOnvifState[]>([]);
  const [messages, setMessages] = useState<Array<{ sequence: number; topic: string; qualified_topic: string; operation: string; data: Record<string, string> }>>([]);
  const [gap, setGap] = useState(false);
  const [observationError, setObservationError] = useState("");
  const [transport, setTransport] = useState<CameraOnvifTransport | undefined>();
  useEffect(() => { setObserving(false); setStates([]); setMessages([]); setGap(false); setObservationError(""); setTransport(undefined); }, [open, camera.id]);
  useEffect(() => {
    if (!open || camera.control.type !== "onvif") return;
    const controller = new AbortController();
    setCatalog(null); setLoading(true); setError(false);
    fetchCameraOnvifCapabilities(camera.id, refresh > 0, controller.signal)
      .then(value => { if (!controller.signal.aborted) setCatalog(value); })
      .catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, camera.id, camera.control.type, refresh]);
  useEffect(() => {
    if (!open || !observing) return;
    const controller = new AbortController();
    let sequence = 0;
    let timer: ReturnType<typeof setTimeout>;
    const deadline = Date.now() + 60_000;
    async function poll(): Promise<void> {
      try {
        const value = await observeCameraOnvifEvents(camera.id, sequence, controller.signal);
        if (controller.signal.aborted) return;
        const batch = value.events;
        sequence = batch.length ? batch[batch.length - 1].sequence : value.last_sequence;
        setStates(value.states); setGap(value.gap || !value.available); setObservationError(value.error || "");
        setTransport(value.transport);
        setMessages(previous => [...previous, ...batch].slice(-32));
      } catch {
        if (controller.signal.aborted) return;
        setGap(true); setObservationError("");
      }
      if (Date.now() >= deadline) setObserving(false);
      else if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 1500);
    }
    void poll();
    const stop = setTimeout(() => setObserving(false), 60_000);
    return () => { controller.abort(); clearTimeout(timer); clearTimeout(stop); };
  }, [open, observing, camera.id]);
  const status = (value: boolean | null | undefined): string => value === true
    ? t("ext.cameras.onvif.announced") : value === false ? t("ext.cameras.onvif.unsupported") : t("ext.cameras.onvif.unknown");
  return <SubModal open={open} title={`${t("ext.cameras.onvif.title")} · ${camera.name || camera.id}`} onClose={onClose}>
    <div className="column" style={{ gap: 16, overflowWrap: "anywhere", minWidth: 0 }}>
      <p className="settingsDescription">{t("ext.cameras.onvif.help")}</p>
      <p className="settingsStatusMuted">{t(camera.control.type === "onvif" ? "ext.cameras.onvif.saved" : "ext.cameras.onvif.manual")}</p>
      {camera.control.type === "onvif" ? <button type="button" className="chipButton" disabled={loading || observing} onClick={() => setRefresh(value => value + 1)}>{t("ext.cameras.onvif.refresh")}</button> : null}
      {loading ? <p role="status">{t("ext.cameras.onvif.loading")}</p> : null}
      {error ? <p role="alert">{t("ext.cameras.onvif.error")}</p> : null}
      {catalog ? <>
        <p className="settingsStatusMuted">{new Date(catalog.queried_at * 1000).toLocaleString()} · {t(catalog.status === "partial" ? "ext.cameras.onvif.partial" : "ext.cameras.onvif.consulted")}</p>
        <section><h3>{t("ext.cameras.onvif.identification")}</h3><p>{Object.values(catalog.identification).filter(Boolean).join(" · ") || t("ext.cameras.onvif.unknown")}</p></section>
        <section><h3>{t("ext.cameras.onvif.profiles")}</h3>{catalog.profiles.length ? catalog.profiles.map(profile => <details key={profile.token}>
          <summary>{profile.name || profile.token} · {profile.encoding || t("ext.cameras.onvif.unknown")} · {profile.width ?? "?"} × {profile.height ?? "?"}</summary>
          <p>{profile.token} · {profile.fps ?? "?"} fps · Audio: {profile.audio_encoding || t("ext.cameras.onvif.unknown")}</p>
          <h4>{t("ext.cameras.onvif.ptz")}</h4>
          <ul>{["absolute", "continuous", "relative", "absolute_zoom"].map(action => <li key={action}>{t(`ext.cameras.onvif.${action}`)}: {status(profile.ptz?.[action as "absolute"])}</li>)}</ul>
          <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({ limits: profile.ptz?.limits, presets: profile.ptz?.presets }, null, 2)}</pre>
        </details>) : <p>{t("ext.cameras.onvif.unknown")}</p>}</section>
        <details><summary>{t("ext.cameras.onvif.services")}</summary>{catalog.services.map(service => <p key={service.namespace}>{service.namespace}<br />{t(service.integrated ? "ext.cameras.onvif.integrated" : "ext.cameras.onvif.unintegrated")}</p>)}</details>
        <section><h3>{t("ext.cameras.onvif.events")}</h3>
          {catalog.events.length ? catalog.events.map((event, index) => <details key={index}><summary>{event.label || event.item_name || event.topic} · {t("ext.cameras.onvif.announced")}</summary><p>{event.topic} · {event.item_name}</p><p>{event.qualified_topic}</p></details>) : <p>{t("ext.cameras.onvif.empty")}</p>}
          <button className="chipButton" type="button" onClick={() => setObserving(value => !value)}>{t(observing ? "ext.cameras.onvif.stop" : "ext.cameras.onvif.observe")}</button>
          {observing ? <p role="status">{t("ext.cameras.onvif.observing")}</p> : null}
          {gap ? <p role="status">{t("ext.cameras.onvif.gap")}{observationError ? ` ${observationError}` : ""}</p> : null}
          {transport ? <p role="status">{t(`ext.cameras.onvif.transport.${transport.status}`)} · {t("ext.cameras.onvif.reads")}: {transport.successful_pulls} · {t("ext.cameras.onvif.failures")}: {transport.failed_pulls} · {t("ext.cameras.onvif.subscriptions")}: {transport.subscriptions}{transport.last_pull_at ? ` · ${new Date(transport.last_pull_at * 1000).toLocaleTimeString()}` : ""}</p> : null}
          {transport ? <p>{t("ext.cameras.onvif.messages")}: {transport.received_messages} · {t("ext.cameras.onvif.transitions")}: {transport.transitions} · {t("ext.cameras.onvif.queue_overflows")}: {transport.queue_overflows}</p> : null}
          {states.map((state, index) => <details key={index}><summary>{state.label || state.item_name}: {state.known && state.available ? String(state.value) : t("ext.cameras.onvif.unknown")}</summary><p>{state.qualified_topic || state.topic}</p><pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({ source: state.source, key: state.key }, null, 2)}</pre></details>)}
          {messages.length ? <details><summary>{t("ext.cameras.onvif.observed")} ({messages.length})</summary><p>{t("ext.cameras.onvif.observed_help")}</p>{messages.map(event => <p key={event.sequence}>{event.topic} · {event.operation} · {JSON.stringify(event.data)}</p>)}</details> : null}
        </section>
      </> : null}
    </div>
  </SubModal>;
}
