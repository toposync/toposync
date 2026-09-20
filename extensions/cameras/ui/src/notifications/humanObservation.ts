import type { Notification } from "@toposync/plugin-api";

export const HUMAN_OBSERVATION_TYPE = "com.toposync.cameras.human_observation";
export const HUMAN_OBSERVATION_MAX_AGE_MS = 750;
export const humanObservationPayloadPaths = [
  "subject", "camera_id", "source_stream_id", "capture_evidence", "vision.poses",
  "vision.pose_media_ts", "vision.pose_frame_packet_id", "vision.gestures",
  "spatial.person_ground", "spatial.pointing", "spatial.camera.status",
  "spatial.camera.frame_packet_id", "spatial.camera.camera_id", "spatial.camera.composition_id",
  "spatial.camera.physical_view_id", "spatial.camera.geometry.calibration_digest",
] as const;

type RecordValue = Record<string, unknown>;
export type Position = [number, number, number];
export type HumanAnchor = {
  position: Position | null;
  provenance: string;
  reason: string;
  contact?: string;
  radius?: number;
  envelope?: { minimum: Position; maximum: Position };
};
type GestureSideEvidence = { status: "available" | "unknown"; reason: string };
type GestureEvidence = {
  status: "active" | "none" | "unknown";
  reason: string;
  // null means a legacy payload did not report per-side evidence.
  side_evidence: { left: GestureSideEvidence; right: GestureSideEvidence } | null;
};
export type HumanObservation = {
  state: "current" | "closed" | "expired" | "unavailable";
  reason: string;
  actor: string | null;
  camera: string | null;
  composition: string | null;
  timestamp: number | null;
  expiresAt: number | null;
  captureKey: string | null;
  scopeKey: string | null;
  metricScopeKey: string | null;
  mapRevision: string | null;
  body: HumanAnchor;
  left: HumanAnchor;
  right: HumanAnchor;
  gestures: { name: string; side: string; evidence: "heuristic_2d"; candidate: boolean }[];
  gestureEvidence: GestureEvidence;
  pose2D: { name: string; position: [number, number] }[];
  pointing: {
    status: string;
    reason: string;
    selected: string | null;
    mapRevision: string | null;
    candidates: { id: string; distance: number | null; occlusion: string }[];
    ray: { origin: Position; direction: Position; length: number; halfAngle: number; radius: number } | null;
  };
};

const record = (value: unknown): RecordValue => value !== null && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const position = (value: unknown): Position | null => Array.isArray(value) && value.length === 3 && value.every((item) => finite(item) && Number.isFinite(Math.fround(item))) ? [...value] as Position : null;
const unavailableAnchor = (reason = "unavailable"): HumanAnchor => ({ position: null, provenance: "unavailable", reason });

function empty(state: HumanObservation["state"], reason: string): HumanObservation {
  return { state, reason, actor: null, camera: null, composition: null, timestamp: null, expiresAt: null,
    captureKey: null, scopeKey: null, metricScopeKey: null, mapRevision: null, body: unavailableAnchor(reason), left: unavailableAnchor(reason), right: unavailableAnchor(reason),
    gestures: [], gestureEvidence: { status: "unknown", reason, side_evidence: null },
    pose2D: [], pointing: { status: "unavailable", reason, selected: null, mapRevision: null, candidates: [], ray: null } };
}

function gestureSideEvidence(raw: unknown): GestureSideEvidence {
  const value = record(raw);
  if ((value.status === "available" || value.status === "unknown") && typeof value.reason === "string") {
    return { status: value.status, reason: value.reason };
  }
  return { status: "unknown", reason: "invalid_side_evidence" };
}

function nonfinite(value: unknown, depth = 0): boolean {
  if (depth > 24) return true;
  if (typeof value === "number") return !Number.isFinite(value);
  if (Array.isArray(value)) return value.some((item) => nonfinite(item, depth + 1));
  return value !== null && typeof value === "object" && Object.values(value).some((item) => nonfinite(item, depth + 1));
}

function anchor(raw: unknown, body: boolean, timestamp: number): HumanAnchor {
  const value = record(raw);
  if (value.status !== "estimated") return unavailableAnchor(text(value.reason) || "unavailable");
  const point = position(value.position);
  const provenance = text(value.provenance);
  if (!point || !(body ? provenance === "geometric_hypothesis" : ["image_estimate", "temporal_prediction"].includes(provenance))) {
    return unavailableAnchor("invalid_anchor_evidence");
  }
  const result: HumanAnchor = { position: point, provenance, reason: text(value.reason), contact: text(value.contact) };
  if (body) {
    const uncertainty = record(value.uncertainty);
    const minimum = position(uncertainty.minimum), maximum = position(uncertainty.maximum);
    if (uncertainty.kind !== "hypothesis_envelope" || uncertainty.calibrated !== false || !minimum || !maximum
      || minimum.some((coordinate, index) => coordinate > point[index] || maximum[index] < point[index]
        || !Number.isFinite(Math.fround(maximum[index] - coordinate)))) {
      return unavailableAnchor("invalid_hypothesis_envelope");
    }
    result.envelope = { minimum, maximum };
  } else {
    if (value.physical_contact_verified !== false || !finite(value.uncertainty_radius_meters) || value.uncertainty_radius_meters <= 0
      || !Number.isFinite(Math.fround(value.uncertainty_radius_meters))
      || !finite(value.valid_for_seconds) || value.valid_for_seconds <= 0) return unavailableAnchor("invalid_foot_evidence");
    result.radius = value.uncertainty_radius_meters;
    if (provenance === "temporal_prediction" && (!finite(value.age_seconds) || value.age_seconds < 0
      || !finite(value.image_evidence_timestamp) || Math.abs(timestamp - value.image_evidence_timestamp - value.age_seconds) > 0.001)) {
      return unavailableAnchor("invalid_temporal_foot_evidence");
    }
  }
  return result;
}

/** now is explicit in milliseconds for deterministic expiry tests. No update/receive timestamps are used. */
export function readHumanObservation(notification: Notification, now: number, compositionId?: string): HumanObservation {
  const payload = record(notification.payload);
  // CLOSE wins before reading data, including stale or malformed geometry.
  if (payload.lifecycle === "close" || payload.status === "closed") return empty("closed", "subject_closed");
  const data = record(payload.data), subject = record(data.subject), outerSubject = record(payload.subject);
  if (subject.lifecycle === "close" || outerSubject.lifecycle === "close") return empty("closed", "subject_closed");
  const actor = text(subject.id), cameraId = text(data.camera_id), stream = text(data.source_stream_id);
  const vision = record(data.vision), spatial = record(data.spatial), camera = record(spatial.camera);
  const ground = record(spatial.person_ground), pointing = record(spatial.pointing), gestures = record(vision.gestures);
  const capture = record(data.capture_evidence), timestamp = vision.pose_media_ts, packet = text(vision.pose_frame_packet_id);
  if (notification.type !== HUMAN_OBSERVATION_TYPE || !finite(now) || nonfinite(data)) return empty("unavailable", "invalid_payload");
  const envelope = text(payload.packet_id), parent = text(payload.parent_packet_id);
  // Tracking's event assembler creates one child envelope and preserves the image
  // payload. Only its explicit immediate parent proves this frame lineage.
  if (!actor || !cameraId || !stream || !packet || !envelope || (envelope !== packet && parent !== packet) || !finite(timestamp)
    || (outerSubject.id !== undefined && outerSubject.id !== actor)) return empty("unavailable", "identity_or_packet_mismatch");
  if (!finite(capture.published_at) || capture.published_at <= 0 || !text(capture.capture_instance)
    || !Number.isSafeInteger(capture.generation) || Number(capture.generation) < 0
    || !Number.isSafeInteger(capture.sequence) || Number(capture.sequence) <= 0
    || (capture.physical_timestamp_verified !== undefined && typeof capture.physical_timestamp_verified !== "boolean")) return empty("unavailable", "capture_evidence_required");
  const composition = text(camera.composition_id), calibration = text(record(camera.geometry).calibration_digest);
  const cameraFrame = text(camera.frame_packet_id);
  const metricReady = camera.status === "ready";
  if (metricReady && (camera.camera_id !== cameraId || (cameraFrame !== packet && cameraFrame !== envelope)
    || !composition || !calibration || !text(camera.physical_view_id))) return empty("unavailable", "metric_camera_binding_required");
  if (metricReady && compositionId !== undefined && compositionId !== composition) return empty("unavailable", "composition_mismatch");
  const poses = Array.isArray(vision.poses) ? vision.poses.map(record).filter((pose) => pose.actor_subject_id === actor) : [];
  if (poses.length !== 1 || (poses[0].camera_id !== undefined && poses[0].camera_id !== cameraId)
    || (poses[0].source_stream_id !== undefined && poses[0].source_stream_id !== stream)) return empty("unavailable", "pose_identity_mismatch");
  for (const value of metricReady ? [ground, pointing] : []) {
    if (!Object.keys(value).length) continue;
    if (value.schema_version !== 1 || value.actor_subject_id !== actor || value.frame_packet_id !== cameraFrame || !finite(value.timestamp) || Math.abs(value.timestamp - timestamp) > 0.001
      || value.units !== "meters" || value.world_axes !== "x_y_up_z"
      || (value.calibration_digest !== undefined && value.calibration_digest !== calibration)
      || (value.camera_id !== undefined && value.camera_id !== cameraId)
      || (value.source_stream_id !== undefined && value.source_stream_id !== stream)
      || (value.composition_id !== undefined && value.composition_id !== composition)) return empty("unavailable", "spatial_binding_mismatch");
  }
  if (Object.keys(gestures).length && (gestures.schema_version !== 1 || gestures.actor_subject_id !== actor || gestures.camera_id !== cameraId || gestures.source_stream_id !== stream
    || !finite(gestures.frame_ts) || Math.abs(gestures.frame_ts - timestamp) > 0.001)) return empty("unavailable", "gesture_binding_mismatch");
  const durations = [HUMAN_OBSERVATION_MAX_AGE_MS];
  if (metricReady && ground.status === "estimated") {
    if (ground.calibration_digest !== calibration || !finite(ground.valid_for_seconds) || ground.valid_for_seconds <= 0) return empty("unavailable", "ground_validity_required");
    durations.push(ground.valid_for_seconds * 1000);
    for (const foot of Object.values(record(ground.feet)).map(record)) {
      if (foot.status === "estimated" && finite(foot.valid_for_seconds)) durations.push(foot.valid_for_seconds * 1000);
    }
  }
  const published = capture.published_at * 1000, expiresAt = published + Math.min(...durations);
  const base = { actor, camera: cameraId, composition: composition || null, timestamp, expiresAt,
    scopeKey: JSON.stringify([actor, cameraId, stream]),
    metricScopeKey: metricReady ? JSON.stringify([composition, calibration, camera.physical_view_id]) : null,
    captureKey: JSON.stringify([capture.capture_instance, capture.generation, capture.sequence]) };
  if (now < published) return { ...empty("unavailable", "capture_clock_skew"), ...base };
  if (now >= expiresAt) return { ...empty("expired", "evidence_expired"), ...base };
  const result: HumanObservation = { ...empty("current", "source_publication_age_only"), ...base };
  const landmarks = Array.isArray(poses[0].landmarks) ? poses[0].landmarks.map(record) : [];
  result.pose2D = landmarks.filter((item) => item.provenance === "image_estimate" && text(item.name)
    && Array.isArray(item.position) && item.position.length === 2 && item.position.every(finite))
    .map((item) => ({ name: text(item.name), position: [...item.position as [number, number]] }));
  if (metricReady && ground.status === "estimated") {
    result.body = anchor(ground.body, true, timestamp);
    result.left = anchor(record(ground.feet).left, false, timestamp);
    result.right = anchor(record(ground.feet).right, false, timestamp);
    result.mapRevision = text(ground.map_revision) || null;
  }
  if ((gestures.status === "active" || gestures.status === "none" || gestures.status === "unknown")
    && gestures.coordinate_basis === "body_relative_2d") {
    const sideEvidence = Object.prototype.hasOwnProperty.call(gestures, "side_evidence") ? {
      left: gestureSideEvidence(record(gestures.side_evidence).left),
      right: gestureSideEvidence(record(gestures.side_evidence).right),
    } : null;
    result.gestureEvidence = { status: gestures.status, reason: text(gestures.reason), side_evidence: sideEvidence };
    // Unknown frames only authorize partial candidates under the explicit
    // per-side contract. "Available" means usable input, never a raised arm.
    const partial = gestures.status === "unknown" && gestures.reason === "partial_landmarks" && sideEvidence !== null;
    const currentActiveGestures = new Set<string>();
    for (const [values, candidate] of [[gestures.active, false], [gestures.candidates, true]] as const) {
      if (!Array.isArray(values)) continue;
      if (gestures.status === "unknown" && (!partial || !candidate)) continue;
      for (const value of values.map(record)) {
        if (value.evidence !== "heuristic_2d" || (!candidate && value.evidence_current !== true) || !text(value.name)) continue;
        if (sideEvidence) {
          const side = value.side;
          if (side === "both") {
            if (sideEvidence.left.status !== "available" || sideEvidence.right.status !== "available") continue;
          } else if (side !== "left" && side !== "right") continue;
          else if (sideEvidence[side].status !== "available") continue;
        }
        const name = text(value.name), side = text(value.side);
        const gestureKey = JSON.stringify([name, side]);
        // Backend candidates include current positives even after temporal activation.
        // Only an accepted active entry replaces its identical candidate presentation.
        if (candidate && currentActiveGestures.has(gestureKey)) continue;
        if (!candidate) currentActiveGestures.add(gestureKey);
        result.gestures.push({ name, side, evidence: "heuristic_2d", candidate });
      }
    }
  }
  if (!metricReady) {
    result.reason = text(camera.reason) || "metric_calibration_required";
    result.body = unavailableAnchor(text(ground.reason) || result.reason);
    result.left = unavailableAnchor(text(record(record(ground.feet).left).reason) || result.body.reason);
    result.right = unavailableAnchor(text(record(record(ground.feet).right).reason) || result.body.reason);
    result.pointing.reason = text(pointing.reason) || result.reason;
    return result;
  }
  if (!Object.keys(pointing).length) return result;
  if (pointing.status !== "unavailable" && (pointing.actions_authorized !== false || pointing.provenance !== "image_3d_geometric_alignment"
    || pointing.calibration_digest !== calibration || pointing.composition_id !== composition || !text(pointing.map_revision))) {
    result.pointing.reason = "pointing_geometry_unverified";
    return result;
  }
  const candidateValues = Array.isArray(pointing.candidates) ? pointing.candidates.map(record) : [];
  result.pointing = { status: text(pointing.status) || "unavailable", reason: text(pointing.reason), selected: null,
    mapRevision: text(pointing.map_revision) || null, ray: null,
    candidates: candidateValues.filter((value) => text(value.entity_id) && (value.central_ray_distance_meters === null
      || (finite(value.central_ray_distance_meters) && value.central_ray_distance_meters > 0)))
      .map((value) => ({ id: text(value.entity_id), distance: value.central_ray_distance_meters as number | null, occlusion: text(value.occlusion) })) };
  if (result.mapRevision && result.pointing.mapRevision && result.mapRevision !== result.pointing.mapRevision) {
    return empty("unavailable", "spatial_map_revision_mismatch");
  }
  // Older observations carry the verified revision only in pointing. New ground
  // estimates remain independently displayable when that operator is absent.
  result.mapRevision ??= result.pointing.mapRevision;
  if (pointing.status === "candidate" && result.pointing.candidates.some((value) => value.id === pointing.selected_entity_id)) result.pointing.selected = text(pointing.selected_entity_id);
  const origin = position(pointing.origin), direction = position(pointing.direction), uncertainty = record(pointing.uncertainty), alignment = record(pointing.alignment);
  const lengths = result.pointing.candidates.map((value) => value.distance).filter((value): value is number => value !== null);
  // Extent must come from actual intersections, never an invented world target.
  if (["candidate", "ambiguous", "none"].includes(text(pointing.status)) && pointing.actions_authorized === false
    && pointing.provenance === "image_3d_geometric_alignment" && pointing.calibration_digest === calibration && pointing.composition_id === composition
    && result.pointing.mapRevision && alignment.metric_accuracy_qualified === false && alignment.global_elevation_ambiguity_resolved === false
    && alignment.method === "image_rays_and_current_metric_support" && finite(alignment.scale) && alignment.scale > 0
    && finite(alignment.maximum_reprojection_error) && alignment.maximum_reprojection_error >= 0 && alignment.maximum_reprojection_error <= 0.03
    && origin && direction && Math.abs(Math.hypot(...direction) - 1) < 0.001 && lengths.length
    && uncertainty.kind === "uncalibrated_cone" && uncertainty.calibrated === false
    && finite(uncertainty.half_angle_degrees) && uncertainty.half_angle_degrees >= 5 && uncertainty.half_angle_degrees <= 30
    && finite(uncertainty.origin_radius_meters) && uncertainty.origin_radius_meters >= 0 && uncertainty.origin_radius_meters <= 0.75
    && lengths.every((value) => value <= 100)) {
    result.pointing.ray = { origin, direction, length: Math.max(...lengths), halfAngle: uncertainty.half_angle_degrees, radius: uncertainty.origin_radius_meters };
  }
  return result;
}

/** Per-notification lifetime: duplicate updates cannot extend a captured frame. */
export function createHumanObservationReader(compositionId?: string) {
  let scope: string | null = null, notificationId: string | null = null, closed = false;
  let metricScope: string | null = null;
  let latestTimestamp = -Infinity, latestCapture: string | null = null;
  let latestEpoch = "", latestSequence = -1;
  let maximumNow = -Infinity;
  const deadlines = new Map<string, number>();
  return (notification: Notification, now: number): HumanObservation => {
    if (!finite(now)) return empty("unavailable", "invalid_clock");
    maximumNow = Math.max(maximumNow, now);
    if (closed) return empty("closed", "subject_closed");
    const result = readHumanObservation(notification, maximumNow, compositionId);
    if (result.state === "closed") { closed = true; return result; }
    if (notificationId !== null && notificationId !== notification.id) return empty("unavailable", "notification_identity_changed");
    notificationId = notification.id;
    if (scope !== null && result.scopeKey !== null && scope !== result.scopeKey) return empty("unavailable", "observation_scope_changed");
    if (metricScope !== null && result.metricScopeKey !== null && metricScope !== result.metricScopeKey) return empty("unavailable", "observation_scope_changed");
    if (result.scopeKey) scope = result.scopeKey;
    if (result.metricScopeKey) metricScope = result.metricScopeKey;
    if (result.timestamp !== null && result.captureKey !== latestCapture && result.timestamp <= latestTimestamp) return empty("unavailable", "superseded_frame");
    if (result.state === "current" && result.timestamp !== null && result.captureKey) {
      const [instance, generation, sequence] = JSON.parse(result.captureKey) as [string, number, number];
      const epoch = JSON.stringify([instance, generation]);
      if (result.captureKey !== latestCapture && epoch === latestEpoch && sequence <= latestSequence) return empty("unavailable", "superseded_capture_sequence");
      latestTimestamp = result.timestamp; latestCapture = result.captureKey; latestEpoch = epoch; latestSequence = sequence;
    }
    if (result.captureKey && result.expiresAt !== null) {
      const deadline = Math.min(deadlines.get(result.captureKey) ?? Infinity, result.expiresAt);
      deadlines.set(result.captureKey, deadline);
      // Bound retained frame deadlines; old frames already fail the 750 ms age check.
      if (deadlines.size > 64) deadlines.delete(deadlines.keys().next().value!);
      if (maximumNow >= deadline) return { ...empty("expired", "evidence_expired"), actor: result.actor, camera: result.camera,
        composition: result.composition, timestamp: result.timestamp, captureKey: result.captureKey, scopeKey: result.scopeKey, expiresAt: deadline };
      result.expiresAt = deadline;
    }
    return result;
  };
}
