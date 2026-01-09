import { IMAGES_DEBUG_STORAGE_KEY } from "./constants";

export const imagesDebugEnabled = (() => {
  try {
    return localStorage.getItem(IMAGES_DEBUG_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
})();

export function debugLog(...args: unknown[]): void {
  if (imagesDebugEnabled) console.log(...args);
}

