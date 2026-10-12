import React, { useCallback, useEffect, useRef, useState } from "react";
import type * as L from "leaflet";
import type { ElementType, Main2DMarker, Notification, Notification2DOverlay, Notification2DPin, NotificationRenderer } from "@toposync/plugin-api";
import type { Composition } from "../../util/api";
import { localToGeographic, type Georeference } from "../../util/georeference";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
import { Notification2DPinView } from "../notifications/Notification2DPinView";
import { orderElementsForMain2D } from "../main2d/shared";
import { geographicViewport, geographicTrail, projectGeographicPoint, projectLocation } from "./projection";

const GeographicNotificationPin = React.memo(Notification2DPinView);

export type GeographicTarget = { compositionId: string; elementId: string };

function ElementButton({ marker, onActivate }: { marker: Main2DMarker; onActivate: () => void }) {
  const pressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pressed = useRef(false);
  const start = useRef<{ x: number; y: number } | null>(null);
  const cancelPress = () => { if (pressTimer.current) clearTimeout(pressTimer.current); pressTimer.current = null; };
  useEffect(() => cancelPress, []);
  return <button type="button" className={`geographicElementButton ${marker.state ? `is-${marker.state}` : ""}`} aria-label={marker.title} title={[marker.title, marker.subtitle].filter(Boolean).join(" · ")}
    onPointerDown={event => {
      pressed.current = false; start.current = { x: event.clientX, y: event.clientY };
      pressTimer.current = setTimeout(() => { pressed.current = true; onActivate(); }, 550);
    }}
    onPointerMove={event => { if (start.current && Math.hypot(event.clientX - start.current.x, event.clientY - start.current.y) > 8) { pressed.current = true; cancelPress(); } }}
    onPointerUp={cancelPress} onPointerCancel={() => { pressed.current = true; cancelPress(); }} onPointerLeave={cancelPress}
    onClick={event => { if (event.detail === 0 || !pressed.current) onActivate(); }}>
    <Icon name={marker.icon || "circle-dot"} />
    <span className="geographicElementLabel">{marker.title}</span>
  </button>;
}

type ScreenMarker = Main2DMarker & { elementId: string; screenX: number; screenY: number };
type ScreenPin = Notification2DPin & { notification: Notification; screenX: number; screenY: number; segments: Array<Array<{ x: number; y: number }>> };

export function GeographicCompositionLayer({ map, composition, reference, elementTypesById, notifications, notificationRenderers, activeNotificationId, selected, onActivate, onNotification, onImage }: {
  map: L.Map; composition: Composition; reference: Georeference; elementTypesById: Record<string, ElementType>;
  notifications: Notification[]; notificationRenderers: NotificationRenderer[]; activeNotificationId?: string | null; selected: boolean;
  onActivate?: (target: GeographicTarget) => void; onNotification?: (id: string) => void;
  onImage?: (args: { url: string; title?: string; subtitle?: string }) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const canvas = useRef<HTMLCanvasElement>(null);
  const overlays = useRef(new Map<string, { overlay: Notification2DOverlay; notification: Notification; renderer: NotificationRenderer }>());
  const projected = useRef(new Map<string, { reference: Georeference; x: number; z: number; worldTrail: ReadonlyArray<{ x: number; z: number }>;
    position: { lat: number; lng: number }; trail: Array<Array<{ lat: number; lng: number }>> }>());
  const [markers, setMarkers] = useState<ScreenMarker[]>([]);
  const [pins, setPins] = useState<ScreenPin[]>([]);
  const [version, setVersion] = useState(0);
  const invalidate = useCallback(() => setVersion(value => value + 1), []);
  useEffect(() => {
    for (const item of overlays.current.values()) item.overlay.dispose?.();
    overlays.current.clear(); projected.current.clear();
    return () => { for (const item of overlays.current.values()) item.overlay.dispose?.(); overlays.current.clear(); projected.current.clear(); };
  }, [composition.id, composition.elements, notificationRenderers]);
  useEffect(() => {
    const visible = new Set(notifications.map(item => item.id));
    for (const [id, item] of overlays.current) if (!visible.has(id)) { item.overlay.dispose?.(); overlays.current.delete(id); projected.current.delete(id); }
    for (const notification of notifications) {
      const renderer = notificationRenderers.find(item => item.type === notification.type);
      if (!renderer?.create2DOverlay) continue;
      const current = overlays.current.get(notification.id);
      try {
        if (current?.renderer === renderer) {
          if (current.notification !== notification) {
            if (current.overlay.update) { current.overlay.update(notification); current.notification = notification; continue; }
            current.overlay.dispose?.(); overlays.current.delete(notification.id);
          } else continue;
        } else if (current) {
          current.overlay.dispose?.(); overlays.current.delete(notification.id); projected.current.delete(notification.id);
        }
        const overlay = renderer.create2DOverlay({ compositionId: composition.id, elements: composition.elements, requestRender: invalidate }, notification, { openImage: args => onImage?.(args) });
        if (overlay) overlays.current.set(notification.id, { overlay, notification, renderer });
      } catch (error) {
        current?.overlay.dispose?.(); overlays.current.delete(notification.id); projected.current.delete(notification.id);
        console.warn("[geographic:notification]", error);
      }
    }
    invalidate();
  }, [notifications, notificationRenderers, composition.id, composition.elements, invalidate, onImage]);
  useEffect(() => {
    const unsubscribers: Array<() => void> = [];
    for (const element of composition.elements) {
      try {
        const unsubscribe = elementTypesById[element.type]?.subscribeMain2DState?.({ element, invalidate });
        if (unsubscribe) unsubscribers.push(unsubscribe);
      } catch (error) { console.warn("[geographic:subscription]", element.id, error); }
    }
    return () => unsubscribers.forEach(unsubscribe => unsubscribe());
  }, [composition.elements, elementTypesById, invalidate]);
  useEffect(() => {
    const output = canvas.current;
    if (!output) return;
    let frame = 0;
    function draw() {
      frame = 0;
      const context = output!.getContext("2d");
      if (!context) return;
      const { x: width, y: height } = map.getSize();
      const dpr = window.devicePixelRatio || 1;
      output!.width = width * dpr; output!.height = height * dpr;
      output!.style.width = `${width}px`; output!.style.height = `${height}px`;
      const viewport = geographicViewport(map, reference, output!);
      const origin = projectGeographicPoint(map, reference, { x: 0, z: 0 });
      context.scale(dpr, dpr); context.translate(origin.x, origin.y); context.rotate(reference.rotationDegrees * Math.PI / 180);
      context.globalAlpha = selected ? 1 : 0.55;
      const nextMarkers: ScreenMarker[] = [];
      for (const element of orderElementsForMain2D(composition.elements, elementTypesById)) {
        const definition = elementTypesById[element.type];
        context.save();
        try {
          if (definition?.render2D) definition.render2D({ ctx: context, element, elements: composition.elements, viewport });
          else {
            const point = viewport.worldToScreen(element.position);
            context.fillStyle = "#1673c9"; context.beginPath(); context.arc(point.x, point.y, 5, 0, Math.PI * 2); context.fill();
          }
          if (onActivate && definition) {
            const marker = definition.getMain2DMarker?.({ element }) ?? {
              x: element.position.x, z: element.position.z,
              title: element.name || (typeof definition.name === "string" ? definition.name : t(definition.name.key)),
            };
            const screen = projectGeographicPoint(map, reference, marker);
            if (screen.x >= -44 && screen.y >= -44 && screen.x <= width + 44 && screen.y <= height + 44)
              nextMarkers.push({ ...marker, elementId: element.id, screenX: screen.x, screenY: screen.y });
          }
        } catch (error) { console.warn("[geographic:element]", element.id, error); }
        finally { context.restore(); }
      }
      setMarkers(nextMarkers);
      const nextPins: ScreenPin[] = [];
      for (const [id, { overlay, notification }] of overlays.current) {
        try {
          const pin = overlay.pin();
          if (!pin) { projected.current.delete(id); continue; }
          const worldTrail = pin.trail ?? [];
          let cached = projected.current.get(id);
          if (!cached || cached.reference !== reference || cached.x !== pin.x || cached.z !== pin.z
            || cached.worldTrail.length !== worldTrail.length || worldTrail.some((point, index) => point.x !== cached!.worldTrail[index].x || point.z !== cached!.worldTrail[index].z)) {
            cached = { reference, x: pin.x, z: pin.z, worldTrail: worldTrail.map(point => ({ x: point.x, z: point.z })),
              position: localToGeographic(pin, reference), trail: geographicTrail(reference, worldTrail) };
            projected.current.set(id, cached);
          }
          const screen = projectLocation(map, cached.position);
          nextPins.push({ ...pin, notification, screenX: screen.x, screenY: screen.y, segments: cached.trail.map(segment => segment.map(point => projectLocation(map, point))) });
        } catch { /* Invalid positions never acquire a geographic marker. */ }
      }
      setPins(nextPins);
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(draw); };
    schedule(); map.on("move zoom resize", schedule); output.addEventListener("toposync:invalidate", schedule);
    return () => { cancelAnimationFrame(frame); map.off("move zoom resize", schedule); output.removeEventListener("toposync:invalidate", schedule); };
  }, [map, composition, reference, elementTypesById, selected, onActivate, version, t]);
  return <div className={`geographicCompositionLayer${selected ? " isSelected" : ""}`} data-composition-id={composition.id}>
    <canvas ref={canvas} aria-hidden="true" />
    {markers.map(marker => <div key={marker.elementId} className="geographicMarker" style={{ left: marker.screenX, top: marker.screenY }} data-element-id={marker.elementId}>
      <ElementButton marker={marker} onActivate={() => onActivate?.({ compositionId: composition.id, elementId: marker.elementId })} />
    </div>)}
    {pins.map(pin => <React.Fragment key={pin.notification.id}>
      {pin.segments.map((trail, index) => <svg className="notification2dTrail" key={index} aria-hidden="true"><polyline fill="none" stroke={pin.priority === "high" ? "#ef4444" : "#f59e0b"} strokeWidth="3" points={trail.map(point => `${point.x},${point.y}`).join(" ")} /></svg>)}
      <div className={`geographicPinPosition${activeNotificationId === pin.notification.id && !pin.closed ? " isActive" : ""}`} style={{ left: pin.screenX, top: pin.screenY }}>
        <GeographicNotificationPin screenX={0} screenY={0} priority={pin.priority} closed={pin.closed} />
      </div>
      {onNotification && <button className={`geographicNotificationTarget${activeNotificationId === pin.notification.id ? " isSelected" : ""}`} type="button"
        style={{ left: pin.screenX, top: pin.screenY }} aria-label={pin.notification.title} title={pin.notification.title}
        data-notification-id={pin.notification.id} onClick={() => onNotification(pin.notification.id)} />}
    </React.Fragment>)}
  </div>;
}
