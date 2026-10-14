import { useEffect, useState } from "react";
import { getGeographicCompositions } from "../../util/api";
import { readGeoreference } from "../../util/georeference";

/** Unknown is deliberately distinct from a composition without a position. */
export function useCompositionPositions(enabled: boolean): Record<string, boolean> | null {
  const [positions, setPositions] = useState<Record<string, boolean> | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setPositions(null);
    getGeographicCompositions(controller.signal)
      .then(items => { if (!controller.signal.aborted) setPositions(Object.fromEntries(items.map(item => [item.id, Boolean(readGeoreference(item.meta?.georeference))]))); })
      .catch(() => { /* Keep unknown rather than claim a position is missing. */ });
    return () => controller.abort();
  }, [enabled]);
  return positions;
}
