import React, { useEffect, useRef, useState } from "react";
import { requestJson, type HostI18n, type Notification } from "@toposync/plugin-api";

const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
function transcription(notification: Notification) {
  return record(record(record(record(notification.payload).data).historical_evidence).audio_analysis).transcription;
}
export function supportsTranscriptionReview(notification: Notification): boolean {
  const text = record(transcription(notification)).text;
  return typeof text === "string" && Boolean(text.trim());
}
type Review = { status: string; interpretation?: string; citations: { segment_index: number; text: string; offset_seconds: number; duration_seconds: number }[] };

export function AiTranscriptionReview({ notification, i18n }: { notification: Notification; i18n: HostI18n }) {
  const { t, locale } = i18n.useI18n();
  const key = JSON.stringify([notification.id, notification.updatedAt, notification.payload]);
  const operation = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ key: string; value: Review } | null>(null);
  useEffect(() => {
    const clear = () => { operation.current?.abort(); operation.current = null; setBusy(false); setResult(null); };
    clear(); window.addEventListener("focus", clear);
    return () => { operation.current?.abort(); window.removeEventListener("focus", clear); };
  }, [key]);
  async function review() {
    if (operation.current) return;
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setResult(null);
    try {
      const value = await requestJson<Review>(`/api/ai/history/${encodeURIComponent(notification.id)}/review-transcription`, {
        method: "POST", headers: { "content-type": "application/json" }, body: "{}", signal: controller.signal,
      });
      if (!controller.signal.aborted) setResult({ key, value });
    } catch {
      if (!controller.signal.aborted) setResult({ key, value: { status: "unavailable", citations: [] } });
    } finally {
      if (operation.current === controller) { operation.current = null; setBusy(false); }
    }
  }
  const value = result?.key === key ? result.value : null;
  const label = value?.status === "reviewed" ? value.interpretation : value?.status;
  return <section className="card" aria-label={t("ext.ai.transcript.title")}>
    <div className="cardBody" style={{ overflowWrap: "anywhere" }}>
      <p>{t("ext.ai.transcript.limit")}</p>
      <button type="button" className="primaryButton" disabled={busy} onClick={() => void review()}>
        {t(busy ? "ext.ai.transcript.busy" : "ext.ai.transcript.title")}
      </button>
      {value && <div aria-live="polite">
        <p>{t(`ext.ai.transcript.${label}`)}</p>
        {value.citations.map(citation => <blockquote key={citation.segment_index}>
          <p>{citation.text}</p>
          <small>{t("ext.ai.transcript.offset", { seconds: citation.offset_seconds.toLocaleString(locale, { maximumFractionDigits: 2 }) })}</small>
        </blockquote>)}
      </div>}
    </div>
  </section>;
}

export const transcriptionReviewTranslations = {
  en: {
    "ext.ai.transcript.title": "Review recorded words",
    "ext.ai.transcript.busy": "Reviewing…",
    "ext.ai.transcript.limit": "Local interpretation of automatic transcription. Review the words and context; this does not confirm danger, safety or intent and does not trigger an action.",
    "ext.ai.transcript.possible_threat": "Possibly threatening words. Check the cited text and original context.",
    "ext.ai.transcript.negated": "The words negate the threatening act. This does not establish safety.",
    "ext.ai.transcript.quoted_or_fictional": "The words appear to quote other speech or fictional content. Attribution and actual circumstances remain unverified.",
    "ext.ai.transcript.no_threat_identified": "No threatening words identified in this excerpt. This does not establish safety or continuous coverage.",
    "ext.ai.transcript.insufficient_context": "Insufficient or unclear transcription/context. Review the original record.",
    "ext.ai.transcript.unavailable": "Interpretation unavailable or evidence changed. Review the original record and local AI settings.",
    "ext.ai.transcript.offset": "Offset in the recorded excerpt: {{seconds}} s",
  },
  "pt-BR": {
    "ext.ai.transcript.title": "Revisar conteúdo da fala",
    "ext.ai.transcript.busy": "Revisando…",
    "ext.ai.transcript.limit": "Interpretação local de transcrição automática. Revise as palavras e o contexto; isso não confirma perigo, segurança ou intenção e não aciona dispositivos.",
    "ext.ai.transcript.possible_threat": "Fala potencialmente ameaçadora. Confira o texto citado e o contexto original.",
    "ext.ai.transcript.negated": "O texto nega a ação ameaçadora. Isso não comprova segurança.",
    "ext.ai.transcript.quoted_or_fictional": "O texto parece citar outra fala ou conteúdo de ficção. Autoria e circunstâncias reais não foram confirmadas.",
    "ext.ai.transcript.no_threat_identified": "Não foi identificada fala ameaçadora neste trecho. Isso não comprova segurança nem cobertura contínua.",
    "ext.ai.transcript.insufficient_context": "Transcrição ou contexto insuficiente ou pouco claro. Revise o registro original.",
    "ext.ai.transcript.unavailable": "Interpretação indisponível ou evidência alterada. Revise o registro original e as configurações locais de AI.",
    "ext.ai.transcript.offset": "Posição no trecho registrado: {{seconds}} s",
  },
};
