type CatalogModel = {
  modelId: string;
  availability: "available" | "manifest_only" | "incompatible";
  artifactExists: boolean;
  availabilityReason?: string;
};

export type VisionCatalogSelectionState = "checking" | "failed" | "unavailable" | "model_missing" | "select_model" | "resolved";

/** Only a successful catalog for the current server can establish readiness.
 * Static picker fallbacks are labels, never evidence of installed artifacts.
 */
export function resolveVisionCatalogSelection<T extends CatalogModel>(input: {
  serverId: string;
  responseServerId?: string;
  responseOk: boolean;
  loading: boolean;
  error: string | null;
  items: readonly T[] | null;
  modelId: string;
}): { state: VisionCatalogSelectionState; item: T | null; ready: boolean; readyItems: T[] } {
  const empty = (state: VisionCatalogSelectionState) => ({ state, item: null, ready: false, readyItems: [] as T[] });
  if (input.loading) return empty("checking");
  if (input.error) return empty("failed");
  if (input.responseServerId !== input.serverId) return empty("checking");
  if (!input.responseOk || input.items === null) return empty("unavailable");
  const items = input.items.filter((item) => item.availabilityReason !== "fallback");
  const isReady = (item: T) => item.availability === "available" && item.artifactExists === true;
  const readyItems = items.filter(isReady);
  const item = items.find((candidate) => candidate.modelId === input.modelId) ?? null;
  return {
    state: item ? "resolved" : input.modelId ? "model_missing" : "select_model",
    item,
    ready: item !== null && isReady(item),
    readyItems,
  };
}
