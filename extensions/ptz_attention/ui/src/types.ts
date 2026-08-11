export type AttentionExecutionMode = "disabled" | "shadow" | "live_preset" | "paused";
export type AttentionResumableMode = Exclude<AttentionExecutionMode, "paused">;

export type AttentionRuntimeState =
  | "IDLE"
  | "CANDIDATE"
  | "ACQUIRING"
  | "FOCUSED"
  | "GRACE"
  | "RETURNING"
  | "MANUAL_OVERRIDE"
  | "FAULT";

export type AttentionEventTypeCatalogItem = {
  event_type: string;
  profile_ids: string[];
};

export type AttentionBindingCatalogItem = {
  pipeline_name: string;
  node_id: string;
  profile_id: string;
  event_type: string;
  event_type_field: string;
  observer_camera_id?: string | null;
  enabled: boolean;
};

export type AttentionViewCatalogItem = {
  id: string;
  label: string;
  composition_id: string;
  composition_name: string;
  camera_element_id: string;
  preset_name: string;
  quality: string;
  pose_bound: boolean;
  compatible_source_ids: string[];
  compatible_roles: string[];
};

export type AttentionCameraCatalogItem = {
  id: string;
  name: string;
  enabled: boolean;
  actuator_id: string;
  control_source_id: string;
  automation_exclusive_control_confirmed: boolean | null;
  automation_ready_reason: string;
  permissions: {
    configure: boolean;
    control: boolean;
  };
  sources: Array<{
    id: string;
    name: string;
    role: string;
    kind: string;
    enabled: boolean;
    is_default: boolean;
    view_id: string;
    has_ptz: boolean;
  }>;
  views: AttentionViewCatalogItem[];
};

export type AttentionCatalogResponse = {
  generated_at: number;
  operator_id: string;
  modes: AttentionExecutionMode[];
  states: AttentionRuntimeState[];
  event_types: AttentionEventTypeCatalogItem[];
  cameras: AttentionCameraCatalogItem[];
  bindings: AttentionBindingCatalogItem[];
  permissions: {
    configure: boolean;
    control: boolean;
  };
  services: Record<string, boolean>;
  partial_errors: Array<{
    source: string;
    code: string;
    message: string;
  }>;
};

export type AttentionEventPolicy = {
  event_type: string;
  enabled: boolean;
  priority: number;
  preferred_view_id: string;
};

export type AttentionProfile = {
  id: string;
  name: string;
  mode: AttentionExecutionMode;
  resume_mode?: AttentionResumableMode | null;
  camera_id: string;
  source_id: string;
  ptz_device_id: string;
  same_head_observer_acknowledged: boolean;
  composition_id: string;
  home_view_id: string;
  eligible_view_ids: string[];
  event_policies: AttentionEventPolicy[];
  candidate_confirm_seconds: number;
  min_focus_seconds: number;
  max_focus_seconds: number;
  close_grace_seconds: number;
  cooldown_seconds: number;
  stale_timeout_seconds: number;
  settle_timeout_seconds: number;
  lease_ttl_seconds: number;
  max_movements_per_minute: number;
  minimum_target_confidence: number;
};

export type AttentionProfilesResponse = {
  profiles: AttentionProfile[];
};

export type AttentionReadinessIssue = {
  severity: "warning" | "error";
  code: string;
  message: string;
  blocking: boolean;
};

export type AttentionValidationResponse = {
  ok: boolean;
  profile_id: string;
  mode: AttentionExecutionMode;
  issues: AttentionReadinessIssue[];
  resolved_views: Record<string, {
    view_id?: string;
    confidence?: number;
    reason?: string;
  }>;
  automation_ready: boolean | null;
  automation_ready_reason: string;
  services: Record<string, boolean>;
};

export type AttentionSession = {
  ptz_device_id: string;
  profile_id: string;
  state: AttentionRuntimeState;
  state_since: number;
  active_event_key: string;
  active_view_id: string;
  active_priority?: number | null;
  candidate_event_key: string;
  candidate_view_id: string;
  pending_events: number;
  lease_active: boolean;
  focused_since?: number | null;
  grace_until?: number | null;
  cooldown_until?: number | null;
  last_heartbeat_at?: number | null;
  movements_last_minute: number;
  paused: boolean;
  fault: string;
};

export type AttentionStatusResponse = {
  generated_at: number;
  devices: AttentionSession[];
  services: Record<string, boolean>;
};

export type AttentionDecision = {
  seq: number;
  id: string;
  ptz_device_id: string;
  profile_id: string;
  event_key: string;
  pipeline_name: string;
  state: AttentionRuntimeState;
  action: string;
  reason: string;
  priority: number;
  created_at: number;
  details: Record<string, unknown>;
};

export type AttentionDecisionsResponse = {
  decisions: AttentionDecision[];
  next_cursor?: number | null;
};

export type AttentionCommand = "pause" | "resume" | "return-home";

export type AttentionRequestOperatorConfig = {
  profile_id: string;
  camera_id: string;
  source_id: string;
  composition_id: string;
  priority: number;
  hold_after_close_seconds: number;
  native_tracking_disabled_confirmed: boolean;
  event_type: string;
  event_type_field: string;
  event_id_field: string;
  world_envelope_field: string;
  world_anchor_field: string;
  bbox01_field: string;
  preferred_view_id_field: string;
};
