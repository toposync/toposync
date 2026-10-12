import type * as L from "leaflet";
import { Vector2 } from "three";
import type { Viewport2DContext } from "@toposync/plugin-api";
import { geographicToLocal, localToGeographic, longitudeNear, type Georeference } from "../../util/georeference";

export function projectGeographicPoint(map: L.Map, reference: Georeference, point: { x: number; z: number }): Vector2 {
  const geographic = localToGeographic(point, reference);
  return projectLocation(map, geographic);
}

export function projectLocation(map: L.Map, geographic: { lat: number; lng: number }): Vector2 {
  if (![geographic.lat, geographic.lng].every(Number.isFinite) || Math.abs(geographic.lat) > 85.05112878)
    throw new Error("Point outside the geographic viewport");
  const screen = map.latLngToContainerPoint([geographic.lat, longitudeNear(geographic.lng, map.getCenter().lng)]);
  return new Vector2(screen.x, screen.y);
}

export function geographicViewport(map: L.Map, reference: Georeference, canvas: HTMLCanvasElement): Viewport2DContext {
  const origin = projectGeographicPoint(map, reference, { x: 0, z: 0 });
  const angle = reference.rotationDegrees * Math.PI / 180;
  const cos = Math.cos(angle), sin = Math.sin(angle);
  const east = localToGeographic({ x: 1, z: 0 }, reference);
  const scale = map.project([east.lat, longitudeNear(east.lng, reference.longitude)]).distanceTo(map.project([reference.latitude, reference.longitude]));
  return { canvas, width: map.getSize().x, height: map.getSize().y, dpr: window.devicePixelRatio || 1, scale,
    // Renderers draw in a rotated canvas; invert that rotation after exact geographic projection.
    worldToScreen: point => {
      const screen = projectGeographicPoint(map, reference, point).sub(origin);
      return new Vector2(screen.x * cos + screen.y * sin, -screen.x * sin + screen.y * cos);
    },
    screenToWorld: point => {
      const geographic = map.containerPointToLatLng([origin.x + point.x * cos - point.y * sin, origin.y + point.x * sin + point.y * cos]);
      return geographicToLocal({ lat: geographic.lat, lng: geographic.wrap().lng }, reference);
    },
  };
}

export function geographicTrail(reference: Georeference, trail: ReadonlyArray<{ x: number; z: number }>): Array<Array<{ lat: number; lng: number }>> {
  const segments: Array<Array<{ lat: number; lng: number }>> = [[]];
  for (const point of trail) {
    try { segments[segments.length - 1].push(localToGeographic(point, reference)); }
    catch { if (segments[segments.length - 1].length) segments.push([]); }
  }
  return segments.filter(segment => segment.length > 1);
}
