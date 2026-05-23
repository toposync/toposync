export type StreamsHealthResponse = {
  status?: string;
  extension?: string;
};

export type StreamingNetworkContractStatus =
  | "ok"
  | "port_mismatch"
  | "proxy_required"
  | "proxy_unavailable"
  | "not_applicable";

export type StreamingNetworkContractPorts = {
  direct_api?: number | null;
  rtsp?: number | null;
  hls?: number | null;
  webrtc?: number | null;
  webrtc_udp?: number | null;
  api?: number | null;
};

export type StreamingNetworkContract = {
  environment?: string;
  mode?: "direct" | "proxy";
  expected_ports?: StreamingNetworkContractPorts;
  actual_ports?: StreamingNetworkContractPorts;
  status?: StreamingNetworkContractStatus;
  public_hls_mode?: "direct" | "proxy";
  webrtc_additional_hosts?: string[];
  warnings?: string[];
  blocking_errors?: string[];
};

export type EngineStatusResponse = {
  running?: boolean;
  metrics_enabled?: boolean;
  metrics_reachable?: boolean;
  pid?: number | null;
  uptime_seconds?: number | null;
  started_at_unix?: number | null;
  bind_host?: string;
  ports?: {
    rtsp?: number;
    hls?: number;
    webrtc?: number;
    webrtc_udp?: number;
    api?: number;
    metrics?: number;
  };
  test_path?: string;
  urls?: {
    rtsp_url?: string;
    hls_url?: string;
    webrtc_url?: string;
  };
  last_error?: string | null;
  network_contract?: StreamingNetworkContract | null;
  mediamtx_version?: string;
  platform?: string | null;
  binary_path?: string | null;
  config_path?: string | null;
  log_path?: string | null;
  warnings?: string[];
  restart_count?: number;
  orphan_pids?: number[];
};

export type TransmissionResolution = {
  width?: number;
  height?: number;
};

export type StreamingQualityProfileId =
  | "quad_grid"
  | "stable_apple_tv"
  | "fullscreen_quality"
  | "diagnostic_low";

export type StreamingLatencyProfile = "normal" | "low" | "ultra_low";

export type StreamingQualityProfile = {
  id: StreamingQualityProfileId;
  label: string;
  resolution: TransmissionResolution;
  fps_limit: number;
  bitrate_kbps: number;
  latency_profile: StreamingLatencyProfile;
  usage?: string;
  default?: boolean;
};

export type StreamingQualityProfilesResponse = {
  default_profile_id: StreamingQualityProfileId;
  profiles: StreamingQualityProfile[];
};

export type StreamAuthentication = {
  enabled?: boolean;
  username?: string | null;
  password?: string | null;
};

export type TransmissionOutput = {
  id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  enabled?: boolean;
  resolution?: TransmissionResolution | null;
  fps_limit?: number | null;
  bitrate_kbps?: number | null;
  latency_profile?: StreamingLatencyProfile;
  encoder_mode?: "inherit" | "auto" | "cpu";
  quality_profile_id?: StreamingQualityProfileId | null;
  authentication?: StreamAuthentication | null;
};

export type Transmission = {
  id: string;
  name: string;
  path: string;
  enabled?: boolean;
  host_server_id?: string;
  placeholder?: "gray" | "black";
  arbitration?: "latest" | "priority_latest";
  camera_controls?: { enabled?: boolean; camera_id?: string | null; camera_source_id?: string | null } | null;
  outputs: TransmissionOutput[];
  created_at?: string;
  updated_at?: string;
  generated_by?: string;
  publication_id?: string;
  owner_kind?: StreamingPublicationOwnerKind;
  camera_id?: string | null;
  camera_source_id?: string | null;
  role?: StreamingPublicationRole;
  camera_live_view_id?: string;
  camera_live_variant_role?: string;
};

export type TransmissionOutputUrl = {
  output_id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  resolved_engine_path: string;
  url: string;
  requires_auth?: boolean;
  auth_username?: string | null;
  media_auth_type?: "none" | "signed_url" | "basic";
  url_expires_at_unix?: number | null;
  renew_after_unix?: number | null;
  quality_profile_id?: StreamingQualityProfileId | null;
  resolution?: TransmissionResolution | null;
  fps_limit?: number | null;
  bitrate_kbps?: number | null;
  latency_profile?: StreamingLatencyProfile | null;
};

export type TransmissionUrlsResponse = {
  transmission_id: string;
  engine_running: boolean;
  outputs: TransmissionOutputUrl[];
  network_contract?: StreamingNetworkContract | null;
  warnings?: string[];
  blocking_errors?: string[];
  public_base_path?: string;
  media_url_origin?: string | null;
};

export type CameraLiveContext = "thumbnail" | "pip" | "large" | "fullscreen" | "ptz";
export type StreamingPublicationOwnerKind = "camera_source" | "pipeline_output";
export type StreamingLiveViewOwnerKind = "camera_source" | "pipeline_output" | "manual";
export type StreamingPublicationRole = "main" | "sub" | "zoom" | "custom";
export type CameraLiveVariantRole = CameraLiveContext | StreamingPublicationRole;
export type CameraLiveTransportPreference = "auto" | "hls" | "webrtc";

export type StreamPublication = {
  id: string;
  owner_kind: StreamingPublicationOwnerKind;
  camera_id?: string | null;
  camera_source_id?: string | null;
  pipeline_name?: string | null;
  publish_node_id?: string | null;
  enabled?: boolean;
  role: StreamingPublicationRole;
  label: string;
  live_view_id?: string | null;
  live_view_label?: string | null;
  variant_id?: string | null;
  variant_label?: string | null;
  host_server_id?: string;
  quality_policy?: Record<string, unknown>;
  transport_policy?: Record<string, unknown>;
};

export type CameraLiveViewDefaults = {
  thumbnail_variant_id: string;
  pip_variant_id: string;
  large_variant_id: string;
  fullscreen_variant_id: string;
  ptz_variant_id?: string | null;
};

export type CameraLiveVariant = {
  id: string;
  label: string;
  role: CameraLiveVariantRole;
  camera_source_id?: string | null;
  transmission_id: string;
  output_id?: string | null;
  quality_profile_id?: StreamingQualityProfileId | null;
  preferred_transport?: CameraLiveTransportPreference;
  enabled?: boolean;
};

export type CameraLiveView = {
  id: string;
  owner_kind?: StreamingLiveViewOwnerKind;
  camera_id?: string | null;
  name: string;
  enabled?: boolean;
  host_server_id?: string;
  defaults: CameraLiveViewDefaults;
  variants: CameraLiveVariant[];
};

export type CameraLiveViewGenerateResponse = {
  camera_live_views: CameraLiveView[];
  transmissions: Transmission[];
  generated_count: number;
  warnings?: string[];
};

export type CameraLiveViewPlaybackResponse = {
  live_view: CameraLiveView;
  context: CameraLiveContext;
  variant: CameraLiveVariant;
  camera_id: string;
  camera_name: string;
  camera_source_id: string;
  camera_source_name: string;
  source_role?: string | null;
  transmission: Transmission;
  urls: TransmissionUrlsResponse;
  selected_output?: TransmissionOutputUrl | null;
  runtime_health?: StreamingRuntimeTransmissionHealth | Record<string, unknown> | null;
  source_health?: Record<string, unknown> | null;
  warnings?: string[];
  blocking_errors?: string[];
};

export type StreamingOutputRuntimeStatus = {
  output_key: string;
  output_id: string;
  transmission_id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  resolved_engine_path: string;
  viewer_count: number;
  demand_signal: boolean;
  publisher_running: boolean;
  publisher_pid?: number | null;
  publisher_frames_sent: number;
  publisher_last_error?: string | null;
  publisher_active_codec?: string | null;
  publisher_hardware_accelerated?: boolean;
  publisher_restart_count?: number;
  publisher_last_frame_at_unix?: number | null;
  publisher_encoder_mode?: "auto" | "cpu";
  publisher_encoder_state?: "candidate" | "trusted" | "quarantined";
  publisher_encoder_reason?: string | null;
  publisher_encoder_quarantined_until_unix?: number | null;
  publisher_encoder_fallback_active?: boolean;
  quality_profile_id?: StreamingQualityProfileId | null;
  resolution?: TransmissionResolution | null;
  fps_limit?: number | null;
  bitrate_kbps?: number | null;
  latency_profile?: StreamingLatencyProfile | null;
  status?: StreamingRuntimeStatus;
  active_writer_id?: string | null;
  selected_writer_id?: string | null;
  selected_frame_age_seconds?: number | null;
  last_incoming_frame_age_seconds?: number | null;
  last_live_frame_at_unix?: number | null;
  fallback_active?: boolean;
  fallback_reason?: StreamingFallbackReason | null;
  stale?: boolean;
  placeholder_active?: boolean;
  stream_behavior?: StreamingStreamBehavior;
  event_gated?: boolean;
  event_gated_idle?: boolean;
  event_gate_reasons?: string[];
  classification?: StreamingObservabilityClassification;
  evidence?: string[];
  active_playback_session_count?: number;
  last_playback_event_at_unix?: number | null;
  publisher_frames_sent_rate?: number | null;
  source_health?: StreamingRuntimeSourceHealth | null;
};

export type StreamingOutputsRuntimeResponse = {
  updated_at_unix: number;
  outputs: StreamingOutputRuntimeStatus[];
};

export type StreamingRuntimeStatus = "live" | "degraded" | "stale" | "offline";
export type StreamingStreamBehavior = "continuous" | "event_gated";
export type StreamingObservabilityClassification =
  | "healthy"
  | "demand_idle"
  | "source_stale"
  | "source_pipeline_stale"
  | "publisher_down"
  | "hls_playlist_stale"
  | "hls_tail_unavailable"
  | "webrtc_transport_error"
  | "network_contract_error"
  | "auth_url_error"
  | "app_player_lifecycle"
  | "event_gated_idle"
  | "unknown";

export type StreamingRuntimeSourceHealth = {
  source_id: string;
  camera_id?: string | null;
  camera_source_id?: string | null;
  camera_source_name?: string | null;
  camera_name?: string | null;
  pipeline_name?: string | null;
  node_id?: string | null;
  backend?: string | null;
  configured_backend?: string;
  source_frame_age_seconds?: number | null;
  capture_fps?: number | null;
  target_fps?: number | null;
  opened?: boolean;
  restarts_total?: number;
  decode_failures?: number;
  frames_captured?: number;
  last_frame_at_unix?: number | null;
  last_seen_at_unix?: number | null;
  last_error?: string | null;
  rtsp_transport?: string;
  used_ingest?: boolean;
  ingest_mode?: "centralized" | "runtime_local" | "direct";
  centralizer_server_id?: string | null;
  ingest_path?: string | null;
  ingest_warnings?: string[];
  ingest_blocking_errors?: string[];
  status?: "healthy" | "starting" | "stale" | "unreachable" | "unauthorized" | "error" | "idle" | "unknown";
  recommended_action?: string;
};

export type StreamingFallbackReason =
  | "no_active_writer"
  | "selected_writer_missing_frame"
  | "no_frame";

export type StreamingRuntimeOutputHealth = {
  transmission_id: string;
  output_key: string;
  output_id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  resolved_engine_path: string;
  viewer_count: number;
  demand_signal: boolean;
  publisher_running: boolean;
  publisher_pid?: number | null;
  publisher_frames_sent: number;
  publisher_last_error?: string | null;
  publisher_active_codec?: string | null;
  publisher_hardware_accelerated?: boolean;
  publisher_restart_count?: number;
  publisher_last_frame_at_unix?: number | null;
  publisher_encoder_mode?: "auto" | "cpu";
  publisher_encoder_state?: "candidate" | "trusted" | "quarantined";
  publisher_encoder_reason?: string | null;
  publisher_encoder_quarantined_until_unix?: number | null;
  publisher_encoder_fallback_active?: boolean;
  quality_profile_id?: StreamingQualityProfileId | null;
  resolution?: TransmissionResolution | null;
  fps_limit?: number | null;
  bitrate_kbps?: number | null;
  latency_profile?: StreamingLatencyProfile | null;
  status: StreamingRuntimeStatus;
  stream_behavior?: StreamingStreamBehavior;
  event_gated?: boolean;
  event_gated_idle?: boolean;
  event_gate_reasons?: string[];
  demand_driven?: boolean;
  demand_idle?: boolean;
  classification?: StreamingObservabilityClassification;
  evidence?: string[];
  active_playback_session_count?: number;
  last_playback_event_at_unix?: number | null;
  publisher_frames_sent_rate?: number | null;
  source_health?: StreamingRuntimeSourceHealth | null;
};

export type StreamingRuntimeTransmissionHealth = {
  transmission_id: string;
  enabled?: boolean;
  active_writer_id?: string | null;
  selected_writer_id?: string | null;
  selected_frame_age_seconds?: number | null;
  last_incoming_frame_age_seconds?: number | null;
  last_live_frame_at_unix?: number | null;
  fallback_active: boolean;
  fallback_reason?: StreamingFallbackReason | null;
  stale: boolean;
  placeholder_active: boolean;
  status: StreamingRuntimeStatus;
  stream_behavior?: StreamingStreamBehavior;
  event_gated?: boolean;
  event_gated_idle?: boolean;
  event_gate_reasons?: string[];
  demand_driven?: boolean;
  demand_idle?: boolean;
  classification?: StreamingObservabilityClassification;
  evidence?: string[];
  active_playback_session_count?: number;
  last_playback_event_at_unix?: number | null;
  source_health?: StreamingRuntimeSourceHealth | null;
  outputs: StreamingRuntimeOutputHealth[];
};

export type StreamingRuntimeHealthResponse = {
  updated_at_unix: number;
  stale_after_seconds: number;
  placeholder_after_seconds: number;
  transmissions: StreamingRuntimeTransmissionHealth[];
};

export type StreamingRuntimePipelineNode = {
  node_id: string;
  operator_id: string;
  upstream_to_publish?: boolean;
  stream_publish?: boolean;
};

export type StreamingRuntimePipelineEdge = {
  source_node_id: string;
  source_port?: string;
  target_node_id: string;
  target_port?: string;
};

export type StreamingRuntimePipelineLink = {
  transmission_id: string;
  pipeline_name: string;
  enabled?: boolean;
  processing_server_id?: string;
  publish_node_id: string;
  source_node_id?: string | null;
  source_id?: string | null;
  camera_id?: string | null;
  camera_source_id?: string | null;
  writer_id: string;
  stream_behavior?: StreamingStreamBehavior;
  event_gated?: boolean;
  event_gate_reasons?: string[];
  demand_driven?: boolean;
  warnings?: string[];
  nodes?: StreamingRuntimePipelineNode[];
  edges?: StreamingRuntimePipelineEdge[];
};

export type StreamingRuntimePipelinesResponse = {
  updated_at_unix: number;
  pipelines: StreamingRuntimePipelineLink[];
};

export type StreamingPlaybackSessionSummary = {
  playback_session_id: string;
  transmission_id: string;
  output_id?: string | null;
  client_kind: "app" | "web";
  platform: string;
  app_state?: string | null;
  pip_active?: boolean | null;
  first_event_at_unix: number;
  last_event_at_unix: number;
  last_type: string;
  last_severity: "debug" | "info" | "warn" | "error";
};

export type StreamingRuntimeObservabilityItem = {
  transmission_id: string;
  output_key?: string | null;
  output_id?: string | null;
  classification: StreamingObservabilityClassification;
  evidence?: string[];
  active_playback_sessions?: StreamingPlaybackSessionSummary[];
  last_playback_event_at_unix?: number | null;
  publisher_frames_sent_rate?: number | null;
  health: StreamingRuntimeTransmissionHealth | StreamingRuntimeOutputHealth;
  pipeline?: StreamingRuntimePipelineLink | null;
  mediamtx?: Record<string, unknown>;
  network_contract?: StreamingNetworkContract | null;
  recent_events?: Array<Record<string, unknown>>;
};

export type StreamingRuntimeObservabilityResponse = {
  updated_at_unix: number;
  retention_seconds: number;
  retained_event_count: number;
  mediamtx?: Record<string, unknown>;
  items: StreamingRuntimeObservabilityItem[];
};

export type StreamingRuntimeEncoderPolicyResponse = {
  mode?: "auto" | "cpu";
  quarantine_enabled?: boolean;
  quarantine_after_restarts?: number;
  quarantine_window_seconds?: number;
  quarantine_duration_seconds?: number;
  max_restarts_per_minute?: number;
};

export type StreamingRuntimeEncoderStateItem = {
  host_id: string;
  encoder: string;
  state: "candidate" | "trusted" | "quarantined";
  until_unix?: number | null;
  reason?: string | null;
  failure_count?: number;
  last_failure_at_unix?: number | null;
  last_output_id?: string | null;
  last_error?: string | null;
};

export type StreamingRuntimeEncoderOutputItem = {
  output_key: string;
  output_id: string;
  transmission_id: string;
  engine_path: string;
  running?: boolean;
  active_codec?: string | null;
  hardware_accelerated?: boolean;
  encoder_mode?: "auto" | "cpu";
  encoder_state?: "candidate" | "trusted" | "quarantined";
  encoder_reason?: string | null;
  encoder_quarantined_until_unix?: number | null;
  encoder_fallback_active?: boolean;
  restart_count?: number;
  restart_window_count?: number;
  frames_sent?: number;
  last_frame_at_unix?: number | null;
  last_error?: string | null;
  log_path?: string | null;
  stderr_tail?: string[];
};

export type StreamingRuntimeEncodersResponse = {
  updated_at_unix: number;
  host_id?: string;
  ffmpeg_path?: string | null;
  ffmpeg_source?: string | null;
  supported_encoders?: string[];
  policy?: StreamingRuntimeEncoderPolicyResponse;
  states?: StreamingRuntimeEncoderStateItem[];
  outputs?: StreamingRuntimeEncoderOutputItem[];
};

export type StreamingHlsProbeStatus =
  | "ok"
  | "engine_stopped"
  | "no_hls_output"
  | "playlist_unreachable"
  | "tail_unavailable"
  | "probe_error";

export type StreamingHlsProbeResponse = {
  transmission_id: string;
  output_id?: string | null;
  url?: string | null;
  media_playlist_url?: string | null;
  playlist_reachable: boolean;
  target_duration_seconds?: number | null;
  media_sequence?: number | null;
  tail_segment_url?: string | null;
  tail_segment_http_status?: number | null;
  tail_segment_reachable: boolean;
  sampled_at_unix: number;
  status: StreamingHlsProbeStatus;
  error?: string | null;
};

export type TransmissionDemandOutputStatus = {
  output_id: string;
  output_key: string;
  viewer_count: number;
};

export type TransmissionDemandResponse = {
  transmission_id: string;
  demand_signal: boolean;
  viewer_count_total: number;
  outputs: TransmissionDemandOutputStatus[];
};

export type StreamingPreferredPorts = {
  rtsp?: number;
  hls?: number;
  webrtc?: number;
  webrtc_udp?: number;
  api?: number;
  metrics?: number;
};

export type StreamingEngineSettings = {
  enabled?: boolean;
  expose_to_lan?: boolean;
  metrics_enabled?: boolean;
  encoder_policy?: StreamingRuntimeEncoderPolicyResponse;
  media_auth?: StreamingMediaAuthSettings;
  preferred_ports?: StreamingPreferredPorts;
  mediamtx_version?: string;
  webrtc_ice_servers?: string[];
  webrtc_additional_hosts?: string[];
};

export type StreamingMediaAuthSettings = {
  mode?: "signed_proxy" | "open";
  token_ttl_seconds?: number;
  renew_margin_seconds?: number;
};

export type StreamingStalePolicySettings = {
  stale_after_seconds?: number;
  placeholder_after_seconds?: number;
};

export type StreamingCameraIngestSettings = {
  enabled?: boolean;
  path_prefix?: string;
  allowed_cidrs?: string[];
};

export type StreamingExtensionSettings = {
  camera_live_views?: CameraLiveView[];
  transmissions?: Transmission[];
  publications?: StreamPublication[];
  engine?: StreamingEngineSettings;
  camera_ingest?: StreamingCameraIngestSettings;
  stale_policy?: StreamingStalePolicySettings;
};

export type StreamingCameraIngestAuthPath = {
  camera_id: string;
  path: string;
  redacted_rtsp_url: string;
  rtsp_url?: string | null;
};

export type StreamingCameraIngestAuthResponse = {
  enabled: boolean;
  credential_active: boolean;
  username: string;
  password?: string | null;
  created_at_unix?: number | null;
  rotated_at_unix?: number | null;
  rtsp_port?: number | null;
  allowed_cidrs?: string[];
  paths?: StreamingCameraIngestAuthPath[];
};

export type CameraIndexItem = {
  id: string;
  name?: string;
  control?: { type?: "onvif" | "none" | string };
  sources?: CameraIndexSource[];
};

export type CameraIndexSource = {
  id: string;
  name?: string;
  enabled?: boolean;
  is_default?: boolean;
  kind?: "video" | "audio" | "data" | string;
  role?: "main" | "sub" | "zoom" | "custom" | string;
  view_id?: string;
  origin?: {
    type?: "onvif_profile" | "rtsp" | string;
    rtsp_url?: string;
    profile_token?: string | null;
    profile_name?: string | null;
  };
  video?: {
    width?: number | null;
    height?: number | null;
    fps?: number | null;
    codec?: string | null;
  };
  ingest?: {
    mode?: "centralized" | "runtime_local" | "direct";
    host_server_id?: string;
  };
};

export type CameraIndexResponse = {
  cameras: CameraIndexItem[];
};

export type ProcessingServer = {
  id: string;
  name?: string;
  kind?: "inprocess" | "http";
  url?: string;
  username?: string;
  password?: string;
};

export type ProcessingServersListResponse = {
  servers: ProcessingServer[];
};

export type StreamingWizardPresetId =
  | "simple_stream"
  | "motion_gate_stream"
  | "detection_stream"
  | "tracking_stream"
  | "segmentation_stream";

export type StreamingWizardCreatePipelineRequest = {
  transmission_id: string;
  camera_id: string;
  camera_source_id?: string;
  preset_id: StreamingWizardPresetId;
  optional_parameters?: {
    pipeline_name?: string;
    enabled?: boolean;
    processing_server_id?: string;
    source_backend?: "auto" | "opencv" | "ffmpeg";
    stream_behavior?: StreamingStreamBehavior;
    use_fps_reducer?: boolean;
    fps_limit?: number;
    motion_sensitivity?: number;
    motion_hold_seconds?: number;
    resize_mode?: "contain" | "none";
    writer_priority?: number;
    bypass_mode?: "auto" | "force_on" | "force_off";
    yolo_confidence_threshold?: number;
    yolo_filter_enabled?: boolean;
    detection_categories?: string[];
    tracking_categories?: string[];
  };
};

export type StreamingWizardCreatePipelineResponse = {
  pipeline_name: string;
  transmission_id: string;
  camera_id: string;
  camera_source_id: string;
  preset_id: StreamingWizardPresetId;
  engine_running: boolean;
  warnings?: string[];
};
