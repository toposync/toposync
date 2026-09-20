import React, { useEffect, useMemo, useState } from "react";
import type { HostI18n, Notification, NotificationRenderer, ToposyncHost } from "@toposync/plugin-api";
import { createHumanObservationReader, HUMAN_OBSERVATION_TYPE } from "./humanObservation";
import type { HumanAnchor } from "./humanObservation";
import { createHumanObservation2D, createHumanObservation3D } from "./humanObservationOverlays";
import { HumanObservationPreview } from "./HumanObservationPreview";

function AnchorDetails({ anchor, label, i18n }: { anchor: HumanAnchor; label: string; i18n: HostI18n }) {
  const { t } = i18n.useI18n();
  return <section className="card">
    <div className="cardBody">
      <strong>{label}</strong>
      <p>{t(`ext.cameras.human.${anchor.provenance}`, {}, anchor.provenance)}</p>
      {anchor.position ? <p>{t("ext.cameras.human.position")}: {anchor.position.map((value) => value.toFixed(3)).join(", ")}</p> : null}
      {anchor.contact ? <p className="cardMeta"><code>{anchor.contact}</code></p> : null}
      {anchor.reason ? <p className="cardMeta"><code>{anchor.reason}</code></p> : null}
    </div>
  </section>;
}

export function HumanObservationDetails({ notification, i18n, session }: {
  notification: Notification; i18n: HostI18n; session?: ReturnType<typeof createHumanObservationReader>;
}) {
  const { t } = i18n.useI18n();
  const read = useMemo(() => session ?? createHumanObservationReader(), [notification.id, session]);
  const [, refresh] = useState(0);
  const model = read(notification, Date.now());
  useEffect(() => {
    if (model.state !== "current" || model.expiresAt === null) return;
    const deadline = model.expiresAt;
    const timer = setTimeout(() => {
      read(notification, Math.max(Date.now(), deadline));
      refresh((value) => value + 1);
    }, Math.max(0, deadline - Date.now()));
    return () => clearTimeout(timer);
  }, [notification, model.expiresAt, model.state]);
  return <section className="card">
    <div className="cardBody">
      <h3>{t("ext.cameras.human.title")}</h3>
      <p role="status">{t(`ext.cameras.human.${model.state}`)}</p>
      <p className="cardMeta"><code>{model.reason}</code></p>
      <dl>
        <dt>{t("ext.cameras.human.actor")}</dt><dd>{model.actor ?? "—"}</dd>
        <dt>{t("ext.cameras.human.camera")}</dt><dd>{model.camera ?? "—"}</dd>
        <dt>{t("ext.cameras.human.timestamp")}</dt><dd>{model.timestamp ?? "—"}</dd>
      </dl>
      <p>{t("ext.cameras.human.safety")}</p>
      <p className="cardMeta">{t("ext.cameras.human.clock")}</p>
      <h4>{t("ext.cameras.human.pose_2d")}</h4>
      <HumanObservationPreview notification={notification} model={model} i18n={i18n} />
      {model.pose2D.length ? <details>
        <summary>{t("ext.cameras.human.landmarks")}: {model.pose2D.length}</summary>
        <ul>{model.pose2D.map((landmark, index) => <li key={`${landmark.name}-${index}`}>
          {landmark.name}: {landmark.position.map((value) => value.toFixed(3)).join(", ")}
        </li>)}</ul>
      </details> : <p>{t("ext.cameras.human.unavailable")}</p>}
      <AnchorDetails anchor={model.body} label={t("ext.cameras.human.body")} i18n={i18n} />
      <AnchorDetails anchor={model.left} label={t("ext.cameras.human.left")} i18n={i18n} />
      <AnchorDetails anchor={model.right} label={t("ext.cameras.human.right")} i18n={i18n} />
      <p className="cardMeta">{t("ext.cameras.human.foot_provenance_legend")}</p>
      <p className="cardMeta">{t("ext.cameras.human.uncertainty")}</p>
      <h4>{t("ext.cameras.human.gestures")}</h4>
      <p>{t(`ext.cameras.human.gesture_status_${model.gestureEvidence.status}`)}</p>
      {model.gestureEvidence.reason ? <p className="cardMeta"><code>{model.gestureEvidence.reason}</code></p> : null}
      {model.gestureEvidence.side_evidence ? <>
        <ul>{(["left", "right"] as const).map((side) => {
          const evidence = model.gestureEvidence.side_evidence![side];
          return <li key={side}>
            {t(`ext.cameras.human.gesture_side_${side}`)}: {t(`ext.cameras.human.gesture_evidence_${evidence.status}`)}
            {evidence.reason ? <> · <code>{evidence.reason}</code></> : null}
          </li>;
        })}</ul>
        <p className="cardMeta">{t("ext.cameras.human.gesture_evidence_help")}</p>
      </> : null}
      {model.gestures.length ? <ul>{model.gestures.map((gesture, index) => <li key={`${gesture.name}-${gesture.side}-${index}`}>
        {["hand_raised", "both_hands_raised", "wave", "pointing_candidate"].includes(gesture.name)
          ? t(`ext.cameras.human.gesture_name_${gesture.name}`, {}, gesture.name) : gesture.name}
        {" · "}{["left", "right", "both"].includes(gesture.side)
          ? t(`ext.cameras.human.gesture_side_${gesture.side}`, {}, gesture.side) : gesture.side}
        {gesture.candidate ? ` · ${t("ext.cameras.human.candidate")}` : ""}
      </li>)}</ul> : null}
      <h4>{t("ext.cameras.human.pointing")}</h4>
      <p><code>{model.pointing.status}</code> · <code>{model.pointing.reason}</code></p>
      {model.pointing.selected ? <p>{t("ext.cameras.human.selected")}: {model.pointing.selected}</p> : null}
      {model.pointing.candidates.length ? <>
        <p>{t("ext.cameras.human.candidates")}</p>
        <ul>{model.pointing.candidates.map((candidate, index) => <li key={`${candidate.id}-${index}`}>
          {candidate.id} · <code>{candidate.occlusion}</code>{candidate.distance !== null ? ` · ${candidate.distance.toFixed(3)} m` : ""}
        </li>)}</ul>
      </> : null}
      {!model.pointing.ray ? <p>{t("ext.cameras.human.no_ray")}</p> : null}
      {model.mapRevision ? <p className="cardMeta">{t("ext.cameras.human.map_revision")}: <code>{model.mapRevision}</code></p> : null}
      <p className="cardMeta">{t("ext.cameras.human.map_revision_limit")}</p>
      <p className="cardMeta">{t("ext.cameras.human.partial_2d")}</p>
    </div>
  </section>;
}

function HumanObservationSummary({ i18n }: { i18n: HostI18n }) {
  const { t } = i18n.useI18n();
  // The host wraps list content in a selection button. Keep controls and live
  // image decoding in the selected detail, never inside that button.
  return <span>{t("ext.cameras.human.title")} · {t("ext.cameras.human.safety")}</span>;
}

/** Registration and opt-in payload selection belong to the extension integration. */
export function createHumanObservationRenderer(host: ToposyncHost): NotificationRenderer {
  // Hosts may recreate an overlay on every notification update. Keep the capture
  // deadline/close guard across those recreations, and across 2D/3D switches.
  const sessions = new Map<string, ReturnType<typeof createHumanObservationReader>>();
  const session = (id: string) => {
    let value = sessions.get(id);
    if (!value) {
      value = createHumanObservationReader();
      sessions.set(id, value);
      if (sessions.size > 128) sessions.delete(sessions.keys().next().value!);
    }
    return value;
  };
  return {
    id: HUMAN_OBSERVATION_TYPE,
    type: HUMAN_OBSERVATION_TYPE,
    render: () => <HumanObservationSummary i18n={host.i18n} />,
    renderDetails: (notification) => <HumanObservationDetails key={notification.id} notification={notification} i18n={host.i18n} session={session(notification.id)} />,
    create2DOverlay: (ctx, notification) => createHumanObservation2D(ctx, notification, session(notification.id)),
    create3DOverlay: (ctx, notification) => createHumanObservation3D(ctx, notification, session(notification.id)),
  };
}
