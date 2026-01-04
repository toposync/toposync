import React, { useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  ElementType,
  HostI18n,
  TopoSyncHost,
} from "@toposync/plugin-api";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations({
    en: {
      "ext.hello_lamp.element.name": "Lamp (Hello Lamp)",
      "ext.hello_lamp.element.desc": "Example element: 3D object + action modal + editor modal.",
      "ext.hello_lamp.action.card_title": "Action",
      "ext.hello_lamp.action.state": "State",
      "ext.hello_lamp.action.on": "On",
      "ext.hello_lamp.action.off": "Off",
      "ext.hello_lamp.action.toggling": "Toggling…",
      "ext.hello_lamp.action.toggle": "Toggle",
      "ext.hello_lamp.editor.device_id": "Device ID",
      "ext.hello_lamp.editor.color_on": "Color (on)",
      "ext.hello_lamp.editor.color_off": "Color (off)",
    },
    "pt-BR": {
      "ext.hello_lamp.element.name": "Lâmpada (Hello Lamp)",
      "ext.hello_lamp.element.desc": "Exemplo de elemento: objeto 3D + modal de ação + modal de edição.",
      "ext.hello_lamp.action.card_title": "Ação",
      "ext.hello_lamp.action.state": "Estado",
      "ext.hello_lamp.action.on": "Ligada",
      "ext.hello_lamp.action.off": "Desligada",
      "ext.hello_lamp.action.toggling": "Alternando...",
      "ext.hello_lamp.action.toggle": "Alternar",
      "ext.hello_lamp.editor.device_id": "Device ID",
      "ext.hello_lamp.editor.color_on": "Cor (ligada)",
      "ext.hello_lamp.editor.color_off": "Cor (desligada)",
    },
  });
  host.registerElementType(helloLampElementType(host.i18n));
}

function readString(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}

function readBool(v: unknown, fallback: boolean): boolean {
  return typeof v === "boolean" ? v : fallback;
}

function helloLampElementType(i18n: HostI18n): ElementType {
  return {
    type: "com.toposync.hello_lamp.lamp",
    name: { key: "ext.hello_lamp.element.name", fallback: "Lamp (Hello Lamp)" },
    description: {
      key: "ext.hello_lamp.element.desc",
      fallback: "Example element: 3D object + action modal + editor modal.",
    },
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
    renderActionModal: ({ element, update, close, api }) => (
      <HelloLampAction element={element} update={update} close={close} api={api} i18n={i18n} />
    ),
    renderEditorModal: ({ element, update, remove, close }) => (
      <HelloLampEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

type ActionProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  close: () => void;
  api: { emitEvent: TopoSyncHost["api"]["emitEvent"] };
  i18n: HostI18n;
};

function HelloLampAction({ element, update, close, api, i18n }: ActionProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const deviceId = readString(element.props.device_id, "lamp1");
  const isOn = readBool(element.props.state, false);
  const onColor = readString(element.props.color_on, "#fbbf24");

  return (
    <div>
      <div className="card">
        <div className="cardHeaderRow">
          <div className="cardTitle">{t("ext.hello_lamp.action.card_title")}</div>
          <div className="cardMeta">{deviceId}</div>
        </div>
        <div className="cardBody">
          {t("ext.hello_lamp.action.state")}:{" "}
          <b style={{ color: isOn ? onColor : "rgba(230,232,242,0.65)" }}>
            {isOn ? t("ext.hello_lamp.action.on") : t("ext.hello_lamp.action.off")}
          </b>
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
          {busy ? t("ext.hello_lamp.action.toggling") : t("ext.hello_lamp.action.toggle")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
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
  i18n: HostI18n;
};

function HelloLampEditor({ element, update, remove, close, i18n }: EditorProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const deviceId = readString(element.props.device_id, "lamp1");
  const onColor = readString(element.props.color_on, "#fbbf24");
  const offColor = readString(element.props.color_off, "#334155");

  return (
    <div>
      <div className="field">
        <div className="label">{t("core.element_editor.name")}</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.hello_lamp.editor.device_id")}</div>
          <input
            className="input"
            value={deviceId}
            onChange={(e) => update({ props: { device_id: e.target.value } })}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.hello_lamp.editor.color_on")}</div>
          <input
            className="input"
            value={onColor}
            onChange={(e) => update({ props: { color_on: e.target.value } })}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.hello_lamp.editor.color_off")}</div>
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
          {t("core.actions.delete")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}
