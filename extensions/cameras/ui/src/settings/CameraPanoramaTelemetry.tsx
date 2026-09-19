import React, { useId } from "react";

import type { CameraSourcePanoramaTelemetry, CameraSourcePanoramaTelemetrySample } from "../types";

type Translate = (key: string, parameters?: Record<string, unknown>) => string;
const validNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const nonnegative = (value: unknown): value is number => validNumber(value) && value >= 0;

function stateLabel(state: string, text: Translate): string {
  if (state === "stable") return text("telemetry_state_stable");
  if (state === "timeout") return text("telemetry_state_timeout");
  return text(state === "observing" ? "telemetry_state_observing" : "telemetry_state_unknown");
}

export function CameraPanoramaTelemetry({ telemetry, text, locale }: {
  telemetry: CameraSourcePanoramaTelemetry; text: Translate; locale: string;
}): React.ReactElement {
  const identifier = useId();
  const format = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 });
  const percentage = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 2 }).format;
  const number = (value: unknown) => validNumber(value) ? format.format(value) : "—";
  const seconds = (value: number) => text("telemetry_seconds", { value: number(value) });
  const ordered = telemetry.samples.filter((sample) => nonnegative(sample.elapsed_seconds)).sort((a, b) => a.elapsed_seconds - b.elapsed_seconds);
  // The service caps the series too. Keep the view bounded if a newer producer sends more.
  const samples = ordered.length <= 128 ? ordered : Array.from({ length: 128 }, (_, index) => ordered[Math.round(index * (ordered.length - 1) / 127)]);
  const end = Math.max(1, samples[samples.length - 1]?.elapsed_seconds ?? 0);
  const maximum = Math.max(1, ...samples.flatMap((sample) => [sample.motion_pixels, sample.drift_pixels].filter(nonnegative)));
  const hasSpeed = telemetry.timing_basis === "media" && samples.some((sample) => validNumber(sample.media_time) && nonnegative(sample.speed_px_s));
  const hasMediaTime = samples.some((sample) => validNumber(sample.media_time));
  const hasPose = samples.some((sample) => sample.pose && Object.values(sample.pose).some(validNumber));
  const x = (time: number) => 48 + time / end * 570;
  const line = (field: "motion_pixels" | "drift_pixels" | "confidence", top: number, height: number, limit: number) => {
    let connected = false;
    return samples.map((sample) => {
      const value = sample[field];
      if (!nonnegative(value) || (field === "confidence" && value > 1)) { connected = false; return ""; }
      const point = `${x(sample.elapsed_seconds).toFixed(2)},${(top + height - value / limit * height).toFixed(2)}`;
      const segment = `${connected ? "L" : "M"}${point}`;
      connected = true;
      return segment;
    }).join(" ");
  };
  const pose = (sample: CameraSourcePanoramaTelemetrySample) => sample.pose
    ? Object.entries(sample.pose).filter(([, value]) => validNumber(value)).map(([key, value]) => `${text(`telemetry_pose_${["pan", "tilt", "native_pan", "native_tilt"].includes(key) ? key : "unknown"}`)} ${number(value)}`).join(" · ") || "—"
    : "—";
  const events = ["command_accepted_seconds", "first_target_readback_seconds", "first_motion_transition_seconds", "stop_requested_seconds", "stop_accepted_seconds"] as const;

  return <div className="sourcePanoramaTelemetry" data-testid="panorama-telemetry">
    <h3>{text("telemetry_title")}</h3>
    <p className="cardMeta">{text(samples.length === 1 ? "telemetry_scope_one" : "telemetry_scope", { count: number(samples.length), width: number(telemetry.analysis_width) })}</p>
    {samples.length ? <>
      <p>{text("telemetry_outcome", { outcome: text(`telemetry_outcome_${["accepted", "timeout"].includes(telemetry.outcome) ? telemetry.outcome : "inconclusive"}`) })} {text("telemetry_last_state", { state: stateLabel(samples[samples.length - 1].state, text) })}</p>
      <figure>
        <div className="sourcePanoramaTelemetryPlot" tabIndex={0} role="region" aria-label={text("telemetry_chart_region")}>
          <svg viewBox="0 0 640 250" role="img" data-testid="panorama-telemetry-chart" aria-labelledby={`${identifier}-title`} aria-describedby={`${identifier}-description`}>
            <title id={`${identifier}-title`}>{text("telemetry_title")}</title>
            <desc id={`${identifier}-description`}>{text("telemetry_chart_description", { seconds: number(end), maximum: number(maximum) })}</desc>
            {[24, 76, 128, 164, 212].map((y) => <line key={y} x1={48} y1={y} x2={618} y2={y} className="sourcePanoramaTelemetryGrid" />)}
            <text x={42} y={28} textAnchor="end">{number(maximum)}</text><text x={42} y={132} textAnchor="end">{text("telemetry_pixels", { value: number(0) })}</text>
            <text x={42} y={168} textAnchor="end">{percentage(1)}</text><text x={42} y={216} textAnchor="end">{percentage(0)}</text>
            <text x={48} y={239}>{seconds(0)}</text><text x={618} y={239} textAnchor="end">{seconds(end)}</text>
            <path d={line("motion_pixels", 24, 104, maximum)} className="sourcePanoramaTelemetryMotion" />
            <path d={line("drift_pixels", 24, 104, maximum)} className="sourcePanoramaTelemetryDrift" />
            <path d={line("confidence", 164, 48, 1)} className="sourcePanoramaTelemetryConfidence" />
            {(["motion_pixels", "drift_pixels", "confidence"] as const).flatMap((field) => samples.map((sample, index) => {
              const value = sample[field];
              const valid = (candidate: unknown) => nonnegative(candidate) && (field !== "confidence" || candidate <= 1);
              if (!valid(value) || valid(samples[index - 1]?.[field]) || valid(samples[index + 1]?.[field])) return null;
              return <circle key={`${field}:${index}`} cx={x(sample.elapsed_seconds)} cy={field === "confidence" ? 212 - value! * 48 : 128 - value! / maximum * 104} r={3} style={{ fill: field === "drift_pixels" ? "var(--color-accent-teal)" : "var(--color-text-primary)" }} />;
            }))}
          </svg>
        </div>
        <figcaption>{text("telemetry_local_axis")}</figcaption>
      </figure>
      <ul className="sourcePanoramaTelemetryLegend" aria-label={text("telemetry_legend")}>
        <li><span className="sourcePanoramaTelemetryMotion" aria-hidden="true" />{text("telemetry_motion")}</li>
        <li><span className="sourcePanoramaTelemetryDrift" aria-hidden="true" />{text("telemetry_drift")}</li>
        <li><span className="sourcePanoramaTelemetryConfidence" aria-hidden="true" />{text("telemetry_confidence")}</li>
      </ul>
      <p className="cardMeta">{text("telemetry_confidence_note")}</p>
      {!hasSpeed ? <p className="cardMeta">{text("telemetry_speed_unavailable")}</p> : null}
      {events.some((event) => nonnegative(telemetry[event])) ? <dl className="sourcePanoramaTelemetryEvents">{events.filter((event) => nonnegative(telemetry[event])).map((event) => <React.Fragment key={event}><dt>{text(`telemetry_${event}`)}</dt><dd>{seconds(telemetry[event]!)}</dd></React.Fragment>)}</dl> : null}
      <details><summary>{text("telemetry_table")}</summary>
        <div className="sourcePanoramaTelemetryTable" tabIndex={0} role="region" aria-label={text("telemetry_table")}>
          <table><caption>{text("telemetry_table_caption")}</caption><thead><tr>
            <th scope="col">{text("telemetry_elapsed")}</th><th scope="col">{text("telemetry_motion")}</th><th scope="col">{text("telemetry_drift")}</th><th scope="col">{text("telemetry_confidence")}</th><th scope="col">{text("telemetry_state")}</th>
            {hasMediaTime ? <th scope="col">{text("telemetry_media_time")}</th> : null}{hasSpeed ? <th scope="col">{text("telemetry_speed")}</th> : null}{hasPose ? <th scope="col">{text("telemetry_pose")}</th> : null}
          </tr></thead><tbody>{samples.map((sample, index) => <tr key={index}>
            <td>{number(sample.elapsed_seconds)}</td><td>{number(sample.motion_pixels)}</td><td>{number(sample.drift_pixels)}</td><td>{validNumber(sample.confidence) && sample.confidence >= 0 && sample.confidence <= 1 ? percentage(sample.confidence) : "—"}</td><td>{stateLabel(sample.state, text)}</td>
            {hasMediaTime ? <td>{number(sample.media_time)}</td> : null}{hasSpeed ? <td>{validNumber(sample.media_time) && nonnegative(sample.speed_px_s) ? number(sample.speed_px_s) : "—"}</td> : null}{hasPose ? <td>{pose(sample)}</td> : null}
          </tr>)}</tbody></table>
        </div>
        <p className="cardMeta">{text("telemetry_clock_note")}</p>{hasPose ? <p className="cardMeta">{text("telemetry_pose_note")}</p> : null}
      </details>
    </> : <p>{text("telemetry_empty")}</p>}
  </div>;
}
