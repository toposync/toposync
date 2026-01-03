import React, { useCallback, useEffect, useMemo, useState } from "react";

import type {
  Notification,
  NotificationRenderer,
  Overlay3DContribution,
  PanelContribution,
  ToolContribution,
  TopoSyncHost,
} from "@toposync/plugin-api";

import { fetchExtensions, getDevice, emitEvent } from "../util/api";
import { loadRemoteActivate } from "../util/moduleFederation";
import { LampViewport } from "./LampViewport";

type ExtensionRecord = {
  id: string;
  name: string;
  version: string;
  frontend?: {
    kind: string;
    remote_entry_url: string;
    scope: string;
    module: string;
  };
};

export function App(): React.ReactElement {
  const [tools, setTools] = useState<ToolContribution[]>([]);
  const [panels, setPanels] = useState<PanelContribution[]>([]);
  const [overlays3d, setOverlays3d] = useState<Overlay3DContribution[]>([]);
  const [notificationRenderers, setNotificationRenderers] = useState<NotificationRenderer[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [lampState, setLampState] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  const host: TopoSyncHost = useMemo(
    () => ({
      registerTool(tool) {
        setTools((prev) => (prev.some((t) => t.id === tool.id) ? prev : [...prev, tool]));
      },
      registerPanel(panel) {
        setPanels((prev) => (prev.some((p) => p.id === panel.id) ? prev : [...prev, panel]));
      },
      registerOverlay3D(overlay) {
        setOverlays3d((prev) => (prev.some((o) => o.id === overlay.id) ? prev : [...prev, overlay]));
      },
      registerNotificationRenderer(renderer) {
        setNotificationRenderers((prev) =>
          prev.some((r) => r.id === renderer.id) ? prev : [...prev, renderer],
        );
      },
      api: {
        emitEvent,
        getDevice,
      },
    }),
    [],
  );

  const refreshLamp = useCallback(async () => {
    const device = await getDevice("lamp1");
    setLampState(Boolean(device.state));
  }, []);

  const onLampClicked = useCallback(async () => {
    const response = await emitEvent("device.action_requested", { device_id: "lamp1", action: "toggle" });
    if (response?.result?.state !== undefined) setLampState(Boolean(response.result.state));
    else await refreshLamp();

    const state = response?.result?.state ?? null;
    setNotifications((prev) => [
      {
        id: `${Date.now()}`,
        type: "device.state",
        title: "Lamp toggled",
        createdAt: new Date().toISOString(),
        payload: { device_id: "lamp1", state },
      },
      ...prev,
    ].slice(0, 25));
  }, [refreshLamp]);

  useEffect(() => {
    void refreshLamp();
  }, [refreshLamp]);

  useEffect(() => {
    let cancelled = false;

    async function run() {
      const exts: ExtensionRecord[] = await fetchExtensions();
      for (const ext of exts) {
        if (!ext.frontend || ext.frontend.kind !== "module-federation") continue;
        try {
          const activate = await loadRemoteActivate(ext.frontend.remote_entry_url, ext.frontend.scope, ext.frontend.module);
          await activate(host);
        } catch (err) {
          if (cancelled) return;
          const msg = err instanceof Error ? err.message : String(err);
          setErrors((prev) => [...prev, `[${ext.id}] ${msg}`]);
        }
      }
    }

    void run();
    return () => {
      cancelled = true;
    };
  }, [host]);

  return (
    <div className="appRoot">
      <div className="main">
        <div className="topBar">
          <div className="brand">TopoSync</div>
          <div className="spacer" />
          <div className="badge">3D</div>
        </div>
        <LampViewport overlays={overlays3d} lampOn={lampState} onLampClicked={onLampClicked} />
      </div>
      <aside className="sidebar">
        <div className="sectionTitle">Tools</div>
        <div className="toolList">
          <button className="toolButton" onClick={onLampClicked} type="button">
            Toggle Lamp (core)
          </button>
          {tools.map((t) => (
            <button
              className="toolButton"
              key={t.id}
              onClick={() => void t.onTrigger()}
              type="button"
              title={t.id}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="sectionTitle">Panels</div>
        <div className="panelList">
          {panels.map((p) => (
            <div className="panel" key={p.id}>
              <div className="panelTitle">{p.title}</div>
              <div className="panelBody">{p.render()}</div>
            </div>
          ))}
        </div>

        <div className="sectionTitle">Notifications</div>
        <div className="panelList">
          {notifications.length === 0 ? (
            <div className="panel">
              <div className="panelBody">No notifications yet.</div>
            </div>
          ) : null}
          {notifications.map((n) => {
            const renderer = notificationRenderers.find((r) => r.type === n.type);
            return (
              <div className="panel" key={n.id}>
                <div className="panelTitle">{n.title}</div>
                <div className="panelBody">{renderer ? renderer.render(n) : JSON.stringify(n.payload)}</div>
              </div>
            );
          })}
        </div>

        {errors.length > 0 ? (
          <>
            <div className="sectionTitle">Extension Errors</div>
            <ul className="errorList">
              {errors.map((e, idx) => (
                <li className="errorItem" key={idx}>
                  {e}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </aside>
    </div>
  );
}
