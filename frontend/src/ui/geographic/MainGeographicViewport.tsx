import React, { useEffect, useMemo, useRef, useState } from "react";
import type { CompositionElement, ElementType, Notification, NotificationRenderer } from "@toposync/plugin-api";
import { getGeographicCompositions, type Composition } from "../../util/api";
import { i18n } from "../../util/i18n";
import { Modal } from "../Modal";
import { GeographicMap, type GeographicView } from "./GeographicMap";
import type { GeographicTarget } from "./GeographicCompositionLayer";

export function MainGeographicViewport({ viewState, compositionId, compositionName, georeference, elements, elementTypesById, notifications, notificationRenderers, activeNotificationId, onActivateComposition, onElementActivated, onNotification, onImage }: {
  viewState?: React.MutableRefObject<GeographicView | null>;
  compositionId: string; compositionName: string; georeference: unknown; elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>; notifications: Notification[]; notificationRenderers: NotificationRenderer[]; activeNotificationId?: string | null;
  onActivateComposition: (id: string) => Promise<Composition>;
  onElementActivated: (id: string, intent?: "click" | "dblclick" | "longpress") => void;
  onNotification: (id: string) => void; onImage: (args: { url: string; title?: string; subtitle?: string }) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const [loaded, setLoaded] = useState<Composition[]>([]);
  const [error, setError] = useState(false);
  const [activationError, setActivationError] = useState(false);
  const [pending, setPending] = useState<GeographicTarget | null>(null);
  const [activating, setActivating] = useState(false);
  const queued = useRef<GeographicTarget | null>(null);
  useEffect(() => {
    let controller: AbortController;
    const refresh = () => {
      controller?.abort();
      const current = new AbortController(); controller = current;
      return getGeographicCompositions(current.signal).then(items => { if (!current.signal.aborted) { setLoaded(items); setError(false); } })
        .catch(() => { if (!current.signal.aborted) setError(true); });
    };
    void refresh();
    const onFocus = () => { void refresh(); };
    window.addEventListener("focus", onFocus);
    return () => { controller.abort(); window.removeEventListener("focus", onFocus); };
  }, [compositionId]);
  const compositions = useMemo(() => {
    const active: Composition = { id: compositionId, name: compositionName, elements, meta: { georeference } };
    return loaded.some(item => item.id === compositionId) ? loaded.map(item => item.id === compositionId ? active : item) : [...loaded, active];
  }, [loaded, compositionId, compositionName, elements, georeference]);
  useEffect(() => {
    const target = queued.current;
    if (target?.compositionId !== compositionId || !elements.some(item => item.id === target.elementId)) return;
    queued.current = null;
    onElementActivated(target.elementId, "longpress");
  }, [compositionId, elements, onElementActivated]);
  return <div className="viewportRoot geographicMainViewport">
    {activationError && !pending && <p role="alert">{t("core.map.activation_failed")}</p>}
    {error && <p role="alert">{t("core.map.compositions_failed")}</p>}
    <GeographicMap viewState={viewState} compositions={compositions} activeCompositionId={compositionId} elementTypesById={elementTypesById} notifications={notifications}
      notificationRenderers={notificationRenderers} activeNotificationId={activeNotificationId} onNotification={onNotification} onImage={onImage}
      onActivate={target => {
        if (target.compositionId === compositionId) onElementActivated(target.elementId, "longpress");
        else { setActivationError(false); setPending(target); }
      }} />
    <Modal open={pending !== null} title={t("core.map.other_composition")} onClose={() => { if (!activating) setPending(null); }}>
      <p>{t("core.map.switch_context", { name: compositions.find(item => item.id === pending?.compositionId)?.name ?? "" })}</p>
      {activationError && <p role="alert">{t("core.map.activation_failed")}</p>}
      <button className="primaryButton" type="button" disabled={activating} onClick={async () => {
        if (!pending) return;
        const target = pending;
        setActivating(true); setActivationError(false); queued.current = target;
        try { await onActivateComposition(target.compositionId); setPending(null); }
        catch { queued.current = null; setActivationError(true); }
        finally { setActivating(false); }
      }}>{t("core.map.open_controls")}</button>
    </Modal>
  </div>;
}
