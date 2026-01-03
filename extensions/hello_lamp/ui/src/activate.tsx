import React from "react";

import type { TopoSyncHost } from "@toposync/plugin-api";

export function activate(host: TopoSyncHost): void {
  host.registerTool({
    id: "com.toposync.hello_lamp.toggle",
    label: "Hello Lamp: Toggle",
    onTrigger: async () => {
      await host.api.emitEvent("device.action_requested", { device_id: "lamp1", action: "toggle" });
    },
  });

  host.registerPanel({
    id: "com.toposync.hello_lamp.panel",
    title: "Hello Lamp",
    render: () => (
      <div>
        <div>Prebuilt extension (wheel) + Module Federation remote.</div>
        <div>Backend hook intercepts <code>device.action_requested</code>.</div>
      </div>
    ),
  });

  host.registerOverlay3D({
    id: "com.toposync.hello_lamp.glow",
    mount: ({ THREE, scene }) => {
      const geometry = new THREE.SphereGeometry(0.12, 18, 18);
      const material = new THREE.MeshStandardMaterial({ color: 0xfbbf24, emissive: 0xffd166 });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(0, 0.65, 0);
      scene.add(mesh);
      return () => {
        scene.remove(mesh);
        geometry.dispose();
        material.dispose();
      };
    },
  });

  host.registerNotificationRenderer({
    id: "com.toposync.hello_lamp.notifications",
    type: "device.state",
    render: (n) => {
      const payload = (n.payload ?? {}) as any;
      const isOn = Boolean(payload.state);
      return (
        <div>
          Lamp is <b style={{ color: isOn ? "#fbbf24" : "#94a3b8" }}>{isOn ? "ON" : "OFF"}</b>
        </div>
      );
    },
  });
}
