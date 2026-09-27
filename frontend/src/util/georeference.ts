/** Editor em metros no plano x/z: +x à direita, +z abaixo; y é elevação. */
export type Georeference = {
  version: 1;
  latitude: number;
  longitude: number;
  rotationDegrees: number;
  scale: number;
  axes: "x-east-z-south";
  units: "metres";
};
const radius = 6371008.8;
const radians = Math.PI / 180;
export function readGeoreference(value: unknown): Georeference | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Georeference;
  return v.version === 1 && v.axes === "x-east-z-south" && v.units === "metres"
    && [v.latitude, v.longitude, v.rotationDegrees, v.scale].every(Number.isFinite)
    && Math.abs(v.latitude) <= 85 && Math.abs(v.longitude) <= 180
    && v.scale >= 0.01 && v.scale <= 100 && Math.abs(v.rotationDegrees) <= 360 ? v : null;
}
function validate(reference: Georeference): void {
  if (!readGeoreference(reference)) throw new Error("Invalid geographic reference");
}
/** Projeção azimutal equidistante esférica: distância à origem em metros. */
export function localToGeographic(point: { x: number; z: number }, reference: Georeference): { lat: number; lng: number } {
  validate(reference);
  const distance = Math.hypot(point.x, point.z) * reference.scale;
  // ponytail: locais até 20 quilômetros; usar referencial topográfico para regiões maiores.
  if (!Number.isFinite(distance) || distance > 20000) throw new Error("Point outside the 20 km local map");
  const latitude = reference.latitude * radians;
  const longitude = reference.longitude * radians;
  const bearing = Math.atan2(point.x, -point.z) + reference.rotationDegrees * radians;
  const angle = distance / radius;
  const lat = Math.asin(Math.sin(latitude) * Math.cos(angle) + Math.cos(latitude) * Math.sin(angle) * Math.cos(bearing));
  const lng = longitude + Math.atan2(Math.sin(bearing) * Math.sin(angle) * Math.cos(latitude), Math.cos(angle) - Math.sin(latitude) * Math.sin(lat));
  return { lat: lat / radians, lng: ((lng / radians + 540) % 360) - 180 };
}
export function geographicToLocal(point: { lat: number; lng: number }, reference: Georeference): { x: number; z: number } {
  validate(reference);
  if (![point.lat, point.lng].every(Number.isFinite) || Math.abs(point.lat) > 90 || Math.abs(point.lng) > 180) throw new Error("Invalid coordinates");
  const latitude = reference.latitude * radians;
  const target = point.lat * radians;
  const delta = (point.lng - reference.longitude) * radians;
  const haversine = Math.sin((target - latitude) / 2) ** 2 + Math.cos(latitude) * Math.cos(target) * Math.sin(delta / 2) ** 2;
  const distance = 2 * radius * Math.asin(Math.sqrt(Math.min(1, Math.max(0, haversine))));
  if (distance > 20000.00001) throw new Error("Point outside the 20 km local map");
  const bearing = Math.atan2(Math.sin(delta) * Math.cos(target), Math.cos(latitude) * Math.sin(target) - Math.sin(latitude) * Math.cos(target) * Math.cos(delta)) - reference.rotationDegrees * radians;
  return { x: Math.sin(bearing) * distance / reference.scale, z: -Math.cos(bearing) * distance / reference.scale };
}
