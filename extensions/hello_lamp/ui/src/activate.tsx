import React, { useState } from "react";

import type { CompositionElement, CompositionElementPatch, ElementType, TopoSyncHost } from "@toposync/plugin-api";

export function activate(host: TopoSyncHost): void {
  host.registerElementType(helloLampElementType());
}

function readString(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}

function readBool(v: unknown, fallback: boolean): boolean {
  return typeof v === "boolean" ? v : fallback;
}

function helloLampElementType(): ElementType {
  return {
    type: "com.toposync.hello_lamp.lamp",
    name: "Lâmpada (Hello Lamp)",
    description: "Exemplo de elemento: objeto 3D + modal de ação + modal de edição.",
    defaultProps: {
      device_id: "lamp1",
      state: false,
      color_on: "#fbbf24",
      color_off: "#334155",
    },
    create3D: ({ THREE }, element) => {
      const group = new THREE.Group();

      const bulbGeom = new THREE.SphereGeometry(0.18, 24, 24);
      const bulbMat = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0x000000, roughness: 0.35 });
      const bulb = new THREE.Mesh(bulbGeom, bulbMat);
      bulb.position.set(0, 0.22, 0);
      group.add(bulb);

      const baseGeom = new THREE.CylinderGeometry(0.14, 0.16, 0.18, 24);
      const baseMat = new THREE.MeshStandardMaterial({ color: 0x334155, roughness: 0.85, metalness: 0.25 });
      const base = new THREE.Mesh(baseGeom, baseMat);
      base.position.set(0, 0.02, 0);
      group.add(base);

      const light = new THREE.PointLight(0xffd166, 0, 4.5, 2);
      light.position.set(0, 0.35, 0);
      group.add(light);

      function apply(el: CompositionElement) {
        const on = readBool(el.props.state, false);
        const onColor = readString(el.props.color_on, "#fbbf24");
        const offColor = readString(el.props.color_off, "#334155");

        baseMat.color.set(offColor);
        bulbMat.emissive.set(on ? onColor : "#000000");
        bulbMat.emissiveIntensity = on ? 1.1 : 0.0;
        light.color.set(onColor);
        light.intensity = on ? 1.4 : 0.0;
      }

      apply(element);
      return {
        object: group,
        update: apply,
        dispose: () => {
          bulbGeom.dispose();
          bulbMat.dispose();
          baseGeom.dispose();
          baseMat.dispose();
        },
      };
    },
    renderActionModal: ({ element, update, close, api }) => <HelloLampAction element={element} update={update} close={close} api={api} />,
    renderEditorModal: ({ element, update, remove, close }) => (
      <HelloLampEditor element={element} update={update} remove={remove} close={close} />
    ),
  };
}

type ActionProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  close: () => void;
  api: { emitEvent: TopoSyncHost["api"]["emitEvent"] };
};

function HelloLampAction({ element, update, close, api }: ActionProps): React.ReactElement {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const deviceId = readString(element.props.device_id, "lamp1");
  const isOn = readBool(element.props.state, false);
  const onColor = readString(element.props.color_on, "#fbbf24");

  return (
    <div>
      <div className="card">
        <div className="cardHeaderRow">
          <div className="cardTitle">Ação</div>
          <div className="cardMeta">{deviceId}</div>
        </div>
        <div className="cardBody">
          Estado:{" "}
          <b style={{ color: isOn ? onColor : "rgba(230,232,242,0.65)" }}>{isOn ? "Ligada" : "Desligada"}</b>
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap">
        <button
          className="primaryButton"
          type="button"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setErr(null);
            try {
              const res = await api.emitEvent("device.action_requested", { device_id: deviceId, action: "toggle" });
              const state = (res as any)?.result?.state;
              const next = typeof state === "boolean" ? state : !isOn;
              update({ props: { state: next } });
            } catch (e) {
              setErr(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Alternando..." : "Alternar"}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          Fechar
        </button>
      </div>

      {err ? (
        <>
          <div className="sectionDivider" />
          <div className="cardBody" style={{ color: "rgba(252,165,165,0.92)" }}>
            {err}
          </div>
        </>
      ) : null}
    </div>
  );
}

type EditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
};

function HelloLampEditor({ element, update, remove, close }: EditorProps): React.ReactElement {
  const deviceId = readString(element.props.device_id, "lamp1");
  const onColor = readString(element.props.color_on, "#fbbf24");
  const offColor = readString(element.props.color_off, "#334155");

  return (
    <div>
      <div className="field">
        <div className="label">Nome</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">Device ID</div>
          <input
            className="input"
            value={deviceId}
            onChange={(e) => update({ props: { device_id: e.target.value } })}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">Cor (ligada)</div>
          <input
            className="input"
            value={onColor}
            onChange={(e) => update({ props: { color_on: e.target.value } })}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">Cor (desligada)</div>
          <input
            className="input"
            value={offColor}
            onChange={(e) => update({ props: { color_off: e.target.value } })}
          />
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap">
        <button className="dangerButton" type="button" onClick={remove}>
          Excluir
        </button>
        <button className="chipButton" type="button" onClick={close}>
          Fechar
        </button>
      </div>
    </div>
  );
}
