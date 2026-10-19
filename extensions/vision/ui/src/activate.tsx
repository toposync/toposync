import React from "react";
import type { ToposyncHost } from "@toposync/plugin-api";
import { Gallery, NotificationIdentity, supportsRecognition } from "./Gallery";
import { CategoryHistoryFilter, categoryHistoryEndpoint, DetectedCategorySummary, detectedCategoryCounts, HistoryFilter, historyEndpoint, HistorySelectionSummary } from "./HistoryFilter";
import { ModelSetup } from "./ModelSetup";
import { translations } from "./translations";
import { manualResultTranslations } from "./ManualAssociationResult";
import { Association, AssociationSummary, associationTranslations } from "./Association";
import { RecognitionPolicyPanel, RecognitionPolicySummary } from "./RecognitionPolicy";
export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(translations);
  host.i18n.registerTranslations(associationTranslations);
  host.i18n.registerTranslations(manualResultTranslations);
  host.registerPipelineOperatorPanel({ id: "vision.associate_entities.panel", operatorId: "vision.associate_entities", renderSummary: config => <AssociationSummary i18n={host.i18n} config={config} />, render: context => <Association i18n={context.i18n} config={context.purpose === "automatic" ? { ...context.config, scope: context.stepUid } : context.config} configurationOnly={context.purpose === "automatic"} recognitionAllowed={context.purpose !== "automatic" || context.observationCapability === "vision.recognition"} updateConfig={patch => context.updateConfig(context.purpose === "automatic" ? { ...patch, scope: context.stepUid } : patch)} /> });
  host.registerNotificationHistoryFilter?.({
    id: "com.toposync.vision.categories", endpoint: categoryHistoryEndpoint,
    renderSummary: ({ value }) => <>{host.i18n.t("ext.vision.identity.historyCategory")}: {host.i18n.t(`ext.vision.identity.${value}`)}</>,
    render: context => <CategoryHistoryFilter {...context} i18n={host.i18n} />,
  });
  host.registerNotificationDetailPanel?.({
    id: "com.toposync.vision.categories", supports: notification => detectedCategoryCounts(notification).length > 0,
    render: notification => <DetectedCategorySummary notification={notification} i18n={host.i18n} />,
  });
  host.registerNotificationHistoryFilter?.({
    id: "com.toposync.vision.identities", endpoint: historyEndpoint,
    renderSummary: ({ value }) => <HistorySelectionSummary value={value} i18n={host.i18n} />,
    render: context => <HistoryFilter {...context} i18n={host.i18n} />,
    subscribe: invalidate => {
      window.addEventListener("toposync:identity-gallery-changed", invalidate);
      return () => window.removeEventListener("toposync:identity-gallery-changed", invalidate);
    },
  });
  host.registerSettingsPanel({ id: "com.toposync.vision.identities", name: { key: "ext.vision.identity.title" }, description: { key: "ext.vision.identity.description" }, icon: "users", render: ({ i18n }) => <Gallery i18n={i18n} ui={host.ui} /> });
  host.registerNotificationDetailPanel?.({ id: "com.toposync.vision.identities", supports: supportsRecognition, render: (notification) => <NotificationIdentity key={notification.id} notification={notification} i18n={host.i18n} ui={host.ui} />, renderSummary: (notification) => <NotificationIdentity key={notification.id} notification={notification} i18n={host.i18n} summary /> });
  host.registerPipelineOperatorPanel({ id: "vision.recognize_identity.panel", operatorId: "vision.recognize_identity",
    renderSummary: config => <RecognitionPolicySummary i18n={host.i18n} config={config} />,
    render: props => <><label hidden={props.purpose === "automatic"}><input type="checkbox" checked={props.config.enabled === true} onChange={event => props.updateConfig({ enabled: event.target.checked })} /> {props.i18n.t("ext.vision.identity.enabled")}</label><RecognitionPolicyPanel {...props} /></> });
  for (const operatorId of ["vision.identity_context", "vision.identity_evidence"]) {
    host.registerPipelineOperatorPanel({ id: `${operatorId}.panel`, operatorId, render: ({ i18n, config, updateConfig, processingServerId }) => <div className="formGrid"><label><input type="checkbox" checked={config.enabled === true} onChange={(event) => updateConfig({ enabled: event.target.checked })} /> {i18n.t("ext.vision.identity.enabled")}</label><p>{i18n.t(`ext.vision.identity.${operatorId === "vision.identity_context" ? "contextHelp" : "operatorHelp"}`)}</p>{operatorId === "vision.identity_evidence" && <ModelSetup i18n={i18n} processingServerId={processingServerId} config={config} />}</div> });
  }
}
