import React, { useId } from "react";

import type { Notification2DPin } from "@toposync/plugin-api";

const PRIORITY_COLOR: Record<NonNullable<Notification2DPin["priority"]>, string> = {
  high: "#ef4444",
  medium: "#f59e0b",
  low: "#9aa4b2",
};

type Props = {
  /** Screen-space anchor in pixels — pin tip lands here. */
  screenX: number;
  screenY: number;
  priority?: Notification2DPin["priority"];
  closed?: boolean;
  image?: Notification2DPin["image"];
  companions?: ReadonlyArray<{ screenX: number; screenY: number; image: NonNullable<Notification2DPin["image"]> }>;
  /** Optional trail points already projected to screen-space pixels. */
  trail?: ReadonlyArray<{ x: number; y: number }>;
  trails?: ReadonlyArray<ReadonlyArray<{ x: number; y: number }>>;
};

// Smooth teardrop: tip at (0, 0), circle center at (0, -140), radius 70.
// Tangent angle θ = arccos(R/h) = arccos(70/140) = 60°.
// Tangent points sit at (±70·sin60°, -140 + 70·cos60°) = (±60.62, -105),
// so the straight sides leave the tip and meet the arc tangentially — no kink.
const PIN_PATH = "M 0 0 L -60.62 -105 A 70 70 0 1 1 60.62 -105 Z";

export function Notification2DPinView({ screenX, screenY, priority, closed, trail, trails, image, companions }: Props): React.ReactElement {
  const tone = priority ?? "medium";
  const color = PRIORITY_COLOR[tone];
  const reactId = useId();
  const gradId = `n2dp-grad-${reactId.replace(/:/g, "")}`;

  const trailPath = (trails ?? (trail ? [trail] : []))
    .filter(segment => segment.length >= 2)
    .map(segment => segment.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" "))
    .join(" ");

  return (
    <>
      {[...(companions || []), ...(image ? [{ screenX, screenY, image }] : [])].map((item, index) =>
        <img key={index} className="activityMapImage" src={item.image.url} alt="" style={{ left: item.screenX, top: item.screenY, transform: `translate(${-item.image.anchor[0] * 100}%, ${-item.image.anchor[1] * 100}%)` }} />)}
      {trailPath ? (
        <svg
          className="notification2dTrail"
          aria-hidden="true"
          style={{ ["--notification2d-trail-color" as string]: color }}
        >
          <path d={trailPath} />
        </svg>
      ) : null}
      {!image && <div
        className={`notification2dPin${closed ? " isClosed" : ""}`}
        style={{
          left: screenX,
          top: screenY,
          ["--notification2d-pin-color" as string]: color,
        }}
        aria-hidden="true"
      >
        <span className="notification2dPinSpot" />
        <span className="notification2dPinPulse" />
        <span className="notification2dPinPulse" style={{ animationDelay: "-0.7s" }} />
        <span className="notification2dPinPulse" style={{ animationDelay: "-1.4s" }} />
        <svg
          className="notification2dPinShape"
          viewBox="-80 -240 160 240"
          width="32"
          height="48"
        >
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ffffff" stopOpacity="0.45" />
              <stop offset="55%" stopColor="#ffffff" stopOpacity="0" />
              <stop offset="100%" stopColor="#000000" stopOpacity="0.30" />
            </linearGradient>
          </defs>
          <path d={PIN_PATH} fill="var(--notification2d-pin-color, #f59e0b)" />
          <path d={PIN_PATH} fill={`url(#${gradId})`} />
          <circle cx="0" cy="-140" r="20" fill="#ffffff" />
        </svg>
      </div>}
    </>
  );
}
