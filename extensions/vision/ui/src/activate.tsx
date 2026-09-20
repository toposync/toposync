import React from "react";
import type { ToposyncHost } from "@toposync/plugin-api";
import { Gallery, NotificationIdentity, supportsRecognition } from "./Gallery";
import { ModelSetup } from "./ModelSetup";
import { translations } from "./translations";
export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(translations);
  host.registerSettingsPanel({ id: "com.toposync.vision.identities", name: { key: "ext.vision.identity.title" }, description: { key: "ext.vision.identity.description" }, icon: "users", render: ({ i18n }) => <Gallery i18n={i18n} /> });
  host.registerNotificationDetailPanel?.({ id: "com.toposync.vision.identities", supports: supportsRecognition, render: (notification) => <NotificationIdentity key={notification.id} notification={notification} i18n={host.i18n} />, renderSummary: (notification) => <NotificationIdentity key={notification.id} notification={notification} i18n={host.i18n} summary /> });
  for (const operatorId of ["vision.identity_context", "vision.identity_evidence", "vision.recognize_identity"]) {
    host.registerPipelineOperatorPanel({ id: `${operatorId}.panel`, operatorId, render: ({ i18n, config, updateConfig, processingServerId }) => <div className="formGrid"><label><input type="checkbox" checked={config.enabled === true} onChange={(event) => updateConfig({ enabled: event.target.checked })} /> {i18n.t("ext.vision.identity.enabled")}</label><p>{i18n.t(`ext.vision.identity.${operatorId === "vision.identity_context" ? "contextHelp" : "operatorHelp"}`)}</p>{operatorId === "vision.identity_evidence" && <ModelSetup i18n={i18n} processingServerId={processingServerId} config={config} />}</div> });
  }
}
