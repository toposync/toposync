import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { ElementType, Notification, NotificationRenderer } from "@toposync/plugin-api";
import { getGeographicMapConfiguration, type Composition, type GeographicMapConfiguration } from "../../util/api";
import { geographicToLocal, localToGeographic, longitudeNear, readGeoreference } from "../../util/georeference";
import { i18n } from "../../util/i18n";
import { computeMain2DBounds } from "../main2d/shared";
import { Modal } from "../Modal";
import { GeographicCompositionLayer, type GeographicTarget } from "./GeographicCompositionLayer";
import { MapNavigation } from "./MapNavigation";
import { useMapConsent } from "./mapConsent";
import { geographicViewport, projectGeographicPoint } from "./projection";

export function GeographicMap({ compositions, activeCompositionId, elementTypesById, notifications, notificationRenderers, activeNotificationId, onAnchorChange, onActivate, onNotification, onImage }: {
  compositions: Composition[]; activeCompositionId: string; elementTypesById: Record<string, ElementType>;
  notifications: Notification[]; notificationRenderers: NotificationRenderer[]; activeNotificationId?: string | null;
  onAnchorChange?: (point: { latitude: number; longitude: number }) => void;
  onActivate?: (target: GeographicTarget) => void; onNotification?: (id: string) => void;
  onImage?: (args: { url: string; title?: string; subtitle?: string }) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<L.Map | null>(null);
  const [configuration, setConfiguration] = useState<GeographicMapConfiguration | null>(null);
  const [error, setError] = useState("");
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [targets, setTargets] = useState<GeographicTarget[]>([]);
  const anchorChange = useRef(onAnchorChange);
  anchorChange.current = onAnchorChange;
  const consent = useMapConsent(configuration ? `background:${configuration.background.id}:${configuration.background.url}` : "unconfigured");
  const positioned = useMemo(() => compositions.flatMap(composition => {
    const reference = readGeoreference(composition.meta?.georeference);
    return reference ? [{ composition, reference }] : [];
  }), [compositions]);
  useEffect(() => {
    const controller = new AbortController();
    getGeographicMapConfiguration(controller.signal).then(setConfiguration).catch(() => { if (!controller.signal.aborted) setError("core.map.configuration_failed"); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!container.current) return;
    const reference = positioned.find(item => item.composition.id === activeCompositionId)?.reference ?? positioned[0]?.reference;
    const instance = L.map(container.current, { maxZoom: 22, minZoom: 1, zoomAnimation: false, markerZoomAnimation: false })
      .setView(reference ? [reference.latitude, reference.longitude] : [0, 0], reference ? 19 : 2);
    instance.zoomControl.setPosition("bottomleft");
    L.control.scale({ imperial: false, position: "bottomright" }).addTo(instance);
    setMap(instance);
    const observer = new ResizeObserver(() => instance.invalidateSize({ debounceMoveend: true }));
    observer.observe(container.current);
    return () => { observer.disconnect(); instance.remove(); };
  }, []);
  const fitCompositions = useCallback(() => {
    if (!map || !positioned.length) return;
    const bounds = L.latLngBounds([]);
    for (const { composition, reference } of positioned) {
      const local = computeMain2DBounds(composition.elements, elementTypesById);
      for (const point of [{ x: local.minX, z: local.minZ }, { x: local.maxX, z: local.maxZ }, { x: local.minX, z: local.maxZ }, { x: local.maxX, z: local.minZ }, { x: 0, z: 0 }]) {
        try {
          const location = localToGeographic(point, reference);
          bounds.extend([location.lat, longitudeNear(location.lng, positioned[0].reference.longitude)]);
        } catch { /* Out-of-domain elements are not fitted. */ }
      }
    }
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [48, 48], maxZoom: 20, animate: false });
  }, [map, positioned, elementTypesById]);
  const fit = useRef(fitCompositions);
  fit.current = fitCompositions;
  const positionedIds = positioned.map(item => item.composition.id).join("|");
  useEffect(() => { fit.current(); }, [map, positionedIds, activeCompositionId]);
  useEffect(() => {
    if (!map || !configuration || consent.decision !== true) return;
    const background = configuration.background;
    const layer = L.tileLayer(background.url, { attribution: background.attribution, maxNativeZoom: background.nativeZoom, maxZoom: 22, updateWhenIdle: true, keepBuffer: 0 });
    layer.on("tileerror", () => setError("core.map.offline"));
    layer.addTo(map);
    return () => { layer.remove(); layer.off(); };
  }, [map, configuration, consent.decision]);
  useEffect(() => {
    if (!map || !onAnchorChange) return;
    const reference = positioned[0]?.reference;
    if (!reference) return;
    const marker = L.marker([reference.latitude, reference.longitude], { draggable: true, keyboard: true, title: t("core.map.drag"),
      icon: L.divIcon({ html: "+", className: "geographicAnchor", iconSize: [44, 44], iconAnchor: [22, 22] }) }).addTo(map);
    marker.on("dragend", () => { const point = marker.getLatLng().wrap(); anchorChange.current?.({ latitude: Math.max(-85, Math.min(85, point.lat)), longitude: point.lng }); });
    return () => { marker.remove(); marker.off(); };
  }, [map, positioned, Boolean(onAnchorChange), t]);
  const activate = useCallback((target: GeographicTarget) => {
    if (!map || !onActivate) return;
    const owner = positioned.find(item => item.composition.id === target.compositionId);
    const element = owner?.composition.elements.find(item => item.id === target.elementId);
    if (!owner || !element) return;
    const origin = projectGeographicPoint(map, owner.reference, elementTypesById[element.type]?.getMain2DMarker?.({ element }) ?? element.position);
    const nearby: GeographicTarget[] = [];
    for (const { composition, reference } of positioned) for (const candidate of composition.elements) {
      if (!elementTypesById[candidate.type]) continue;
      try {
        const point = projectGeographicPoint(map, reference, elementTypesById[candidate.type]?.getMain2DMarker?.({ element: candidate }) ?? candidate.position);
        if (point.distanceTo(origin) <= 44) nearby.push({ compositionId: composition.id, elementId: candidate.id });
      } catch { /* Invalid element positions cannot be selected. */ }
    }
    if (nearby.length > 1) setTargets(nearby); else onActivate(target);
  }, [map, positioned, onActivate, elementTypesById]);
  useEffect(() => {
    if (!map) return;
    const click = (event: L.LeafletMouseEvent) => {
      if (anchorChange.current) { const point = event.latlng.wrap(); anchorChange.current({ latitude: Math.max(-85, Math.min(85, point.lat)), longitude: point.lng }); return; }
      if (!onActivate) return;
      const selected: GeographicTarget[] = [];
      for (const { composition, reference } of positioned) {
        try {
          const world = geographicToLocal({ lat: event.latlng.lat, lng: event.latlng.wrap().lng }, reference);
          const viewport = geographicViewport(map, reference, document.createElement("canvas"));
          for (const element of [...composition.elements].reverse())
            if (elementTypesById[element.type]?.hitTest2D?.({ element, world, viewport })) selected.push({ compositionId: composition.id, elementId: element.id });
        } catch { /* Clicks outside a composition's domain do not target it. */ }
      }
      if (selected.length > 1) setTargets(selected); else if (selected[0]) onActivate(selected[0]);
    };
    map.on("click", click);
    return () => { map.off("click", click); };
  }, [map, positioned, elementTypesById, onActivate]);
  return <div className="geographicMapRoot" data-testid="geographic-map">
    {configuration && <div className="geographicToolbar">
      {(consent.decision === null || permissionOpen) ? <div className="geographicPermission" role="alert">
        <p>{t("core.map.background_consent", { provider: configuration.background.name })}</p>
        <button type="button" className="primaryButton" onClick={() => { consent.choose(true); setPermissionOpen(false); setError(""); }}>{t("core.map.allow_background")}</button>
        <button type="button" className="chipButton" onClick={() => { consent.choose(false); setPermissionOpen(false); }}>{t("core.map.without_background")}</button>
      </div> : <button type="button" className="geographicServiceButton" onClick={() => {
        if (consent.decision) consent.choose(false); else setPermissionOpen(true);
      }}>{t(consent.decision ? "core.map.revoke_background" : "core.map.allow_background")}</button>}
      {map && <MapNavigation map={map} configuration={configuration} onHome={fitCompositions} />}
      {!consent.persistent && <p role="status">{t("core.map.permission_session")}</p>}
    </div>}
    {error && <p className="geographicError" role="alert">{t(error)}</p>}
    <div className="geographicMapStage">
      <div ref={container} className="geographicLeaflet" aria-label={t("core.map.aria")} />
      {map && positioned.map(({ composition, reference }) => <GeographicCompositionLayer key={composition.id} map={map} composition={composition} reference={reference}
        elementTypesById={elementTypesById} notifications={notifications} notificationRenderers={notificationRenderers} activeNotificationId={activeNotificationId}
        selected={composition.id === activeCompositionId} onActivate={onActivate ? activate : undefined} onNotification={onNotification} onImage={onImage} />)}
      {!positioned.length && <p className="geographicEmpty" role="status">{t("core.map.unpositioned")}</p>}
    </div>
    <Modal open={targets.length > 0} title={t("core.map.choose_element")} onClose={() => setTargets([])}>
      <div className="geographicChoices">{targets.map(target => {
        const owner = compositions.find(item => item.id === target.compositionId);
        const element = owner?.elements.find(item => item.id === target.elementId);
        return <button className="chipButton" type="button" key={`${target.compositionId}:${target.elementId}`} onClick={() => { setTargets([]); onActivate?.(target); }}>
          {owner?.name} · {String(element?.props.name ?? element?.id ?? "")}
        </button>;
      })}</div>
    </Modal>
  </div>;
}
