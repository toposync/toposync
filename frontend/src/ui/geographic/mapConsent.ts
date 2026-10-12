import { useEffect, useState } from "react";
import { getToposyncBasePath } from "@toposync/plugin-api";

const session = new Map<string, boolean>();
const changedEvent = "toposync:map-consent";

export function mapConsentKey(service: string): string {
  return `toposync.map.permission.v1:${getToposyncBasePath()}:${service}`;
}
export function readMapConsent(key: string): boolean | null {
  if (session.has(key)) return session.get(key)!;
  try {
    const value = localStorage.getItem(key);
    return value === "allowed" ? true : value === "denied" ? false : null;
  } catch { return null; }
}
export function saveMapConsent(key: string, allowed: boolean): boolean {
  session.set(key, allowed);
  let persisted = true;
  try { localStorage.setItem(key, allowed ? "allowed" : "denied"); }
  catch { persisted = false; }
  window.dispatchEvent(new Event(changedEvent));
  return persisted;
}
export function useMapConsent(service: string): { decision: boolean | null; persistent: boolean; choose: (allowed: boolean) => void } {
  const key = mapConsentKey(service);
  const [selection, setSelection] = useState(() => ({ key, decision: readMapConsent(key) }));
  const [persistent, setPersistent] = useState(true);
  useEffect(() => {
    const update = () => setSelection({ key, decision: readMapConsent(key) });
    const storage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== key) return;
      session.delete(key);
      update();
    };
    update();
    window.addEventListener(changedEvent, update);
    window.addEventListener("storage", storage);
    return () => { window.removeEventListener(changedEvent, update); window.removeEventListener("storage", storage); };
  }, [key]);
  return { decision: selection.key === key ? selection.decision : null, persistent,
    choose: allowed => { setPersistent(saveMapConsent(key, allowed)); setSelection({ key, decision: allowed }); } };
}
