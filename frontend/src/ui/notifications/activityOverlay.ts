import type { Notification, Notification2DContext, Notification2DOverlay, Notification3DOverlay, Scene3DContext } from "@toposync/plugin-api";
import { activityIdentifier, type ActivityImage } from "../../util/activity";
import { imageAvailable, imageHoldDuration, playback, sameImageScope, subscribePlayback } from "./activityPlayback";

export function spatialImage(image: ActivityImage, composition?: string): boolean {
  return !!composition && image.coordinate_reference === composition && !!image.mapping_revision
    && image.position?.length === 2 && image.position.every(Number.isFinite) && imageAvailable(image);
}
function selected(notification: Notification, composition?: string) {
  const value = playback();
  if (!value || value.occurrence !== activityIdentifier(notification) || !spatialImage(value.image, composition)) return null;
  const samples = [value, ...(value.companions || [])].filter(sample => spatialImage(sample.image, composition));
  return { samples, trails: samples.flatMap(sample => imageTrails(sample.trail, sample.image)) };
}
export function imageTrails(images: ActivityImage[], reference?: ActivityImage): Array<Array<{ x: number; z: number }>> {
  const trails: Array<Array<{ x: number; z: number }>> = [];
  let previous: ActivityImage | null = null;
  for (const image of images) {
    if (image.position?.length !== 2 || !image.position.every(Number.isFinite) || !imageAvailable(image)
      || (reference && !sameImageScope(reference, image))) { previous = null; continue; }
    if (!previous || image.gap || image.selection_gap || !sameImageScope(previous, image) || image.observed_at - previous.observed_at > imageHoldDuration(image)) trails.push([]);
    trails[trails.length - 1].push({ x: image.position[0], z: image.position[1] });
    previous = image;
  }
  return trails;
}
export function createActivity2DOverlay(ctx: Notification2DContext, notification: Notification): Notification2DOverlay {
  const dispose = subscribePlayback(() => ctx.requestRender?.());
  return { dispose, pin: () => {
    const value = selected(notification, ctx.compositionId);
    if (!value) return null;
    const pins = value.samples.map(sample => ({ x: sample.image.position![0], z: sample.image.position![1],
      image: { url: sample.url, anchor: sample.image.anchor || [0.5, 1] as [number, number] } }));
    return { ...pins[0], trails: value.trails, companions: pins.slice(1) };
  } };
}
export function createActivity3DOverlay(ctx: Scene3DContext, notification: Notification): Notification3DOverlay {
  const { THREE } = ctx;
  const group = new THREE.Group();
  const sprites: import("three").Sprite[] = [];
  const clear = () => { for (const sprite of sprites) { sprite.material.map?.dispose(); sprite.material.dispose(); group.remove(sprite); } sprites.length = 0; };
  group.visible = false;
  const geometry = new THREE.BufferGeometry();
  const positions = new THREE.Float32BufferAttribute(new Float32Array(120 * 6), 3);
  geometry.setAttribute("position", positions); geometry.setDrawRange(0, 0);
  const lineMaterial = new THREE.LineBasicMaterial({ color: 0xf59e0b });
  const line = new THREE.LineSegments(geometry, lineMaterial);
  line.frustumCulled = false;
  group.add(line);
  let generation = 0;
  const update = () => {
    const revision = ++generation;
    group.visible = false;
    const value = selected(notification, ctx.compositionId);
    clear();
    if (value) {
      const points = value.trails.flatMap(segment => segment.slice(1).flatMap((point, index) => [
        segment[index].x, 0.01, segment[index].z, point.x, 0.01, point.z]));
      positions.array.set(points); positions.needsUpdate = true;
      geometry.setDrawRange(0, points.length / 3);
      for (const sample of value.samples) {
        const material = new THREE.SpriteMaterial({ transparent: true, depthWrite: false });
        const sprite = new THREE.Sprite(material); group.add(sprite); sprites.push(sprite); sprite.visible = false;
        const image = new Image();
        image.onload = () => {
          if (revision !== generation) return;
          const texture = new THREE.Texture(image); texture.colorSpace = THREE.SRGBColorSpace; texture.needsUpdate = true;
          material.map = texture; material.needsUpdate = true;
          sprite.center.set(sample.image.anchor?.[0] ?? 0.5, 1 - (sample.image.anchor?.[1] ?? 1));
          // Display size is symbolic; image pixels do not establish a person's world height.
          sprite.scale.set(1.4 * image.width / Math.max(1, image.height), 1.4, 1);
          sprite.position.set(sample.image.position![0], 0.02, sample.image.position![1]);
          sprite.visible = true; group.visible = true; ctx.requestRender?.();
        };
        image.src = sample.url;
      }
    }
    ctx.requestRender?.();
  };
  const unsubscribe = subscribePlayback(update); update();
  return { object: group, bounds: () => {
    const value = selected(notification, ctx.compositionId);
    if (!value) return null;
    const points = value.trails.flat();
    if (!points.length) return null;
    return { minX: Math.min(...points.map(point => point.x)), maxX: Math.max(...points.map(point => point.x)),
      minZ: Math.min(...points.map(point => point.z)), maxZ: Math.max(...points.map(point => point.z)) };
  }, dispose: () => { generation++; unsubscribe(); clear(); geometry.dispose(); lineMaterial.dispose(); } };
}
