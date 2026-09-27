import React, { useState } from "react";
import type { HostI18n, Notification } from "@toposync/plugin-api";
import { resolveToposyncUrl } from "@toposync/plugin-api";
import { readRecord, readString } from "../parsing";

function evidence(notification: Notification) {
  const data = readRecord(readRecord(notification.payload).data);
  return readRecord(data.historical_evidence);
}

export function supportsAudioObservation(notification: Notification): boolean {
  const data = evidence(notification);
  return Object.keys(readRecord(data.audio_analysis)).length > 0
    || Array.isArray(readRecord(data.audio_group).members);
}

export function AudioObservationDetails({ notification, i18n }: { notification: Notification; i18n: HostI18n }) {
  const { t, locale } = i18n.useI18n();
  const data = evidence(notification);
  const analysis = readRecord(data.audio_analysis);
  const occurrence = readRecord(data.audio_occurrence);
  const clip = readRecord(occurrence.clip);
  const clipId = readString(clip.id);
  const [failedClip, setFailedClip] = useState<string | null>(null);
  const retained = clip.status === "retained" && /^[a-f0-9]{32}$/.test(clipId);
  const baseline = readRecord(analysis.baseline);
  const transcription = readRecord(analysis.transcription);
  const duration = analysis.duration_seconds;
  const reasons: Record<string, string> = {
    above_baseline: "above", returned_to_baseline: "quiet", evidence_timeout: "missing",
    evidence_gap: "missing", analysis_unavailable: "missing", source_closed: "ended",
    source_capacity: "ended", stale_evidence: "missing", invalid_clock: "missing",
    invalid_window: "missing", baseline_unavailable: "missing", baseline_changed: "changed",
  };
  const payload = readRecord(notification.payload);
  const key = payload.status === "closed" && payload.reason === "runtime_restart"
    ? "restarted" : reasons[readString(occurrence.status)] ?? "unknown";
  const text = readString(transcription.text).slice(0, 4096);
  const group = readRecord(data.audio_group);
  const members = Array.isArray(group.members) ? group.members.slice(0, 2).map(readRecord) : [];
  if (members.length) return <section className="card" aria-label={t("ext.cameras.audioHistory.group")}>
    <div className="cardBody" style={{ overflowWrap: "anywhere" }}>
      <strong>{t("ext.cameras.audioHistory.group")}</strong>
      <p>{t(members.length > 1 ? "ext.cameras.audioHistory.association" : "ext.cameras.audioHistory.independent")}</p>
      {members.map((member, index) => <details key={readString(member.event_id) || index} open={members.length === 1}
        style={{ marginBottom: 12 }}>
        <summary style={{ minHeight: 44 }}>
          {(readString(member.camera_name) || readString(member.camera_id) || t("ext.cameras.audioHistory.source", { number: index + 1 })).slice(0, 200)}
          {" · "}{t(payload.status === "closed" && payload.reason === "runtime_restart" ? "ext.cameras.audioHistory.interruptedMember" : member.active === true ? "ext.cameras.audioHistory.activeMember" : "ext.cameras.audioHistory.endedMember")}
        </summary>
        <AudioObservationDetails i18n={i18n} notification={{ ...notification, payload: {
          status: payload.status, reason: payload.reason,
          data: { historical_evidence: { audio_analysis: readRecord(member.analysis), audio_occurrence: readRecord(member.occurrence) } },
        } }} />
      </details>)}
    </div>
  </section>;
  return <section className="card" aria-label={t("ext.cameras.audioHistory.title")}>
    <div className="cardBody" style={{ overflowWrap: "anywhere" }}>
      <strong>{t("ext.cameras.audioHistory.title")}</strong>
      <p>{t(`ext.cameras.audioHistory.${key}`)}</p>
      {readString(baseline.reference) && <p>{t("ext.cameras.audioHistory.reference", { name: readString(baseline.reference) })}</p>}
      {typeof duration === "number" && Number.isFinite(duration) && duration > 0 &&
        <p>{t("ext.cameras.audioHistory.window", { seconds: duration.toLocaleString(locale, { maximumFractionDigits: 2 }) })}</p>}
      {retained ? <>
        <p>{t("ext.cameras.audioHistory.excerpt", { seconds: Number(clip.duration_seconds).toLocaleString(locale, { maximumFractionDigits: 2 }) })}</p>
        {failedClip === clipId ? <p role="status">{t("ext.cameras.audioHistory.unavailable")}</p> :
          <audio key={clipId} controls preload="none" style={{ width: "100%", maxWidth: 420 }}
            aria-label={t("ext.cameras.audioHistory.title")} onError={() => setFailedClip(clipId)}
            src={resolveToposyncUrl(`/api/cameras/audio/clips/${clipId}`)} />}
      </> : <p>{t("ext.cameras.audioHistory.retention")}</p>}
      {text && <><p>{t("ext.cameras.audioHistory.transcription")}</p><blockquote>{text}</blockquote></>}
      <p className="cardMeta">{t("ext.cameras.audioHistory.clock")}</p>
    </div>
  </section>;
}

export const audioHistoryTranslations = {
  en: {
    "ext.cameras.audioHistory.group": "Audio observations by camera",
    "ext.cameras.audioHistory.association": "These observations were associated by acoustic similarity. This does not confirm a single physical sound or its location. Review each source.",
    "ext.cameras.audioHistory.independent": "This source has a separate occurrence; no shared sound association was established.",
    "ext.cameras.audioHistory.source": "Source {{number}}",
    "ext.cameras.audioHistory.activeMember": "Observing",
    "ext.cameras.audioHistory.interruptedMember": "Observation interrupted",
    "ext.cameras.audioHistory.endedMember": "Observation ended",

    "ext.cameras.audioHistory.optIn": "Keep a private excerpt of each occurrence for review, for up to 24 hours. Old excerpts may expire earlier when storage is full.",
    "ext.cameras.audioHistory.excerpt": "Retained starting excerpt: {{seconds}} s. It does not cover the full occurrence. Available for up to 24 hours.",
    "ext.cameras.audioHistory.unavailable": "Audio excerpt unavailable, expired or access denied.",
    "ext.cameras.audioHistory.title": "Recorded audio evidence",
    "ext.cameras.audioHistory.above": "Sound exceeded the supplied normal reference. This does not classify its cause or danger.",
    "ext.cameras.audioHistory.quiet": "Observation ended after sound returned to the reference range.",
    "ext.cameras.audioHistory.missing": "Observation ended without continuous valid audio evidence. Quiet was not confirmed.",
    "ext.cameras.audioHistory.ended": "Observation ended because the source or processing stopped.",
    "ext.cameras.audioHistory.restarted": "Observation interrupted by an app restart. Earlier evidence was preserved; continuous observation and quiet were not confirmed.",
    "ext.cameras.audioHistory.changed": "Observation ended because the normal reference changed.",
    "ext.cameras.audioHistory.unknown": "Recorded analysis; the reason for ending is unavailable.",
    "ext.cameras.audioHistory.reference": "Normal reference: {{name}}",
    "ext.cameras.audioHistory.window": "Last analyzed excerpt: {{seconds}} s. This is not the full occurrence duration.",
    "ext.cameras.audioHistory.retention": "The audio recording was not retained. Available evidence consists of measurements and any saved transcription.",
    "ext.cameras.audioHistory.transcription": "Automatic transcription — review against the source; wording may be wrong.",
    "ext.cameras.audioHistory.clock": "Intervals follow decoded samples. Receipt time does not establish synchronized camera capture time or sound location.",
  },
  "pt-BR": {
    "ext.cameras.audioHistory.group": "Observações de áudio por câmera",
    "ext.cameras.audioHistory.association": "Estas observações foram associadas por semelhança acústica. Isso não confirma um único som físico nem sua localização. Revise cada fonte.",
    "ext.cameras.audioHistory.independent": "Esta fonte tem uma ocorrência separada; não foi estabelecida associação de som compartilhado.",
    "ext.cameras.audioHistory.source": "Fonte {{number}}",
    "ext.cameras.audioHistory.activeMember": "Observando",
    "ext.cameras.audioHistory.interruptedMember": "Observação interrompida",
    "ext.cameras.audioHistory.endedMember": "Observação encerrada",

    "ext.cameras.audioHistory.optIn": "Guardar um trecho privado de cada ocorrência para revisão por até 24 horas. Trechos antigos podem expirar antes quando o armazenamento ficar cheio.",
    "ext.cameras.audioHistory.excerpt": "Trecho inicial retido: {{seconds}} s. Não cobre toda a ocorrência. Disponível por até 24 horas.",
    "ext.cameras.audioHistory.unavailable": "Trecho de áudio indisponível, expirado ou sem permissão de acesso.",
    "ext.cameras.audioHistory.title": "Evidência de áudio registrada",
    "ext.cameras.audioHistory.above": "O som superou a referência normal informada. Isso não classifica sua causa nem perigo.",
    "ext.cameras.audioHistory.quiet": "Observação encerrada após o som voltar à faixa da referência.",
    "ext.cameras.audioHistory.missing": "Observação encerrada sem evidência contínua de áudio válido. Não foi confirmado silêncio.",
    "ext.cameras.audioHistory.ended": "Observação encerrada porque a fonte ou o processamento parou.",
    "ext.cameras.audioHistory.restarted": "Observação interrompida pelo reinício do aplicativo. A evidência anterior foi preservada; não foram confirmados continuidade nem silêncio.",
    "ext.cameras.audioHistory.changed": "Observação encerrada porque a referência normal mudou.",
    "ext.cameras.audioHistory.unknown": "Análise registrada; motivo do encerramento indisponível.",
    "ext.cameras.audioHistory.reference": "Referência normal: {{name}}",
    "ext.cameras.audioHistory.window": "Último trecho analisado: {{seconds}} s. Não é a duração total da ocorrência.",
    "ext.cameras.audioHistory.retention": "A gravação de áudio não foi retida. A evidência disponível contém medidas e eventual transcrição salva.",
    "ext.cameras.audioHistory.transcription": "Transcrição automática — revise com a fonte; as palavras podem estar incorretas.",
    "ext.cameras.audioHistory.clock": "Intervalos seguem as amostras decodificadas. O horário de recepção não comprova captura sincronizada nem localização do som.",
  },
};
