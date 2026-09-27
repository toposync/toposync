import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { HostI18n, Notification } from "@toposync/plugin-api";
import { AudioObservationDetails, audioHistoryTranslations, supportsAudioObservation } from "./AudioObservationDetails";

test("audio history explains closure without claiming calibrated confidence or retained media", () => {
  const i18n = { useI18n: () => ({ locale: "pt-BR", t: (key: string, values: Record<string, unknown> = {}) => {
    let text = audioHistoryTranslations["pt-BR"][key as keyof typeof audioHistoryTranslations["pt-BR"]] || key;
    for (const [name, value] of Object.entries(values)) text = text.replace(`{{${name}}}`, String(value));
    return text;
  } }) } as unknown as HostI18n;
  const notification = { id: "audio", payload: { data: { historical_evidence: {
    audio_analysis: { baseline: { reference: "<normal>" }, duration_seconds: 1, transcription: { text: "<texto>" } },
    audio_occurrence: { status: "returned_to_baseline" },
  } } } } as unknown as Notification;
  assert.equal(supportsAudioObservation(notification), true);
  assert.equal(supportsAudioObservation({ payload: {} } as Notification), false);
  let html = renderToStaticMarkup(<AudioObservationDetails notification={notification} i18n={i18n} />);
  assert.match(html, /voltar à faixa da referência/);
  assert.match(html, /&lt;normal&gt;/);
  assert.match(html, /&lt;texto&gt;/);
  assert.match(html, /Não é a duração total/);
  assert.match(html, /gravação de áudio não foi retida/);
  const evidence = (notification.payload as any).data.historical_evidence;
  evidence.audio_occurrence.status = "evidence_timeout";
  evidence.audio_analysis.duration_seconds = Number.NaN;
  html = renderToStaticMarkup(<AudioObservationDetails notification={notification} i18n={i18n} />);
  assert.match(html, /Não foi confirmado silêncio/);
  assert.doesNotMatch(html, /NaN|Último trecho analisado/);
  Object.assign(notification.payload as Record<string, unknown>, { status: "closed", reason: "runtime_restart" });
  evidence.audio_occurrence.status = "above_baseline";
  html = renderToStaticMarkup(<AudioObservationDetails notification={notification} i18n={i18n} />);
  assert.match(html, /interrompida pelo reinício/);
  assert.match(html, /não foram confirmados continuidade nem silêncio/);
  assert.doesNotMatch(html, /O som superou/);
});
