/** Shared navigation in content units; screen coordinates are CSS pixels. */
export type NavigationPoint = { x: number; y: number };
export type NavigationSize = { width: number; height: number };
export type NavigationView = { center: NavigationPoint; scale: number };
export type NavigationBounds = NavigationPoint & NavigationSize;

export function rotateNavigationDelta(point: NavigationPoint, rotation = 0): NavigationPoint {
  const angle = ((rotation % 360) + 360) % 360;
  if (angle === 90) return { x: -point.y, y: point.x };
  if (angle === 180) return { x: -point.x, y: -point.y };
  if (angle === 270) return { x: point.y, y: -point.x };
  return point;
}

export function navigationToContent(point: NavigationPoint, view: NavigationView, size: NavigationSize, rotation = 0): NavigationPoint {
  const delta = rotateNavigationDelta({ x: point.x - size.width / 2, y: point.y - size.height / 2 }, -rotation);
  return { x: view.center.x + delta.x / view.scale, y: view.center.y + delta.y / view.scale };
}

export function navigationToScreen(point: NavigationPoint, view: NavigationView, size: NavigationSize, rotation = 0): NavigationPoint {
  const delta = rotateNavigationDelta({ x: (point.x - view.center.x) * view.scale, y: (point.y - view.center.y) * view.scale }, rotation);
  return { x: size.width / 2 + delta.x, y: size.height / 2 + delta.y };
}

export function panNavigation(view: NavigationView, delta: NavigationPoint, rotation = 0): NavigationView {
  const contentDelta = rotateNavigationDelta(delta, -rotation);
  return { scale: view.scale, center: { x: view.center.x - contentDelta.x / view.scale, y: view.center.y - contentDelta.y / view.scale } };
}

export function zoomNavigation(view: NavigationView, scale: number, anchor: NavigationPoint, size: NavigationSize, rotation = 0): NavigationView {
  const content = navigationToContent(anchor, view, size, rotation);
  const delta = rotateNavigationDelta({ x: anchor.x - size.width / 2, y: anchor.y - size.height / 2 }, -rotation);
  return { scale, center: { x: content.x - delta.x / scale, y: content.y - delta.y / scale } };
}

export function wheelNavigationScale(scale: number, deltaY: number, deltaMode: number, height: number, minimum: number, maximum: number): number {
  const pixels = deltaY * (deltaMode === 1 ? 16 : deltaMode === 2 ? height : 1);
  return Math.max(minimum, Math.min(maximum, scale * Math.pow(2, -pixels / 420)));
}

export function fitNavigation(bounds: NavigationBounds, size: NavigationSize): NavigationView {
  return {
    center: { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 },
    scale: Math.min(size.width / Math.max(bounds.width, 0.001), size.height / Math.max(bounds.height, 0.001)),
  };
}
