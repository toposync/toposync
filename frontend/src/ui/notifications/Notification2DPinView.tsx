import React, { useId } from "react";

import type { Notification2DPin } from "@toposync/plugin-api";

const PRIORITY_COLOR: Record<NonNullable<Notification2DPin["priority"]>, string> = {
  high: "#ff3b3b",
  medium: "#ff3b81",
  low: "#9aa4b2",
};

const TRAIL_COLOR: Record<NonNullable<Notification2DPin["priority"]>, string> = {
  high: "#ff3b3b",
  medium: "#00d1ff",
  low: "#9aa4b2",
};

type Props = {
  /** Screen-space anchor in pixels — pin tip lands here. */
  screenX: number;
  screenY: number;
  priority?: Notification2DPin["priority"];
  closed?: boolean;
  /** Optional trail points already projected to screen-space pixels. */
  trail?: ReadonlyArray<{ x: number; y: number }>;
};

// Smooth teardrop: tip at (0, 0), circle center at (0, -140), radius 70.
// Tangent angle θ = arccos(R/h) = arccos(70/140) = 60°.
// Tangent points sit at (±70·sin60°, -140 + 70·cos60°) = (±60.62, -105),
// so the straight sides leave the tip and meet the arc tangentially — no kink.
const PIN_PATH = "M 0 0 L -60.62 -105 A 70 70 0 1 1 60.62 -105 Z";

export function Notification2DPinView({ screenX, screenY, priority, closed, trail }: Props): React.ReactElement {
  const tone = priority ?? "medium";
  const color = PRIORITY_COLOR[tone];
  const trailColor = TRAIL_COLOR[tone];
  const reactId = useId();
  const gradId = `n2dp-grad-${reactId.replace(/:/g, "")}`;

  const trailPath =
    trail && trail.length >= 2
      ? trail.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ")
      : null;

  return (
    <>
      {trailPath ? (
        <svg
          className="notification2dTrail"
          aria-hidden="true"
          style={{ ["--notification2d-trail-color" as string]: trailColor }}
        >
          <path d={trailPath} />
        </svg>
      ) : null}
      <div
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
          <path d={PIN_PATH} fill="var(--notification2d-pin-color, #ff3b81)" />
          <path d={PIN_PATH} fill={`url(#${gradId})`} />
          <circle cx="0" cy="-140" r="20" fill="#ffffff" />
        </svg>
      </div>
    </>
  );
}
