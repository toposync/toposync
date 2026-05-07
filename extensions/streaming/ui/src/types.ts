export type StreamsHealthResponse = {
  status?: string;
  extension?: string;
};

export type EngineStatusResponse = {
  running?: boolean;
  pid?: number | null;
  uptime_seconds?: number | null;
  started_at_unix?: number | null;
  bind_host?: string;
  ports?: {
    rtsp?: number;
    hls?: number;
    webrtc?: number;
    api?: number;
  };
  test_path?: string;
  urls?: {
    rtsp_url?: string;
    hls_url?: string;
    webrtc_url?: string;
  };
  last_error?: string | null;
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
  latency_profile?: "normal" | "low" | "ultra_low";
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
  camera_controls?: { enabled?: boolean; camera_id?: string | null } | null;
  outputs: TransmissionOutput[];
  created_at?: string;
  updated_at?: string;
};

export type TransmissionOutputUrl = {
  output_id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  resolved_engine_path: string;
  url: string;
  requires_auth?: boolean;
  auth_username?: string | null;
};

export type TransmissionUrlsResponse = {
  transmission_id: string;
  engine_running: boolean;
  outputs: TransmissionOutputUrl[];
  warnings?: string[];
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
};

export type StreamingOutputsRuntimeResponse = {
  updated_at_unix: number;
  outputs: StreamingOutputRuntimeStatus[];
};

export type StreamingRuntimeStatus = "live" | "degraded" | "stale" | "offline";
export type StreamingStreamBehavior = "continuous" | "event_gated";

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
  status: StreamingRuntimeStatus;
  stream_behavior?: StreamingStreamBehavior;
  event_gated?: boolean;
  event_gated_idle?: boolean;
  event_gate_reasons?: string[];
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
  writer_id: string;
  stream_behavior?: StreamingStreamBehavior;
  event_gated?: boolean;
  event_gate_reasons?: string[];
  warnings?: string[];
  nodes?: StreamingRuntimePipelineNode[];
  edges?: StreamingRuntimePipelineEdge[];
};

export type StreamingRuntimePipelinesResponse = {
  updated_at_unix: number;
  pipelines: StreamingRuntimePipelineLink[];
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
  api?: number;
};

export type StreamingEngineSettings = {
  enabled?: boolean;
  expose_to_lan?: boolean;
  preferred_ports?: StreamingPreferredPorts;
  mediamtx_version?: string;
  webrtc_ice_servers?: string[];
};

export type StreamingStalePolicySettings = {
  stale_after_seconds?: number;
  placeholder_after_seconds?: number;
};

export type StreamingExtensionSettings = {
  transmissions?: Transmission[];
  engine?: StreamingEngineSettings;
  stale_policy?: StreamingStalePolicySettings;
};

export type CameraIndexItem = {
  id: string;
  name?: string;
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
  preset_id: StreamingWizardPresetId;
  engine_running: boolean;
  warnings?: string[];
};
