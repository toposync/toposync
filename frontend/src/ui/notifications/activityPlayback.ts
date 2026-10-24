import type { ActivityImage } from "../../util/activity";

export type PlaybackSample = { image: ActivityImage; url: string; trail: ActivityImage[] };
export type Playback = PlaybackSample & { occurrence: string; companions?: PlaybackSample[] };
let current: Playback | null = null;
const listeners = new Set<() => void>();
export const playback = () => current;
export function publishPlayback(value: Playback | null): void { current = value; listeners.forEach(listener => listener()); }
export function subscribePlayback(listener: () => void): () => void { listeners.add(listener); return () => { listeners.delete(listener); }; }
export function sameImageSource(left: ActivityImage, right: ActivityImage): boolean {
  return ["camera_id", "source_id", "clock_domain", "capture_instance", "capture_generation", "coordinate_reference", "mapping_revision"]
    .every(key => left[key as keyof ActivityImage] === right[key as keyof ActivityImage]);
}
export function sameImageScope(left: ActivityImage, right: ActivityImage): boolean {
  return left.participant_id === right.participant_id && sameImageSource(left, right);
}
export function imageAvailable(image: ActivityImage): boolean {
  return image.available && image.expires_at * 1000 > Date.now();
}
export function imageHoldDuration(image: ActivityImage): number { return 1.5 / (image.rate || 1); }
export function imageAt(images: ActivityImage[], time: number): ActivityImage | null {
  const image = images.filter(item => item.observed_at <= time).at(-1);
  return image && time - image.observed_at <= imageHoldDuration(image) && imageAvailable(image) ? image : null;
}
