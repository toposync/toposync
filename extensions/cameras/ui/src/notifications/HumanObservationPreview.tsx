import React, { useEffect, useMemo, useState } from "react";
import type { HostI18n, Notification } from "@toposync/plugin-api";
import type { HumanObservation } from "./humanObservation";
import { humanImagePrimitives, readHumanObservationImage } from "./humanObservationImage";
import type { HumanImageFrame } from "./humanObservationImage";

const landmarkStyle: React.CSSProperties = {
  fill: "none", stroke: "white", strokeWidth: 2, filter: "drop-shadow(0 0 1px black)",
};

export function HumanObservationPreview({ notification, model, i18n }: {
  notification: Notification; model: HumanObservation; i18n: HostI18n;
}) {
  const { t } = i18n.useI18n();
  const matched = useMemo(() => readHumanObservationImage(notification, model, Date.now()), [notification, model]);
  const [decoded, setDecoded] = useState<{ notificationId: string; frame: HumanImageFrame; url: string } | null>(null);
  // Layout memory only: never retain bytes, URLs, landmarks or a freshness lease.
  const [reservedSize, setReservedSize] = useState<{ notificationId: string; width: number; height: number } | null>(null);
  const [status, setStatus] = useState("image_unavailable");
  useEffect(() => {
    setDecoded(null);
    setReservedSize((previous) => previous?.notificationId === notification.id ? previous : null);
    if (!matched) { setStatus("image_unavailable"); return; }
    let disposed = false, url: string | null = null;
    const remaining = Math.max(0, matched.expiresAt - Date.now());
    const deadline = performance.now() + remaining;
    const image = new Image();
    const release = () => {
      image.onload = null; image.onerror = null; image.src = "";
      if (url) { URL.revokeObjectURL(url); url = null; }
    };
    const timer = setTimeout(() => { disposed = true; release(); setDecoded(null); setStatus("image_unavailable"); }, remaining);
    setStatus("image_loading");
    try {
      const bytes = Uint8Array.from(atob(matched.image.dataBase64), (character) => character.charCodeAt(0));
      url = URL.createObjectURL(new Blob([bytes], { type: matched.image.mimeType }));
      image.onload = () => {
        if (disposed) return;
        if (performance.now() >= deadline || Date.now() >= matched.expiresAt
          || image.naturalWidth !== matched.image.width || image.naturalHeight !== matched.image.height) {
          release(); setStatus("image_invalid"); return;
        }
        setReservedSize({ notificationId: notification.id, width: image.naturalWidth, height: image.naturalHeight });
        setDecoded({ notificationId: notification.id, frame: matched, url: url! }); setStatus("image_ready");
      };
      image.onerror = () => { if (!disposed) { release(); setStatus("image_invalid"); } };
      image.src = url;
    } catch { release(); setStatus("image_invalid"); }
    return () => { disposed = true; clearTimeout(timer); release(); };
  }, [notification.id, matched?.image, matched?.key, matched?.expiresAt]);
  // A new pose never appears over the previously decoded frame while loading.
  const current = decoded && decoded.notificationId === notification.id && matched && decoded.frame.key === matched.key && decoded.frame.image === matched.image
    && Date.now() < decoded.frame.expiresAt ? decoded : null;
  const size = current?.frame.image ?? (reservedSize?.notificationId === notification.id ? reservedSize : null);
  const displayStatus = status === "image_ready" ? matched ? "image_loading" : "image_unavailable" : status;
  const message = t(`ext.cameras.human.${displayStatus}`);
  // No geometry is invented before the first successfully decoded image.
  if (!size) return <p className="cardMeta">{message}</p>;
  const primitives = current ? humanImagePrimitives(current.frame) : null;
  return <figure className="humanObservationPreview" style={{ margin: 0 }}>
    <div className="humanObservationPreviewStage" style={{ position: "relative", aspectRatio: `${size.width} / ${size.height}` }}>
      {current && primitives ? <>
      <img style={{ display: "block", width: "100%", height: "auto" }} src={current.url} alt={t("ext.cameras.human.image_alt", { actor: model.actor ?? "—" })} />
      <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true"
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
        {primitives.segments.map(({ start, end }) => <line key={`${start.name}-${end.name}`} x1={start.position[0]} y1={start.position[1]}
          x2={end.position[0]} y2={end.position[1]} style={{ ...landmarkStyle, strokeDasharray: "4 3" }} vectorEffect="non-scaling-stroke" />)}
        {primitives.points.map((point) => <circle key={point.name} cx={point.position[0]} cy={point.position[1]} r={0.006}
          style={landmarkStyle} vectorEffect="non-scaling-stroke"><title>{point.name}</title></circle>)}
      </svg>
      </> : <p className="cardMeta" style={{ position: "absolute", inset: 0, margin: 0, display: "grid", placeItems: "center" }}>{message}</p>}
    </div>
    <figcaption className="cardMeta">{t("ext.cameras.human.image_legend")}
      <span style={{ display: "grid" }}>
        {/* Reserve the bounded warning's space without displaying stale counts. */}
        <span aria-hidden="true" style={{ gridArea: "1 / 1", visibility: "hidden" }}>{t("ext.cameras.human.image_outside", { count: 128 })}</span>
        <span style={{ gridArea: "1 / 1" }}>{primitives && primitives.outside > 0 ? t("ext.cameras.human.image_outside", { count: primitives.outside }) : ""}</span>
      </span>
    </figcaption>
  </figure>;
}
