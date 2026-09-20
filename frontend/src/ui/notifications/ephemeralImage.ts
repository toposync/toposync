import type { Notification, NotificationEphemeralImage } from "@toposync/plugin-api";

const MAXIMUM_BYTES = 256 * 1024;
const MAXIMUM_PIXELS = 2_097_152;
const MAXIMUM_AGE_MS = 750;
const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const identifier = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value.length <= 512;
const integer = (value: unknown, minimum: number): value is number => finite(value) && Number.isSafeInteger(value) && value >= minimum;

/** Fail closed beyond 16 KiB, depth 8 or 1024 nodes of raw provenance.
 * This client bound is intentionally stricter than the backend metadata bound.
 * Never serialize image pixels here.
 */
function provenance(value: unknown): string {
  let nodes = 0;
  const walk = (item: unknown, depth: number): unknown => {
    if (++nodes > 1024 || depth > 8) throw new Error("Oversized image provenance");
    if (item === null || typeof item === "boolean" || typeof item === "string" || finite(item)) return item;
    if (Array.isArray(item)) return item.map((entry) => walk(entry, depth + 1));
    const object = record(item);
    if (!object) throw new Error("Invalid image provenance");
    return Object.fromEntries(Object.keys(object).sort().map((key) => [key, walk(object[key], depth + 1)]));
  };
  const encoded = JSON.stringify(walk(value, 0));
  if (encoded.length > 16384) throw new Error("Oversized image provenance");
  return encoded;
}

/** Check encoded dimensions before handing bytes to a browser image decoder. */
function encodedSize(bytes: string, mime: string): [number, number] | null {
  const byte = (offset: number) => bytes.charCodeAt(offset);
  const big16 = (offset: number) => byte(offset) * 256 + byte(offset + 1);
  const big32 = (offset: number) => big16(offset) * 65536 + big16(offset + 2);
  const little24 = (offset: number) => byte(offset) + byte(offset + 1) * 256 + byte(offset + 2) * 65536;
  if (mime === "image/png" && bytes.length >= 33 && bytes.slice(0, 8) === "\x89PNG\r\n\x1a\n"
    && bytes.slice(12, 16) === "IHDR" && big32(8) === 13) return [big32(16), big32(20)];
  if (mime === "image/jpeg" && bytes.slice(0, 2) === "\xff\xd8") {
    let offset = 2;
    while (offset + 3 < bytes.length) {
      if (byte(offset++) !== 255) return null;
      while (byte(offset) === 255) offset++;
      const marker = byte(offset++);
      if (marker === 0xda || marker === 0xd9) return null;
      if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
      const length = big16(offset);
      if (length < 2 || offset + length > bytes.length) return null;
      if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) {
        return length >= 8 ? [big16(offset + 5), big16(offset + 3)] : null;
      }
      offset += length;
    }
  }
  if (mime === "image/webp" && bytes.length >= 30 && bytes.slice(0, 4) === "RIFF" && bytes.slice(8, 12) === "WEBP") {
    const kind = bytes.slice(12, 16);
    if (kind === "VP8X") return [little24(24) + 1, little24(27) + 1];
    if (kind === "VP8 " && bytes.slice(23, 26) === "\x9d\x01\x2a")
      return [(byte(26) + byte(27) * 256) & 0x3fff, (byte(28) + byte(29) * 256) & 0x3fff];
    if (kind === "VP8L" && byte(20) === 0x2f)
      return [1 + byte(21) + ((byte(22) & 0x3f) << 8), 1 + (byte(22) >> 6) + (byte(23) << 2) + ((byte(24) & 0x0f) << 10)];
  }
  return null;
}

export function withoutEphemeralImage(notification: Notification): Notification {
  if (!Object.prototype.hasOwnProperty.call(notification, "ephemeralImage")) return notification;
  const { ephemeralImage: _discarded, ...persistable } = notification;
  return persistable;
}

function matchesPacket(notification: Notification, image: NotificationEphemeralImage): boolean {
  const payload = record(notification.payload);
  if (!payload || payload.realtime !== true || payload.status === "closed" || payload.lifecycle === "close"
    || payload.packet_id !== image.packetId || (payload.parent_packet_id ?? null) !== image.parentPacketId) return false;
  const data = record(payload.data);
  return !data || ((data.camera_id === undefined || data.camera_id === image.cameraId)
    && (data.source_stream_id === undefined || data.source_stream_id === image.sourceStreamId)
    && (data.capture_evidence === undefined || provenance(data.capture_evidence) === provenance(image.captureEvidence)));
}

function validate(notification: Notification, now: number): NotificationEphemeralImage | null {
  const value = record(notification.ephemeralImage);
  if (!value || value.schemaVersion !== 1 || !identifier(value.packetId)
    || (value.parentPacketId !== null && !identifier(value.parentPacketId))
    || !identifier(value.cameraId) || !identifier(value.sourceStreamId) || !identifier(value.artifactName)
    || !finite(value.mediaTimestamp) || !finite(value.expiresAt)
    || !integer(value.width, 2) || !integer(value.height, 2) || value.width * value.height > MAXIMUM_PIXELS
    || !["image/jpeg", "image/png", "image/webp"].includes(String(value.mimeType))) return null;
  const capture = record(value.captureEvidence), geometry = record(value.imageGeometry);
  if (!capture || !geometry || !identifier(capture.capture_instance) || !integer(capture.generation, 0)
    || !integer(capture.sequence, 1) || !finite(capture.published_at) || capture.published_at <= 0
    || (capture.physical_timestamp_verified !== undefined && typeof capture.physical_timestamp_verified !== "boolean")) return null;
  if (capture.published_at * 1000 > now || value.expiresAt <= now || value.expiresAt > capture.published_at * 1000 + MAXIMUM_AGE_MS) return null;
  if (provenance(capture) !== provenance(geometry.capture_evidence)) return null;
  if (!Array.isArray(geometry.image_size) || geometry.image_size.length !== 2
    || geometry.image_size[0] !== value.width || geometry.image_size[1] !== value.height
    || !Array.isArray(geometry.source_size) || geometry.source_size.length !== 2
    || !geometry.source_size.every((size) => integer(size, 2))) return null;
  const matrix = geometry.to_source;
  if (!Array.isArray(matrix) || matrix.length !== 3 || !matrix.every((row) => Array.isArray(row) && row.length === 3 && row.every(finite))) return null;
  const [[a, b, c], [d, e, f], [g, h, i]] = matrix as number[][];
  const determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
  if (!Number.isFinite(determinant) || Math.abs(determinant) < 1e-12) return null;
  if (typeof value.dataBase64 !== "string" || !value.dataBase64 || value.dataBase64.length > Math.ceil(MAXIMUM_BYTES / 3) * 4
    || value.dataBase64.length % 4 !== 0 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value.dataBase64)) return null;
  const bytes = atob(value.dataBase64);
  const size = encodedSize(bytes, value.mimeType as string);
  if (bytes.length > MAXIMUM_BYTES || !size || size[0] !== value.width || size[1] !== value.height) return null;
  const image = {
    schemaVersion: 1, packetId: value.packetId, parentPacketId: value.parentPacketId,
    cameraId: value.cameraId, sourceStreamId: value.sourceStreamId, artifactName: value.artifactName,
    mediaTimestamp: value.mediaTimestamp, captureEvidence: JSON.parse(provenance(capture)),
    imageGeometry: JSON.parse(provenance(geometry)), width: value.width, height: value.height,
    mimeType: value.mimeType, dataBase64: value.dataBase64, expiresAt: value.expiresAt,
  } as NotificationEphemeralImage;
  return matchesPacket(notification, image) ? image : null;
}

export type EphemeralImageLease = {
  notificationId: string;
  image: NotificationEphemeralImage;
  expiresMonotonic: number;
};

/** Only the selected detail may receive this value; inputs from history are stripped. */
export function attachEphemeralImage(notification: Notification | null, lease: EphemeralImageLease | null,
  now = Date.now(), monotonic = performance.now()): Notification | null {
  if (!notification) return null;
  const clean = withoutEphemeralImage(notification);
  try {
    return lease && lease.notificationId === clean.id && monotonic < lease.expiresMonotonic
      && now < lease.image.expiresAt && matchesPacket(clean, lease.image)
      ? { ...clean, ephemeralImage: lease.image } : clean;
  } catch { return clean; }
}

type Clock = {
  wallNow: () => number; monotonicNow: () => number;
  setTimer: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  clearTimer: (timer: ReturnType<typeof setTimeout>) => void;
};

/** One selected-stream lifetime. Clearing pixels retains a constant-size watermark. */
export function createEphemeralImageSession(notificationId: string,
  changed: (lease: EphemeralImageLease | null) => void,
  clock: Clock = { wallNow: Date.now, monotonicNow: () => performance.now(),
    setTimer: (callback, delay) => setTimeout(callback, delay), clearTimer: (timer) => clearTimeout(timer) }) {
  let lease: EphemeralImageLease | null = null, timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false, closed = false, maximumWall = -Infinity;
  let latest: { scope: string; epoch: string; instance: string; generation: number; sequence: number;
    publication: number; media: number; expiry: number } | null = null;
  const clear = () => {
    if (timer !== undefined) clock.clearTimer(timer);
    timer = undefined; lease = null; changed(null);
  };
  const observe = (notification: Notification) => {
    if (disposed || notification.id !== notificationId) return;
    const payload = record(notification.payload);
    if (payload?.lifecycle === "close" || payload?.status === "closed") { closed = true; clear(); }
  };
  return {
    clear, observe,
    receive: (notification: Notification) => {
      if (disposed || closed || notification.id !== notificationId) return;
      observe(notification);
      if (closed) return;
      const receivedMonotonic = clock.monotonicNow();
      maximumWall = Math.max(maximumWall, clock.wallNow());
      try {
        const image = validate(notification, maximumWall);
        if (!image) { clear(); return; }
        const capture = image.captureEvidence;
        const scope = JSON.stringify([image.cameraId, image.sourceStreamId]);
        const epoch = JSON.stringify([capture.capture_instance, capture.generation]);
        if (latest) {
          if (latest.scope !== scope || capture.published_at < latest.publication
            || (capture.capture_instance === latest.instance && capture.generation < latest.generation)) { clear(); return; }
          if (epoch === latest.epoch && capture.sequence === latest.sequence) {
            if (capture.published_at !== latest.publication || image.expiresAt !== latest.expiry) clear();
            return; // No pixels restored and no timer renewed, even after error/reconnect.
          }
          if ((epoch === latest.epoch && (capture.sequence < latest.sequence || image.mediaTimestamp <= latest.media))
            || (epoch !== latest.epoch && capture.published_at <= latest.publication)) { clear(); return; }
        }
        latest = { scope, epoch, instance: capture.capture_instance, generation: capture.generation,
          sequence: capture.sequence, publication: capture.published_at, media: image.mediaTimestamp, expiry: image.expiresAt };
        if (timer !== undefined) clock.clearTimer(timer);
        const expiresMonotonic = receivedMonotonic + image.expiresAt - maximumWall;
        const remaining = expiresMonotonic - clock.monotonicNow();
        if (remaining <= 0) { clear(); return; }
        lease = { notificationId, image, expiresMonotonic };
        timer = clock.setTimer(clear, remaining);
        changed(lease);
      } catch { clear(); }
    },
    dispose: () => { disposed = true; clear(); latest = null; },
  };
}
