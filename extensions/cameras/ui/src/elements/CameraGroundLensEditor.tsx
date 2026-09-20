import React, { useId, useState } from "react";
import type { HostI18n } from "@toposync/plugin-api";
import { coefficientNames, groundImageResolutionStatus, groundLensDraft, validateGroundLensDraft } from "../groundLensEditor";
import type { GroundImageSize, GroundLensDraft, LensChoice } from "../groundLensEditor";
import type { CameraRayGroundCalibratedView } from "../types";

export function CameraGroundLensEditor({ view, imageSize, i18n, onDirty, onApply }: {
  view: CameraRayGroundCalibratedView;
  imageSize: GroundImageSize | null;
  i18n: HostI18n;
  onDirty: () => void;
  onApply: (draft: GroundLensDraft) => void;
}) {
  const { t } = i18n.useI18n();
  const id = useId();
  const [draft, setDraft] = useState(() => groundLensDraft(view));
  const [dirty, setDirty] = useState(false);
  const validation = validateGroundLensDraft(draft);
  const resolutionStatus = groundImageResolutionStatus(imageSize, { width: Number(draft.width), height: Number(draft.height) });
  const change = (next: GroundLensDraft) => {
    if (!dirty) onDirty();
    setDirty(true);
    setDraft(next);
  };
  const field = (key: "width" | "height" | "fx" | "fy" | "cx" | "cy") => <div className="field" key={key}>
    <label className="label" htmlFor={`${id}-${key}`}>{t(`ext.cameras.ground_lens.${key}`)}</label>
    <input id={`${id}-${key}`} className="input" inputMode="decimal" value={draft[key]}
      aria-invalid={validation.invalidFields.includes(key)} aria-describedby={`${id}-help ${id}-error`}
      onChange={(event) => change({ ...draft, [key]: event.target.value })} />
  </div>;
  return <section className="card">
    <div className="cardBody">
      <h3>{t("ext.cameras.ground_lens.title")}</h3>
      <p id={`${id}-help`} className="cardMeta">{t("ext.cameras.ground_lens.help")}</p>
      <p className="cardMeta" role="status">{imageSize
        ? t("ext.cameras.ground_lens.actual_resolution").replace("{width}", String(imageSize.width)).replace("{height}", String(imageSize.height))
        : t("ext.cameras.ground_lens.image_required")}</p>
      {resolutionStatus === "mismatch" ? <p className="cardMeta" role="alert">{t("ext.cameras.ground_lens.resolution_mismatch")}</p> : null}
      <div className="field">
        <label className="label" htmlFor={`${id}-choice`}>{t("ext.cameras.ground_lens.model")}</label>
        <select id={`${id}-choice`} className="input" value={draft.choice} onChange={(event) => {
          const choice = event.target.value as LensChoice;
          const sameFamily = choice.startsWith("brown") && draft.choice.startsWith("brown");
          change({ ...draft, choice, coefficients: coefficientNames(choice).map((_, index) => sameFamily ? draft.coefficients[index] ?? "" : "") });
        }}>
          {(["identity", "brown4", "brown5", "brown8", "fisheye4"] as const).map((choice) =>
            <option key={choice} value={choice}>{t(`ext.cameras.ground_lens.${choice}`)}</option>)}
        </select>
      </div>
      {field("width")}{field("height")}
      {draft.choice !== "identity" ? <>
        <p className="cardMeta">{t("ext.cameras.ground_lens.units")}</p>
        {field("fx")}{field("fy")}{field("cx")}{field("cy")}
        <p className="cardMeta">{t("ext.cameras.ground_lens.coefficients")}</p>
        {coefficientNames(draft.choice).map((name, index) => <div className="field" key={name}>
          <label className="label" htmlFor={`${id}-${name}`}>{name}</label>
          <input id={`${id}-${name}`} className="input" inputMode="decimal" value={draft.coefficients[index] ?? ""}
            aria-invalid={validation.invalidFields.includes(name)} aria-describedby={`${id}-help ${id}-error`}
            onChange={(event) => change({ ...draft, coefficients: coefficientNames(draft.choice).map((_, position) => position === index ? event.target.value : draft.coefficients[position] ?? "") })} />
        </div>)}
      </> : <p className="cardMeta">{t("ext.cameras.ground_lens.identity_hint")}</p>}
      <p id={`${id}-error`} className="cardMeta" role="status">{validation.invalidFields.length ? t("ext.cameras.ground_lens.invalid") : dirty ? t("ext.cameras.ground_lens.pending") : ""}</p>
      <button className="chipButton" type="button" disabled={!dirty || !validation.value || resolutionStatus === "mismatch"} onClick={() => {
        if (!validation.value || resolutionStatus === "mismatch") return;
        onApply(draft); setDirty(false);
      }}>{t("ext.cameras.ground_lens.apply")}</button>
    </div>
  </section>;
}
