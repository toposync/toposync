export type CameraConnectionType = "rtsp" | "onvif";

export type CameraOnvifConfig = {
  device_id?: string;
  xaddr: string;
  media_xaddr?: string;
  ptz_xaddr?: string;
  profile_token?: string;
  profile_name?: string;
  hardware?: string;
};

export type CameraConfig = {
  id: string;
  name: string;
  connection_type: CameraConnectionType;
  channel_id?: string;
  rtsp_url: string;
  username?: string;
  password?: string;
  fps: number;
  onvif?: CameraOnvifConfig | null;
};

export type CamerasIndex = {
  cameras: Array<{ id: string; name: string; connection_type: CameraConnectionType | string }>;
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
};

export type CameraMappingQuality = {
  status: "good" | "review" | "incomplete";
  complete_points: number;
  convex_hull_area_ratio_uv: number;
  is_pose_bound: boolean;
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
};

export type CameraContextCameraElement = {
  id: string;
  name: string;
  control_points_pairs: number;
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

export type CameraPipelineWizardPreset = "people" | "vehicles_stopped" | "pets";

export type CameraPipelineWizardRequest = {
  preset: CameraPipelineWizardPreset;
  pipeline_name?: string;
  enabled?: boolean;
  processing_server_id?: string;
  composition_id?: string;
  area_id?: string;
  notification_title?: string;
  notification_description?: string;
};

export type CameraPipelineWizardResponse = {
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
