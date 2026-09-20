import type { Notification } from "@toposync/plugin-api";
import type { HumanObservation } from "./humanObservation";

type ImageDescriptor = NonNullable<Notification["ephemeralImage"]>;
type Point = { name: string; position: [number, number]; visibility: string };
export type HumanImageFrame = { image: ImageDescriptor; key: string; points: Point[]; expiresAt: number };
const record = (value: unknown): Record<string, any> => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const captureKey = (value: unknown) => {
  const capture = record(value);
  return JSON.stringify([capture.capture_instance, capture.generation, capture.sequence,
    capture.published_at, capture.physical_timestamp_verified]);
};

/** The host validates transient bytes. The extension additionally binds pose coordinates. */
export function readHumanObservationImage(notification: Notification, model: HumanObservation, now: number): HumanImageFrame | null {
  const image = notification.ephemeralImage;
  if (!image || model.state !== "current" || model.expiresAt === null || !Number.isFinite(now)
    || now >= Math.min(model.expiresAt, image.expiresAt)) return null;
  const payload = record(notification.payload), data = record(payload.data), vision = record(data.vision);
  if (image.schemaVersion !== 1 || image.cameraId !== model.camera || image.cameraId !== data.camera_id
    || image.sourceStreamId !== data.source_stream_id || image.packetId !== payload.packet_id
    || (image.parentPacketId ?? null) !== (payload.parent_packet_id ?? null)
    || (vision.pose_frame_packet_id !== image.packetId && vision.pose_frame_packet_id !== image.parentPacketId)
    || image.mediaTimestamp !== model.timestamp || captureKey(image.captureEvidence) !== captureKey(data.capture_evidence)
    || now < image.captureEvidence.published_at * 1000
    || !Number.isFinite(image.expiresAt) || image.expiresAt > image.captureEvidence.published_at * 1000 + 750
    || !["image/jpeg", "image/png", "image/webp"].includes(image.mimeType)
    || typeof image.dataBase64 !== "string" || !image.dataBase64.length || image.dataBase64.length > 349528
    || !Number.isSafeInteger(image.width) || !Number.isSafeInteger(image.height)
    || image.width < 2 || image.height < 2 || image.width * image.height > 2097152) return null;
  const geometry = record(image.imageGeometry);
  if (captureKey(geometry.capture_evidence) !== captureKey(image.captureEvidence)
    || JSON.stringify(geometry.image_size) !== JSON.stringify([image.width, image.height])
    || JSON.stringify(geometry.source_size) !== JSON.stringify([image.width, image.height])
    || JSON.stringify(geometry.to_source) !== JSON.stringify([[1, 0, 0], [0, 1, 0], [0, 0, 1]])) return null;
  const poses = Array.isArray(vision.poses) ? vision.poses.filter((pose: unknown) => record(pose).actor_subject_id === model.actor) : [];
  const pose = record(poses[0]);
  if (poses.length !== 1 || pose.schema_version !== 1 || pose.landmark_units !== "image_fraction"
    || pose.landmark_reference !== "stream_image" || !Array.isArray(pose.landmarks) || pose.landmarks.length > 128) return null;
  const points: Point[] = [], names = new Set<string>();
  for (const raw of pose.landmarks) {
    const point = record(raw);
    if (typeof point.name !== "string" || !point.name || names.has(point.name)) return null;
    names.add(point.name);
    if (point.provenance !== "image_estimate" || point.invalid_reason || !Array.isArray(point.position)
      || point.position.length !== 2 || !point.position.every((value: unknown) => typeof value === "number" && Number.isFinite(value))) continue;
    points.push({ name: point.name, position: [...point.position] as [number, number], visibility: String(point.visibility || "unknown") });
  }
  if (!points.length) return null;
  return { image, points, expiresAt: Math.min(model.expiresAt, image.expiresAt),
    key: JSON.stringify([image.packetId, image.parentPacketId, model.scopeKey, captureKey(image.captureEvidence)]) };
}

export function humanImagePrimitives(frame: HumanImageFrame) {
  const inside = frame.points.filter((point) => point.position.every((coordinate) => coordinate >= 0 && coordinate <= 1)
    && point.visibility !== "outside_image");
  const byName = new Map(inside.map((point) => [point.name, point]));
  const edges = [
    ["left_shoulder", "right_shoulder"], ["left_shoulder", "left_hip"], ["right_shoulder", "right_hip"], ["left_hip", "right_hip"],
    ...["left", "right"].flatMap((side) => [[`${side}_shoulder`, `${side}_elbow`], [`${side}_elbow`, `${side}_wrist`],
      [`${side}_hip`, `${side}_knee`], [`${side}_knee`, `${side}_ankle`], [`${side}_ankle`, `${side}_heel`],
      [`${side}_heel`, `${side}_foot_index`], [`${side}_ankle`, `${side}_foot_index`],
      // Halpe26 keeps its own toe names and ankle-to-toe skeleton edges.
      [`${side}_ankle`, `${side}_big_toe`], [`${side}_ankle`, `${side}_small_toe`]]),
  ];
  return { points: inside, outside: frame.points.length - inside.length,
    segments: edges.flatMap(([start, end]) => {
      const a = byName.get(start), b = byName.get(end);
      return a && b ? [{ start: a, end: b }] : [];
    }) };
}
