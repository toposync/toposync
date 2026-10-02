import type { ToposyncHost } from "@toposync/plugin-api";
import React from "react";

import { createAiConditionFilterOperatorPanel, createAiSmartCropOperatorPanel } from "./operators/AiOperatorPanels";
import { createAiSettingsPanel } from "./settings/AiSettingsPanel";
import { AiHistorySearch } from "./settings/AiHistorySearch";
import { createAiAssistantPanel } from "./settings/AiAssistantPanel";
import { aiTranslations } from "./translations";
import { AiTranscriptionReview, supportsTranscriptionReview, transcriptionReviewTranslations } from "./settings/AiTranscriptionReview";

export function activate(host: ToposyncHost): void {
  host.i18n.registerTranslations(aiTranslations);
  host.i18n.registerTranslations(transcriptionReviewTranslations);
  host.registerNotificationDetailPanel?.({ id: "com.toposync.ai.transcription-review", supports: supportsTranscriptionReview,
    render: notification => <AiTranscriptionReview notification={notification} i18n={host.i18n} /> });
  host.registerSettingsPanel(createAiSettingsPanel());
  host.registerSettingsPanel(createAiAssistantPanel());
  host.registerNotificationHistoryAssistant?.({
    id: "com.toposync.ai.history", name: { key: "ext.ai.history.title", fallback: "Ask about events" },
    render: context => <AiHistorySearch context={context} i18n={host.i18n} />,
  });
  host.registerPipelineOperatorPanel(createAiSmartCropOperatorPanel());
  host.registerPipelineOperatorPanel(createAiConditionFilterOperatorPanel());
}
