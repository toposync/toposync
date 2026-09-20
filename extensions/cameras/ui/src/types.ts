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
  automation_exclusive_control_confirmed?: boolean;
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
  boundary_refinement_points?: CameraProjectionBoundaryPoint[];
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

export type CameraProjectionBoundaryEdge = "top" | "right" | "bottom" | "left";

export type CameraProjectionBoundaryPoint = {
  id: string;
  edge: CameraProjectionBoundaryEdge;
  t: number;
  image: { x: number; y: number };
  world: { x: number; z: number };
};

export type CameraProjectionBoundaryRefinement = {
  model: "edge_handles_v1";
  points: CameraProjectionBoundaryPoint[];
};

export type CameraVisualPoseSignature = {
  algorithm: "orb_hamming_v1";
  keypoint_count: number;
  keypoints_base64: string;
  descriptors_base64: string;
  original_width: number;
  original_height: number;
  digest_sha256: string;
};

export type CameraProjectionModel = {
  type: "image_quad_on_world";
  image_region: CameraImageRegion;
  world_quad: CameraProjectionWorldQuad;
  refinement?: CameraProjectionRefinement | null;
  boundary_refinement?: CameraProjectionBoundaryRefinement | null;
  visual_pose_signature?: CameraVisualPoseSignature | null;
};

export type CameraGroundLens =
  | { type: "identity_rectilinear_v1" }
  | {
      type: "rectilinear_brown_v1";
      fx: number;
      fy: number;
      cx: number;
      cy: number;
      coefficients: number[];
    }
  | {
      type: "fisheye_kb4_v1";
      fx: number;
      fy: number;
      cx: number;
      cy: number;
      coefficients: number[];
    };

export type CameraGroundCorrespondence = {
  id: string;
  role: "fit" | "check";
  origin: "manual" | "automatic";
  image: { x: number; y: number };
  world: { x: number; z: number };
};

export type CameraRayGroundProjectionModel = {
  type: "camera_ray_ground_v2";
  solver_version: 1;
  source_geometry: {
    width: number;
    height: number;
    content_rect?: CameraImageRegion;
    rotation_degrees?: 0 | 90 | 180 | 270;
    mirror_x?: boolean;
    mirror_y?: boolean;
  };
  lens: CameraGroundLens;
  correspondences: CameraGroundCorrespondence[];
  visual_pose_signature?: CameraVisualPoseSignature | null;
};

export type CameraRayGroundCalibratedView = {
  id: string;
  label: string;
  /** Presentation-only rotation of the floor-plan canvas for this calibration view. */
  editor_view_rotation_degrees?: 0 | 90 | 180 | 270;
  pose_reference?: CameraPoseReference | null;
  requires_pose_evidence?: boolean;
  stream_scope: {
    physical_view_id: string;
    compatible_roles: string[];
    compatible_source_ids: string[];
    compatible_view_ids?: string[];
  };
  projection_model: CameraRayGroundProjectionModel;
  projection_quality?: {
    status?: "ready" | "estimated" | "incomplete";
    estimated?: boolean;
    note?: string | null;
    calibration_digest?: string | null;
    fit_points?: number | null;
    fit_inliers?: number | null;
    check_points?: number | null;
    check_errors_meters?: number[];
    image_coverage_ratio?: number | null;
    solver?: string | null;
  };
};

export type CameraProjectionSolveResult = {
  metric_geometry?: {
    status: "ready" | "unavailable";
    reason?: string;
    maximum_reprojection_error?: number | null;
    check_errors_meters?: number[];
  };
  accepted: boolean;
  status: "ready" | "review" | "incomplete";
  quality: {
    status: "ready" | "review" | "incomplete";
    number_of_fit_points: number;
    number_of_inliers: number;
    inlier_ratio: number;
    image_hull_area_ratio_uv: number;
    check_errors_meters: number[];
    median_reprojection_error_uv: number | null;
    p95_reprojection_error_uv: number | null;
    is_numerically_unstable: boolean;
  };
  calibration_digest: string;
  valid_image_polygon: Array<{ x: number; y: number }>;
  valid_world_polygon: Array<{ x: number; z: number }>;
};

export type CameraVisualCalibrationProjectionModel = CameraProjectionModel & {
  visual_pose_signature: CameraVisualPoseSignature;
};

export type CameraVisualCalibrationQuality = {
  method: string;
  source_width: number;
  source_height: number;
  target_width: number;
  target_height: number;
  source_keypoints: number;
  target_keypoints: number;
  candidate_matches: number;
  inliers: number;
  inlier_ratio: number;
  competing_inliers: number;
  ambiguity_ratio: number;
  symmetry_inliers: number;
  symmetry_coverage_ratio: number;
  median_reprojection_error_px: number | null;
  p95_reprojection_error_px: number | null;
  source_coverage_ratio: number;
  target_coverage_ratio: number;
  overlap_ratio: number;
  median_displacement_ratio: number;
  refinement_points: number;
  discarded_refinement_points: number;
  duration_ms: number;
};

export type CameraVisualCalibrationResult = {
  accepted: boolean;
  reason: string | null;
  projection_model: CameraVisualCalibrationProjectionModel | null;
  source_visual_pose_signature: CameraVisualPoseSignature | null;
  quality: CameraVisualCalibrationQuality;
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
  preset_token?: string | null;
  preset_name?: string | null;
  geometry_safe?: boolean | null;
  motion_epoch?: number | null;
  motion_state?: string | null;
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

export type CameraPipelinePreset =
  | "people_simple"
  | "people_individual"
  | "people_quiet"
  | "presence_area"
  | "vehicle_stopped"
  | "person_stopped"
  | "person_vehicle_interaction";
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
  model_id?: string;
  composition_id?: string;
  area_id?: string;
  stopped_speed_threshold?: number;
  min_stationary_seconds?: number;
  notification_title?: string;
  notification_description?: string;
  notification_priority?: CameraNotificationPriority;
  enable_ptz_attention?: boolean;
  ptz_attention_native_tracking_disabled_confirmed?: boolean;
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

export type CameraPanoramaProfile = {
  lens: { width: number; height: number; fx: number; fy: number; cx: number; cy: number; distortion: number[] };
  pan_axis: { position_min: number; position_max: number; angle_min_radians: number; angle_max_radians: number };
  tilt_axis: { position_min: number; position_max: number; angle_min_radians: number; angle_max_radians: number };
  zoom: number;
  position_tolerance: number;
  settle_timeout_seconds: number;
};

export type CameraPanoramaScan = {
  pan_min: number;
  pan_max: number;
  tilt_min: number;
  tilt_max: number;
  overlap: number;
};

export type CameraPanoramaPoint = {
  id: string;
  role: "fit" | "check";
  panorama: { x: number; y: number };
  world: { x: number; z: number };
};

export type CameraPanoramaCheck = {
  id: string;
  point_id: string;
  revision: number;
  image_url: string;
  result: "correct" | "offset" | "unverifiable" | null;
  observed_image?: { x: number; y: number };
  evidence?: { kind?: string; verified?: boolean; lens?: { width: number; height: number; cx: number; cy: number } };
};

export type CameraPanoramaJob = {
  id: string;
  revision: number;
  camera_id: string;
  source_id: string;
  element_id: string;
  composition_id: string;
  state: "draft" | "capturing" | "processing" | "ready" | "failed" | "cancelled" | "interrupted";
  profile: CameraPanoramaProfile | null;
  scan: CameraPanoramaScan | null;
  progress: { captured: number; total: number; stage: string };
  panorama_url: string | null;
  coverage_url: string | null;
  coverage_bounds?: { min_x: number; min_y: number; max_x: number; max_y: number } | null;
  stop_confirmed?: boolean;
  points: CameraPanoramaPoint[];
  solution: import("./elements/panoramaProjection").PanoramaProjectionSolution | null;
  physical_blockers?: string[];
  permissions?: { map_validated: boolean; can_activate: boolean; can_verify_aim: boolean; aim_enabled: boolean };
  navigation?: { phase: string; physical_state: string; can_return?: boolean; error_code?: string | null };
  source_panorama?: CameraPanoramaSourceArtifact | null;
  checks: CameraPanoramaCheck[];
  active: boolean;
  error: { code: string; message: string } | null;
  updated_at: string | number;
  aim_image_url?: string;
};

export type CameraPanoramaSourceArtifact = { id: string; compatible: false; blockers: string[] } | {
  id: string;
  revision: number;
  compatible: true;
  blockers: string[];
  image_url: string;
  coverage_url: string;
  width: number;
  height: number;
  crop: CameraSourcePanoramaCrop | null;
  crop_revision: number;
};

export type CameraPanoramaContext = {
  camera_id: string;
  element_id: string;
  composition_id: string;
  sources: Array<{ id: string; label: string; profile?: CameraPanoramaProfile | null; panorama?: CameraPanoramaSourceArtifact | null }>;
  profile: CameraPanoramaProfile | null;
  job: CameraPanoramaJob | null;
  active: { job_id: string; revision: number } | null;
  previous?: { job_id: string; revision: number } | null;
  blockers: string[];
};

/** A non-destructive selection in the canonical, periodic panorama image. */
export type CameraSourcePanoramaCrop = {
  u_start: number;
  u_width: number;
  v_start: number;
  v_height: number;
};

export type CameraSourcePanoramaOutcomes = {
  acquisition: "pending" | "running" | "completed" | "sufficient" | "incomplete";
  reconstruction: "pending" | "running" | "ready" | "review" | "failed" | "interrupted";
  return: "pending" | "running" | "verified" | "unverified";
};

export type CameraSourcePanoramaArtifact = {
  outcomes?: CameraSourcePanoramaOutcomes;
  capture_goal?: "initial_region" | "reachable_domain";
  region_status?: "ready" | "incomplete" | "review";
  id: string;
  revision: number;
  camera_id: string;
  source_id: string;
  status: "ready" | "partial";
  created_at: string | number;
  width: number;
  height: number;
  image_url: string;
  coverage_url?: string | null;
  crop: CameraSourcePanoramaCrop | null;
  crop_revision: number;
  coverage_ratio: number;
  coverage?: {
    bounds_pixels?: { left: number; top: number; right: number; bottom: number } | null;
    acquisition_complete?: boolean;
    acquisition?: { bands?: Record<string, { complete?: boolean }>; progress?: { bands_completed?: number; regions_pending?: number } };
  } | null;
  quality: Record<string, unknown>;
  quality_approved?: boolean;
  presentation?: {
    status?: "verified" | "unverified";
    method?: string;
    horizontal_motion_pairs?: number;
    horizontal_rotation_degrees?: number;
    axis_divergence_degrees_p95?: number | null;
  } | null;
  positioning_status: "not_validated";
  stale?: boolean;
  stale_reason?: "source_changed" | "source_unavailable" | null;
};

export type CameraSourcePanoramaTelemetrySample = {
  elapsed_seconds: number;
  motion_pixels: number | null;
  speed_px_s: number | null;
  media_time: number | null;
  drift_pixels: number | null;
  confidence: number | null;
  state: string;
  pose?: { pan?: number; tilt?: number; native_pan?: number; native_tilt?: number } | null;
};

export type CameraSourcePanoramaTelemetry = {
  kind: "movement";
  outcome: "accepted" | "timeout" | "inconclusive";
  timing_basis: "media" | "local_observation";
  analysis_width: number;
  samples: CameraSourcePanoramaTelemetrySample[];
  command_accepted_seconds?: number | null;
  first_target_readback_seconds?: number | null;
  first_motion_transition_seconds?: number | null;
  stop_requested_seconds?: number | null;
  stop_accepted_seconds?: number | null;
};

export type CameraSourcePanoramaJob = {
  outcomes?: CameraSourcePanoramaOutcomes;
  capture_goal?: "initial_region" | "reachable_domain";
  operation?: "capture" | "verify_control";
  control_checks_passed?: number;
  coverage_progress?: {
    primary_complete: boolean;
    bands_completed: number;
    current_band: number | null;
    stage?: "reference" | "pan" | "step" | "return_reference" | "done";
    regions_pending?: number;
    continued_after_recovery?: boolean;
    goal?: "initial_region";
    qualified_views?: number;
    required_views?: number;
    region_complete?: boolean;
    policy_version?: number;
    region_rows_completed?: number;
    region_phase?: "prepare" | "first_row" | "height_change" | "second_row" | "done" | "reference" | "lower" | "side_seed" | "opposite" | "side_extension";
    decision?: {
      experimental: boolean;
      targets: Record<string, number>;
      observed: {
        row_views: Record<string, number>;
        horizontal_extents: Record<string, number>;
        vertical_extent: number;
        transverse_links: number;
        independent_transverse_anchors: number;
      };
      criteria: Record<string, boolean>;
      sufficient: boolean;
      pending: string[];
    } | {
      sufficient: false;
      route_complete: boolean;
      coverage_approval: "pending_visual_acceptance";
      criteria: Record<string, boolean>;
      observed: { extents: Record<string, number>; connected_captures: string[] };
    };
  } | null;
  id: string;
  camera_id: string;
  source_id: string;
  status: "queued" | "preparing" | "exploring" | "capturing" | "returning" | "processing" | "ready" | "verified" | "partial" | "interrupted" | "failed" | "stopping";
  phase: string;
  captures_accepted: number;
  planned_captures?: number | null;
  estimated_remaining_seconds?: number | null;
  error?: { code: string; message: string } | null;
  physical_state: "unknown" | "stopped" | "restored" | "returning" | "stop_unconfirmed" | "ownership_lost";
  artifact_id?: string | null;
  preview_url?: string | null;
  can_resume: boolean;
  resume_unavailable_code?: string | null;
  can_return?: boolean;
  can_reconstruct?: boolean;
  can_cleanup?: boolean;
  created_at: string | number;
  updated_at: string | number;
  issues?: string[];
  issue_codes?: string[];
  telemetry?: CameraSourcePanoramaTelemetry | null;
};

export type CameraSourcePanorama = {
  camera_id: string;
  source_id: string;
  active: CameraSourcePanoramaArtifact | null;
  previous: CameraSourcePanoramaArtifact | null;
  candidate?: CameraSourcePanoramaArtifact | null;
  replacement_pending?: boolean;
  job: CameraSourcePanoramaJob | null;
};
