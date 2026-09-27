import React, { createContext, useContext } from "react";
import { i18n } from "../../util/i18n";

export type StreamingAvailability = "loading" | "available" | "unavailable" | "unknown";
export const StreamingAvailabilityContext = createContext<StreamingAvailability>("unknown");

export function StreamingAvailabilityBoundary({ children }: { children: React.ReactNode }): React.ReactElement {
  const availability = useContext(StreamingAvailabilityContext);
  const { t } = i18n.useI18n();
  if (availability === "loading" || availability === "unavailable") {
    return <div className="viewportRoot streamsRoot"><div className="main2dCenterHint"><div className="card">
      <div role="status" className="cardBody">{t(availability === "loading" ? "core.ui.loading" : "core.ui.streams.unavailable")}</div>
    </div></div></div>;
  }
  return <>{children}</>;
}
