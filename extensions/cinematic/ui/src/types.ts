export type CameraMode = "all" | "include" | "exclude";
export type Priority = "low" | "medium" | "high";
export type ResizeMode = "contain" | "none";
export type SourceRole = "auto" | "main" | "sub" | "zoom";

export type Transmission = {
  id: string;
  name?: string;
  path?: string;
  enabled?: boolean;
  host_server_id?: string;
};

export type CameraIndexItem = {
  id: string;
  name?: string;
  sources?: Array<{ id?: string; name?: string; enabled?: boolean; kind?: string; role?: string }>;
};

export type CinematicStatusItem = {
  key: string;
  pipeline_name: string;
  node_id: string;
  updated_at: number;
  demand_active?: boolean;
  stream_open?: boolean;
  lifecycle?: string;
  mode?: string;
  cut_reason?: string;
  active_camera_id?: string | null;
  active_source_id?: string | null;
  pending_camera_id?: string | null;
  active_event_key?: string | null;
  frame_ts?: number | null;
  frame_age_seconds?: number | null;
  frame_width?: number | null;
  frame_height?: number | null;
  active_events?: number;
  last_error?: string;
};

export type CinematicStatusResponse = {
  generated_at: number;
  items: CinematicStatusItem[];
};

export type CinematicDiagnosticsResponse = {
  ok: boolean;
  generated_at: number;
  operators?: Record<string, boolean>;
  services?: Record<string, boolean>;
  counts?: Record<string, number>;
  issues?: Array<{ severity: "info" | "warning" | "error"; code: string; message: string }>;
};

export type CinematicWizardCreatePipelineRequest = {
  transmission_id: string;
  optional_parameters?: {
    pipeline_name?: string;
    enabled?: boolean;
    processing_server_id?: string;
    cameras_mode?: CameraMode;
    camera_ids?: string[];
    priority_filter?: Priority[];
    pipeline_camera_map?: Record<string, string>;
    preferred_source_role?: SourceRole;
    idle_dwell_seconds?: number;
    event_min_seconds?: number;
    cut_cooldown_seconds?: number;
    close_hold_seconds?: number;
    current_camera_sticky_seconds?: number;
    max_event_hold_seconds?: number;
    max_cuts_per_minute?: number;
    fps?: number;
    width?: number;
    height?: number;
    handoff_timeout_seconds?: number;
    stale_frame_max_age_seconds?: number;
    resize_mode?: ResizeMode;
    writer_priority?: number;
    demand_gate_output_id?: string;
    demand_gate_quality_profile_id?: string;
  };
};

export type CinematicWizardCreatePipelineResponse = {
  pipeline_name: string;
  transmission_id: string;
  cameras_mode: CameraMode;
  camera_ids: string[];
  processing_server_id: string;
  engine_running: boolean;
  warnings?: string[];
};
