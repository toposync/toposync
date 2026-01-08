import { MODELS_DEBUG_STORAGE_KEY } from "./constants";

export const modelsDebugEnabled = (() => {
  try {
    return localStorage.getItem(MODELS_DEBUG_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
})();

export function debugLog(...args: unknown[]): void {
  if (modelsDebugEnabled) console.log(...args);
}

