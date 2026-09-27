import React, { useEffect, useId, useRef, useState } from "react";
import { resolveToposyncUrl, type HostI18n } from "@toposync/plugin-api";

type Analysis = {
  status: string;
  baseline: { reference: string } | null;
  transcription?: {
    status: string;
    segments: Array<{ offset_seconds: number; duration_seconds: number; text: string }>;
  };
  windows: Array<{
    offset_seconds: number;
    duration_seconds: number;
    status: string;
    rms: number;
    dominant_frequency_hz: number | null;
    clipped_fraction: number;
    anomaly: { status: string };
  }>;
};

export function AudioFileAnalysis({ i18n }: { i18n: HostI18n }): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const label = (key: string) => t(`ext.cameras.audio.${key}`);
  const id = useId();
  const [file, setFile] = useState<File | null>(null);
  const [reference, setReference] = useState<File | null>(null);
  const [language, setLanguage] = useState(locale.startsWith("pt") ? "pt" : "en");
  const [result, setResult] = useState<Analysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  async function analyze() {
    if (!file || busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setResult(null);
    setError("");
    async function request(sample: File, parameters = new URLSearchParams()): Promise<Analysis> {
      if (sample.size > 12_000_000) throw new Error("error");
      parameters.set("started_at", "0");
      parameters.set("clock_basis", "synthetic");
      const response = await fetch(resolveToposyncUrl(`/api/cameras/audio/analyze?${parameters}`), {
        method: "POST", body: sample, signal: controller.signal,
        headers: { "Content-Type": "audio/wav" },
      });
      if (!response.ok) throw new Error("error");
      return response.json() as Promise<Analysis>;
    }
    try {
      const parameters = new URLSearchParams();
      if (reference) {
        const baseline = await request(reference);
        const duration = baseline.windows.reduce((sum, window) => sum + window.duration_seconds, 0);
        const rms = Math.sqrt(baseline.windows.reduce((sum, window) => sum + window.rms ** 2 * window.duration_seconds, 0) / duration);
        if (!Number.isFinite(rms) || rms <= 0) throw new Error("referenceError");
        parameters.set("baseline_rms", String(rms));
        parameters.set("baseline_reference", reference.name.slice(0, 200));
      }
      parameters.set("transcribe", "true");
      if (language) parameters.set("language", language);
      const next = await request(file, parameters);
      if (!controller.signal.aborted) setResult(next);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error && reason.message === "referenceError" ? "referenceError" : "error");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <details className="card">
      <summary style={{ padding: 16, cursor: "pointer" }}>{label("title")}</summary>
      <div className="cardBody" style={{ display: "grid", gap: 12, minWidth: 0 }}>
        <p className="settingsDescription">{label("help")}</p>
        <label className="label" htmlFor={`${id}-file`}>{label("file")}</label>
        <input id={`${id}-file`} type="file" accept=".wav,audio/wav" disabled={busy}
          aria-describedby={`${id}-format`} style={{ maxWidth: "100%" }}
          onChange={(event) => { setFile(event.target.files?.[0] || null); setResult(null); setError(""); }} />
        <p id={`${id}-format`} className="settingsDescription">{label("format")}</p>
        <label className="label" htmlFor={`${id}-language`}>{label("speechLanguage")}</label>
        <select id={`${id}-language`} className="input" value={language} disabled={busy}
          onChange={event => { setLanguage(event.target.value); setResult(null); }}>
          <option value="pt">Português</option>
          <option value="en">English</option>
          <option value="">{label("automaticLanguage")}</option>
        </select>
        <details>
          <summary>{label("advanced")}</summary>
          <p className="settingsDescription">{label("referenceHelp")}</p>
          <label className="label" htmlFor={`${id}-reference`}>{label("reference")}</label>
          <input id={`${id}-reference`} type="file" accept=".wav,audio/wav" disabled={busy} style={{ maxWidth: "100%" }}
            onChange={(event) => { setReference(event.target.files?.[0] || null); setResult(null); setError(""); }} />
        </details>
        <div><button className="primaryButton" type="button" disabled={!file || busy} onClick={() => void analyze()}>
          {label(busy ? "busy" : "analyze")}
        </button></div>
        {error ? <p role="alert">{label(error)}</p> : null}
        <div role="status" aria-live="polite">{busy ? label("busy") : result ? label(result.status === "silent" ? "silent" : "audible") : ""}</div>
        {result ? <>
          <p className="settingsDescription">{label("limitations")}</p>
          <section aria-labelledby={`${id}-transcription`}>
            <h3 id={`${id}-transcription`}>{label("transcriptionTitle")}</h3>
            <p>{label(result.transcription?.status === "no_speech" ? "noSpeech"
              : result.transcription?.status === "busy" ? "transcriptionBusy"
              : result.transcription?.status === "partial" ? "transcriptionPartial"
              : result.transcription?.status === "transcribed" ? "transcriptionReview" : "transcriptionUnavailable")}</p>
            {result.transcription?.segments.map((segment, index) => <p key={index} style={{ overflowWrap: "anywhere" }}>
              <span>{segment.offset_seconds.toFixed(1)}–{(segment.offset_seconds + segment.duration_seconds).toFixed(1)} s: </span>
              {segment.text}
            </p>)}
          </section>
          <p>{result.baseline ? t("ext.cameras.audio.baselineUsed", { name: result.baseline.reference }) : label("baselineMissing")}</p>
          <div style={{ overflowX: "auto", maxWidth: "100%" }}>
            <table style={{ width: "100%", textAlign: "left" }}>
              <caption>{label("windows")}</caption>
              <thead><tr>{["time", "sound", "frequency", "clipping"].map((key) => <th scope="col" key={key}>{label(key)}</th>)}</tr></thead>
              <tbody>{result.windows.map((window) => <tr key={window.offset_seconds}>
                <td>{window.offset_seconds.toFixed(1)}–{(window.offset_seconds + window.duration_seconds).toFixed(1)} s</td>
                <td>{label(window.anomaly.status === "above_baseline" ? "review" : window.status === "silent" ? "silent" : "audible")}</td>
                <td>{window.dominant_frequency_hz === null ? "—" : `${window.dominant_frequency_hz.toFixed(0)} Hz`}</td>
                <td>{(window.clipped_fraction * 100).toFixed(1)}%</td>
              </tr>)}</tbody>
            </table>
          </div>
        </> : null}
      </div>
    </details>
  );
}
