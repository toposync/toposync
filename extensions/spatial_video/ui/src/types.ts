import type { CompositionElement } from "@toposync/plugin-api";

export type Vector2 = { x: number; y: number };
export type WorldPoint = { x: number; z: number };

export type CameraPoseReference = {
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
  preset_token?: string | null;
  preset_name?: string | null;
};

export type CameraControlPoint = {
  id?: string;
  label?: string;
  image?: Vector2 | null;
  world?: WorldPoint | null;
};

export type CameraProjectionRefinementPoint = {
  id: string;
  image: Vector2;
  world: WorldPoint;
};

export type CameraProjectionBoundaryEdge = "top" | "right" | "bottom" | "left";

export type CameraProjectionBoundaryPoint = {
  id: string;
  edge: CameraProjectionBoundaryEdge;
  t: number;
  image: Vector2;
  world: WorldPoint;
};

export type CameraControlPointSet = {
  id: string;
  label: string;
  pose_reference?: CameraPoseReference | null;
  stream_scope?: {
    compatible_roles?: string[];
    compatible_source_ids?: string[];
  };
  control_points?: CameraControlPoint[];
  refinement_points?: CameraProjectionRefinementPoint[];
  boundary_refinement_points?: CameraProjectionBoundaryPoint[];
};

export type AreaClip = {
  areaElementId: string;
  label: string;
  polygon: WorldPoint[];
  signature: string;
  warning?: string | null;
};

export type StreamingTransport = "mse" | "hls" | "jsmpeg" | "webrtc";

export type MediaContentRect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type StreamingOutputUrl = {
  output_id: string;
  protocol: StreamingTransport | "rtsp";
  url: string;
  quality_profile_id?: string | null;
  content_rect?: MediaContentRect | null;
};

export type StreamingPlaybackPlanTransport = {
  transport: StreamingTransport;
  rank: number;
  available: boolean;
  output_id?: string | null;
  url?: string | null;
  protocol?: StreamingTransport | "rtsp" | null;
  blocking_errors?: string[];
  warnings?: string[];
};

export type StreamingPlaybackResponse = {
  live_view: CameraLiveView;
  context: "spatial_map";
  variant: CameraLiveVariant;
  camera_id: string;
  camera_source_id: string;
  camera_name: string;
  transmission: { id: string; name: string };
  urls: { outputs: StreamingOutputUrl[]; warnings?: string[]; blocking_errors?: string[] };
  playback_plan?: {
    lease_seconds?: number;
    heartbeat_interval_seconds?: number;
    selected_transport?: StreamingTransport | null;
    transports?: StreamingPlaybackPlanTransport[];
  } | null;
  selected_output?: StreamingOutputUrl | null;
};

export type CameraLiveVariant = {
  id: string;
  label: string;
  role: string;
  camera_source_id?: string | null;
  transmission_id: string;
  output_id?: string | null;
  quality_profile_id?: string | null;
  enabled?: boolean;
};

export type CameraLiveView = {
  id: string;
  camera_id?: string | null;
  name: string;
  enabled?: boolean;
  variants?: CameraLiveVariant[];
};

export type PtzStatus = {
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
  move_status?: string | null;
  preset_token?: string | null;
  preset_name?: string | null;
};

export type PtzPreset = {
  token?: string | null;
  name?: string | null;
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
};

export type ProjectionCandidate = {
  id: string;
  cameraId: string;
  cameraSourceId?: string | null;
  liveViewId: string;
  label: string;
  element: CompositionElement;
  controlPointSets: CameraControlPointSet[];
  initialControlPointSet: CameraControlPointSet;
  variantId?: string | null;
  areaClip?: AreaClip | null;
  areaClipWarning?: string | null;
};

export type ActiveProjectionPose = {
  set: CameraControlPointSet;
  status: "matched" | "interpolated" | "extrapolated" | "nearest_reference" | "single_reference" | "fallback" | "unmatched";
  moving: boolean;
};

export type ProjectionMeshData = {
  positions: Float32Array;
  uvs: Float32Array;
  indices: Uint32Array;
};
