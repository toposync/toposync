export type CameraConnectionType = "rtsp" | "onvif";
export type CameraStreamProfile = "onvif" | "custom";
export type CameraIngestMode = "centralized" | "runtime_local" | "direct";
export type CameraControlType = "onvif" | "none";
export type CameraSourceKind = "video" | "audio" | "data";
export type CameraSourceRole = "main" | "sub" | "zoom" | "custom";
export type CameraSourceOriginType = "onvif_profile" | "rtsp";

export type CameraIngestConfig = {
  mode: CameraIngestMode;
  host_server_id: string;
};

export type CameraOnvifConfig = {
  device_id?: string;
  xaddr: string;
  username?: string;
  password?: string;
  media_xaddr?: string;
  ptz_xaddr?: string;
  profile_token?: string;
  profile_name?: string;
  ptz_profile_token?: string;
  hardware?: string;
};

export type CameraControlConfig = {
  type: CameraControlType;
};

export type CameraSourceOriginConfig = {
  type: CameraSourceOriginType;
  rtsp_url: string;
  stream_username?: string;
  stream_password?: string;
  profile_token?: string | null;
  profile_name?: string | null;
  has_ptz?: boolean;
  metadata?: Record<string, unknown>;
};

export type CameraSourceVideoConfig = {
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  codec?: string | null;
};

export type CameraSourceConfig = {
  id: string;
  name: string;
  enabled: boolean;
  is_default: boolean;
  kind: CameraSourceKind;
  role: CameraSourceRole;
  view_id: string;
  origin: CameraSourceOriginConfig;
  video: CameraSourceVideoConfig;
  ingest: CameraIngestConfig;
  metadata?: Record<string, unknown>;
};

export type StreamPublication = {
  id: string;
  owner_kind: "camera_source" | "pipeline_output";
  camera_id?: string | null;
  camera_source_id?: string | null;
  enabled?: boolean;
  role: CameraSourceRole;
  label: string;
  host_server_id?: string;
  quality_policy?: Record<string, unknown>;
  transport_policy?: Record<string, unknown>;
};

export type CameraConfig = {
  id: string;
  name: string;
  enabled: boolean;
  control: CameraControlConfig;
  onvif?: CameraOnvifConfig | null;
  sources: CameraSourceConfig[];
  metadata?: Record<string, unknown>;
};

export type CamerasIndex = {
  cameras: Array<{
    id: string;
    name: string;
    control?: CameraControlConfig;
    sources?: CameraSourceConfig[];
  }>;
};

export type ProcessingServer = {
  id: string;
  name?: string;
  kind?: "inprocess" | "http" | string;
  url?: string;
};

export type CameraSourceHealthStatus =
  | "healthy"
  | "starting"
  | "stale"
  | "unreachable"
  | "unauthorized"
  | "error"
  | "idle"
  | "unknown";

export type CameraSourceHealthItem = {
  source_id: string;
  camera_id?: string | null;
  camera_source_id?: string | null;
  camera_source_name?: string | null;
  camera_name?: string | null;
  pipeline_name?: string | null;
  node_id?: string | null;
  backend?: string | null;
  configured_backend: string;
  source_frame_age_seconds?: number | null;
  capture_fps?: number | null;
  target_fps?: number | null;
  opened: boolean;
  restarts_total: number;
  decode_failures: number;
  frames_captured: number;
  last_frame_at_unix?: number | null;
  last_seen_at_unix?: number | null;
  last_error?: string | null;
  rtsp_transport: string;
  used_ingest: boolean;
  ingest_mode?: CameraIngestMode;
  centralizer_server_id?: string | null;
  ingest_path?: string | null;
  ingest_warnings?: string[];
  ingest_blocking_errors?: string[];
  status: CameraSourceHealthStatus;
  recommended_action: string;
};

export type CameraSourceHealthResponse = {
  updated_at_unix: number;
  stale_after_seconds: number;
  offline_after_seconds: number;
  retention_seconds: number;
  sources: CameraSourceHealthItem[];
};

export type RtspProbeStatus = "ok" | "unreachable" | "unauthorized" | "timeout" | "probe_error";

export type RtspProbeResponse = {
  status: RtspProbeStatus;
  url: string;
  transports_tested: string[];
  latency_ms: number;
  backend: string;
  source: string;
  error?: string | null;
};

export type CameraControlPoint = {
  id: string;
  label: string;
  image?: { x: number; y: number } | null;
  world?: { x: number; z: number } | null;
};

export type CameraPoseReference = {
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
  preset_token?: string | null;
  preset_name?: string | null;
};

export type CameraControlPointSet = {
  id: string;
  label: string;
  pose_reference?: CameraPoseReference | null;
  control_points: CameraControlPoint[];
  refinement_points?: CameraProjectionRefinementPoint[];
};

export type CameraMappingQuality = {
  status: "good" | "review" | "incomplete";
  complete_points: number;
  convex_hull_area_ratio_uv: number;
  is_pose_bound: boolean;
};

export type CameraProjectionCornerKey = "top_left" | "top_right" | "bottom_right" | "bottom_left";

export type CameraProjectionWorldQuad = Record<CameraProjectionCornerKey, { x: number; z: number }>;

export type CameraImageRegion = {
  top_left: { x: number; y: number };
  bottom_right: { x: number; y: number };
};

export type CameraProjectionRefinementPoint = {
  id: string;
  image: { x: number; y: number };
  world: { x: number; z: number };
};

export type CameraProjectionRefinement = {
  model: "local_rbf_v1";
  points: CameraProjectionRefinementPoint[];
};

export type CameraProjectionModel = {
  type: "image_quad_on_world";
  image_region: CameraImageRegion;
  world_quad: CameraProjectionWorldQuad;
  refinement?: CameraProjectionRefinement | null;
};

export type CameraCalibratedView = {
  id: string;
  label: string;
  pose_reference?: CameraPoseReference | null;
  stream_scope?: {
    compatible_roles?: string[];
    compatible_source_ids?: string[];
  };
  projection_model: CameraProjectionModel;
  projection_quality?: {
    status?: "ready" | "estimated" | "incomplete";
    estimated?: boolean;
    note?: string | null;
  };
};

export type PanTiltZoomState = {
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
  move_status?: string | null;
  utc_time?: string | null;
  error?: string | null;
  source?: string | null;
  confidence?: number | null;
};

export type CameraPtzPreset = {
  token: string;
  name?: string;
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
};

export type CameraContextArea = {
  id: string;
  name: string;
  vertices_count: number;
  vertices?: { x: number; z: number }[];
};

export type CameraContextCameraElement = {
  id: string;
  name: string;
  control_points_pairs: number;
  calibrated_views?: number;
  has_mapping: boolean;
};

export type CameraContextComposition = {
  id: string;
  name: string;
  camera_elements: CameraContextCameraElement[];
  areas: CameraContextArea[];
};

export type CameraContextsResponse = {
  camera_id: string;
  compositions: CameraContextComposition[];
};

export type CameraPipelinePreset = "people_individual" | "people_quiet" | "presence_area" | "vehicle_stopped";
export type CameraNotificationPriority = "low" | "medium" | "high";

export type CameraPipelineSummary = {
  name: string;
  enabled: boolean;
  processing_server_id?: string;
  source_ids?: string[];
};

export type CameraPipelinesResponse = {
  camera_id: string;
  pipelines: CameraPipelineSummary[];
  suggested_pipeline_names?: Partial<Record<CameraPipelinePreset, string>>;
};

export type CameraPipelinePresetRequest = {
  preset: CameraPipelinePreset;
  source_id?: string;
  pipeline_name?: string;
  enabled?: boolean;
  processing_server_id?: string;
  composition_id?: string;
  area_id?: string;
  stopped_speed_threshold?: number;
  notification_title?: string;
  notification_description?: string;
  notification_priority?: CameraNotificationPriority;
};

export type CameraPipelinePresetResponse = {
  pipeline_name: string;
};

export type OnvifAuthMode = "auto" | "digest" | "text" | "none";

export type OnvifInspectRequest = {
  xaddr: string;
  username?: string;
  password?: string;
  timeout_ms?: number;
  auth?: OnvifAuthMode;
};

export type OnvifProfileInfo = {
  token: string;
  name?: string;
  encoding?: string;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  has_ptz?: boolean;
  stream_uri?: string | null;
};

export type OnvifInspectResponse = {
  xaddr: string;
  media_xaddr?: string | null;
  ptz_xaddr?: string | null;
  profiles: OnvifProfileInfo[];
  warnings?: string[];
};

export type OnvifStreamUriRequest = {
  xaddr: string;
  media_xaddr?: string;
  profile_token: string;
  username?: string;
  password?: string;
  timeout_ms?: number;
  auth?: OnvifAuthMode;
};

export type OnvifStreamUriResponse = {
  rtsp_url: string;
};

export type OnvifDiscoverRequest = {
  timeout_ms?: number;
  force?: boolean;
  exclude_known?: boolean;
};

export type OnvifDiscoveredDeviceInfo = {
  device_id: string;
  xaddr?: string;
  xaddrs?: string[];
  source_ip?: string;
  name?: string;
  hardware?: string;
};

export type OnvifDiscoverResponse = {
  scanned_at_unix: number;
  duration_ms: number;
  cached: boolean;
  targets?: string[];
  warnings?: string[];
  devices: OnvifDiscoveredDeviceInfo[];
};
