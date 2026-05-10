import type { StylesConfig } from "react-select";

import type { SelectOption } from "./types";

export const PIPELINE_PRESET_OPERATOR_IDS = [
  "camera.source",
  "core.schedule_gate",
  "camera.motion_bgsub_adaptive",
  "camera.motion_sample_bg",
  "camera.motion_gate",
  "core.lifecycle_from_boolean",
  "core.fps_reducer",
  "camera.image_crop",
  "camera.artifact_privacy",
  "camera.image_privacy",
  "camera.image_perspective_crop",
  "camera.local_contrast_clahe",
  "camera.unsharp_mask",
  "camera.denoise_luma",
  "camera.auto_gamma",
  "camera.global_stabilize",
  "vision.track",
  "vision.classify_image",
  "vision.detect",
  "vision.segment_instances",
  "vision.crop_objects",
  "core.category_gate",
  "core.filter",
  "camera.camera_mapping",
  "camera.area_restriction",
  "camera.velocity_estimation",
  "camera.image_adjust",
  "camera.image_resize",
  "core.throttle",
  "core.velocity_throttle",
  "core.debounce",
  "core.debug",
  "core.store_images",
  "core.notify",
  "home_assistant.notify",
  "stream.publish_video",
];

export const PIPELINE_OPERATOR_GROUPS = {
  input: {
    color: "#2563EB",
    labelKey: "core.ui.pipelines.group.input",
  },
  motion: {
    color: "#EA580C",
    labelKey: "core.ui.pipelines.group.motion",
  },
  image: {
    color: "#7C3AED",
    labelKey: "core.ui.pipelines.group.image",
  },
  privacy: {
    color: "#BE123C",
    labelKey: "core.ui.pipelines.group.privacy",
  },
  vision: {
    color: "#0891B2",
    labelKey: "core.ui.pipelines.group.vision",
  },
  rules: {
    color: "#CA8A04",
    labelKey: "core.ui.pipelines.group.rules",
  },
  space: {
    color: "#16A34A",
    labelKey: "core.ui.pipelines.group.space",
  },
  rate: {
    color: "#DB2777",
    labelKey: "core.ui.pipelines.group.rate",
  },
  output: {
    color: "#0D9488",
    labelKey: "core.ui.pipelines.group.output",
  },
  diagnostics: {
    color: "#64748B",
    labelKey: "core.ui.pipelines.group.diagnostics",
  },
  extensions: {
    color: "#4F46E5",
    labelKey: "core.ui.pipelines.group.extensions",
  },
} as const;

export type PipelineOperatorGroupId = keyof typeof PIPELINE_OPERATOR_GROUPS;
export type PipelineOperatorLevel = "basic" | "advanced";

export type PipelineOperatorUxMetadata = {
  group: PipelineOperatorGroupId;
  level: PipelineOperatorLevel;
  order: number;
  aliases?: string[];
};

export const PIPELINE_OPERATOR_GROUP_ORDER = Object.keys(PIPELINE_OPERATOR_GROUPS) as PipelineOperatorGroupId[];

export const PIPELINE_OPERATOR_UX = {
  "camera.source": { group: "input", level: "basic", order: 10 },
  "core.schedule_gate": { group: "input", level: "basic", order: 20 },
  "core.source": { group: "input", level: "advanced", order: 900 },
  "core.synthetic_source": { group: "input", level: "advanced", order: 910 },
  "core.demo_frame_sequence_source": { group: "input", level: "advanced", order: 920 },
  "dist.remote_source": { group: "input", level: "advanced", order: 930 },

  "camera.motion_bgsub_adaptive": { group: "motion", level: "basic", order: 10 },
  "camera.motion_sample_bg": { group: "motion", level: "advanced", order: 20 },
  "camera.motion_gate": { group: "motion", level: "basic", order: 30 },

  "camera.frame_attach": { group: "image", level: "advanced", order: 5 },
  "core.fps_reducer": { group: "image", level: "advanced", order: 10 },
  "camera.image_crop": { group: "image", level: "basic", order: 20 },
  "camera.image_perspective_crop": { group: "image", level: "advanced", order: 30 },
  "camera.local_contrast_clahe": { group: "image", level: "advanced", order: 40 },
  "camera.unsharp_mask": { group: "image", level: "advanced", order: 50 },
  "camera.denoise_luma": { group: "image", level: "advanced", order: 60 },
  "camera.auto_gamma": { group: "image", level: "advanced", order: 70 },
  "camera.global_stabilize": { group: "image", level: "advanced", order: 80 },
  "camera.image_adjust": { group: "image", level: "advanced", order: 90 },
  "camera.image_resize": { group: "image", level: "advanced", order: 100 },
  "camera.lens_undistort": { group: "image", level: "advanced", order: 110 },

  "camera.artifact_privacy": { group: "privacy", level: "advanced", order: 10 },
  "camera.image_privacy": { group: "privacy", level: "basic", order: 20 },

  "vision.detect": { group: "vision", level: "basic", order: 10 },
  "vision.track": { group: "vision", level: "basic", order: 20 },
  "vision.classify_image": { group: "vision", level: "advanced", order: 30 },
  "vision.segment_instances": { group: "vision", level: "advanced", order: 40 },
  "vision.crop_objects": { group: "vision", level: "basic", order: 50 },
  "ai.condition_filter": { group: "vision", level: "basic", order: 60 },
  "ai.smart_crop": { group: "vision", level: "advanced", order: 70 },
  "vision.pose_estimate": { group: "vision", level: "advanced", order: 80 },

  "core.category_gate": { group: "rules", level: "basic", order: 10 },
  "core.filter": { group: "rules", level: "advanced", order: 20 },
  "core.lifecycle_from_boolean": { group: "rules", level: "advanced", order: 30 },
  "core.passthrough": { group: "rules", level: "advanced", order: 900 },
  "dist.target_filter": { group: "rules", level: "advanced", order: 910 },

  "camera.camera_mapping": { group: "space", level: "advanced", order: 10 },
  "camera.area_restriction": { group: "space", level: "basic", order: 20 },
  "camera.velocity_estimation": { group: "space", level: "advanced", order: 30 },

  "core.throttle": { group: "rate", level: "advanced", order: 10 },
  "core.velocity_throttle": { group: "rate", level: "advanced", order: 20 },
  "core.debounce": { group: "rate", level: "advanced", order: 30 },

  "core.store_images": { group: "output", level: "basic", order: 10 },
  "core.notify": { group: "output", level: "basic", order: 20 },
  "home_assistant.notify": { group: "output", level: "basic", order: 30 },
  "stream.publish_video": { group: "output", level: "basic", order: 40 },
  "core.sink": { group: "output", level: "advanced", order: 900 },
  "dist.project_to_origin": { group: "output", level: "advanced", order: 910 },

  "core.debug": { group: "diagnostics", level: "advanced", order: 10 },
  "core.stream_state_snapshot": { group: "diagnostics", level: "advanced", order: 20 },
} satisfies Record<string, PipelineOperatorUxMetadata>;

export const NODE_ID_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

export const OPERATOR_FRIENDLY_NAMES: Record<string, string> = {
  "core.source": "Start flow",
  "core.synthetic_source": "Use synthetic source",
  "core.demo_frame_sequence_source": "Use demo frames",
  "core.schedule_gate": "Limit by schedule",
  "camera.source": "Use camera",
  "camera.motion_bgsub_adaptive": "Filter motion with the default detector",
  "camera.motion_sample_bg": "Filter motion with samples",
  "camera.motion_gate": "Filter by motion",
  "core.lifecycle_from_boolean": "Create event from condition",
  "core.fps_reducer": "Reduce frames",
  "camera.frame_attach": "Attach frame",
  "camera.image_crop": "Crop image",
  "camera.artifact_privacy": "Remove sensitive images",
  "camera.image_privacy": "Hide image area",
  "camera.image_perspective_crop": "Correct perspective",
  "camera.image_adjust": "Adjust image",
  "camera.local_contrast_clahe": "Improve contrast",
  "camera.unsharp_mask": "Sharpen image",
  "camera.denoise_luma": "Reduce noise",
  "camera.auto_gamma": "Correct brightness",
  "camera.global_stabilize": "Stabilize image",
  "camera.lens_undistort": "Correct lens distortion",
  "vision.track": "Track objects",
  "vision.classify_image": "Classify scene",
  "vision.detect": "Detect objects",
  "vision.segment_instances": "Separate objects from scene",
  "vision.crop_objects": "Crop objects",
  "vision.pose_estimate": "Estimate pose",
  "ai.condition_filter": "Filter by visual condition",
  "ai.smart_crop": "Crop with AI",
  "core.category_gate": "Filter by category",
  "core.filter": "Apply rule",
  "camera.camera_mapping": "Map position in space",
  "camera.area_restriction": "Filter by area",
  "camera.velocity_estimation": "Calculate speed",
  "camera.image_resize": "Resize image",
  "core.throttle": "Limit frequency",
  "core.velocity_throttle": "Reduce sends when stopped",
  "core.debounce": "Avoid repetition",
  "core.debug": "Inspect flow",
  "core.stream_state_snapshot": "Capture stream state",
  "core.passthrough": "Pass through flow",
  "core.sink": "End flow",
  "dist.remote_source": "Receive remote packets",
  "dist.target_filter": "Route remote packets",
  "dist.project_to_origin": "Send to origin",
  "core.store_images": "Save images",
  "core.notify": "Send notification",
  "home_assistant.notify": "Notify through Home Assistant",
  "stream.publish_video": "Publish video",
};

export const pipelinesReactSelectStyles: StylesConfig<SelectOption, boolean> = {
  container: (base) => ({ ...base }),
  control: (base, state) => ({
    ...base,
    minHeight: 40,
    borderRadius: "var(--radius-control)",
    border: `1px solid ${state.isFocused ? "var(--color-accent-border)" : "var(--color-border-subtle)"}`,
    backgroundColor: "var(--color-surface-frost)",
    boxShadow: state.isFocused
      ? "0 0 0 2px var(--color-focus-ring-inner), 0 0 0 4px var(--color-focus-ring-outer)"
      : "inset 0 1px 0 color-mix(in srgb, var(--color-surface-solid) 28%, transparent)",
    cursor: "text",
    backdropFilter: "blur(var(--frost-blur-small)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    WebkitBackdropFilter: "blur(var(--frost-blur-small)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    transition: "border-color var(--motion-medium) var(--ease-standard), box-shadow var(--motion-medium) var(--ease-standard)",
  }),
  menu: (base) => ({
    ...base,
    backgroundColor: "var(--color-surface-frost-strong)",
    border: "1px solid var(--color-border-subtle)",
    borderRadius: "var(--radius-panel)",
    overflow: "hidden",
    boxShadow: "var(--shadow-elevation-3)",
    backdropFilter: "blur(var(--frost-blur-large)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    WebkitBackdropFilter: "blur(var(--frost-blur-large)) saturate(var(--frost-saturate)) brightness(var(--frost-brightness))",
    zIndex: 50,
  }),
  menuList: (base) => ({
    ...base,
    paddingTop: 4,
    paddingBottom: 4,
  }),
  option: (base, state) => ({
    ...base,
    padding: "10px 12px",
    backgroundColor: "transparent",
    background: state.isSelected
      ? "linear-gradient(135deg, var(--color-accent-background-strong), var(--color-accent-background-strong-2))"
      : state.isFocused
        ? "linear-gradient(135deg, var(--color-accent-background-soft), var(--color-accent-background-soft-2))"
        : "transparent",
    color: "var(--color-text-primary)",
    cursor: "pointer",
    borderBottom: "1px solid color-mix(in srgb, var(--color-border-subtle) 35%, transparent)",
  }),
  multiValue: (base) => ({
    ...base,
    backgroundColor: "transparent",
    background: "linear-gradient(135deg, var(--color-accent-background-soft), var(--color-accent-background-soft-2))",
    border: "1px solid var(--color-accent-border)",
    borderRadius: "var(--radius-pill)",
  }),
  multiValueLabel: (base) => ({ ...base, color: "var(--color-text-primary)", fontWeight: 650 }),
  multiValueRemove: (base, state) => ({
    ...base,
    color: "var(--color-text-muted)",
    backgroundColor: state.isFocused ? "var(--color-accent-background-soft)" : "transparent",
  }),
  input: (base) => ({ ...base, color: "var(--color-text-primary)" }),
  placeholder: (base) => ({ ...base, color: "var(--color-text-subtle)" }),
  singleValue: (base) => ({ ...base, color: "var(--color-text-primary)" }),
  indicatorSeparator: (base) => ({ ...base, backgroundColor: "var(--color-border-subtle)" }),
  dropdownIndicator: (base) => ({ ...base, color: "var(--color-text-muted)" }),
  clearIndicator: (base) => ({ ...base, color: "var(--color-text-muted)" }),
};

const YOLO_CATEGORY_VALUES = [
  "person",
  "bicycle",
  "car",
  "motorcycle",
  "airplane",
  "bus",
  "train",
  "truck",
  "boat",
  "traffic light",
  "fire hydrant",
  "stop sign",
  "parking meter",
  "bench",
  "bird",
  "cat",
  "dog",
  "horse",
  "sheep",
  "cow",
  "elephant",
  "bear",
  "zebra",
  "giraffe",
  "backpack",
  "umbrella",
  "handbag",
  "tie",
  "suitcase",
  "frisbee",
  "skis",
  "snowboard",
  "sports ball",
  "kite",
  "baseball bat",
  "baseball glove",
  "skateboard",
  "surfboard",
  "tennis racket",
  "bottle",
  "wine glass",
  "cup",
  "fork",
  "knife",
  "spoon",
  "bowl",
  "banana",
  "apple",
  "sandwich",
  "orange",
  "broccoli",
  "carrot",
  "hot dog",
  "pizza",
  "donut",
  "cake",
  "chair",
  "couch",
  "potted plant",
  "bed",
  "dining table",
  "toilet",
  "tv",
  "laptop",
  "mouse",
  "remote",
  "keyboard",
  "cell phone",
  "microwave",
  "oven",
  "toaster",
  "sink",
  "refrigerator",
  "book",
  "clock",
  "vase",
  "scissors",
  "teddy bear",
  "hair drier",
  "toothbrush",
];

export const YOLO_CATEGORY_OPTIONS: SelectOption[] = YOLO_CATEGORY_VALUES.map((value) => ({ value, label: value }));

type TranslateFn = (key: string, params?: Record<string, unknown>, fallback?: string) => string;

const ARTIFACT_SUGGESTION_DEFS: Array<{ value: string; labelKey: string; fallback: string }> = [
  { value: "main", labelKey: "core.ui.pipelines.artifacts.main", fallback: "Main" },
  { value: "face", labelKey: "core.ui.pipelines.artifacts.face", fallback: "Face" },
  { value: "pose", labelKey: "core.ui.pipelines.artifacts.pose", fallback: "Pose" },
];

export function buildArtifactSuggestions(t: TranslateFn): SelectOption[] {
  return ARTIFACT_SUGGESTION_DEFS.map((item) => ({ value: item.value, label: t(item.labelKey, {}, item.fallback) }));
}

const PACKET_ARTIFACT_SUGGESTION_DEFS: Array<{ value: string; labelKey: string; fallback: string }> = [
  { value: "main", labelKey: "core.ui.pipelines.artifacts.main", fallback: "Main" },
  { value: "face", labelKey: "core.ui.pipelines.artifacts.face", fallback: "Face" },
  { value: "pose", labelKey: "core.ui.pipelines.artifacts.pose", fallback: "Pose" },
  { value: "mask", labelKey: "", fallback: "Mask" },
];

export function buildPacketArtifactSuggestions(t: TranslateFn): SelectOption[] {
  return PACKET_ARTIFACT_SUGGESTION_DEFS.map((item) => ({
    value: item.value,
    label: item.labelKey ? t(item.labelKey, {}, item.fallback) : item.fallback,
  }));
}

const WEEKDAY_DEFS: Array<{ value: string; labelKey: string; fallback: string }> = [
  { value: "mon", labelKey: "core.ui.pipelines.weekday.mon", fallback: "Mon" },
  { value: "tue", labelKey: "core.ui.pipelines.weekday.tue", fallback: "Tue" },
  { value: "wed", labelKey: "core.ui.pipelines.weekday.wed", fallback: "Wed" },
  { value: "thu", labelKey: "core.ui.pipelines.weekday.thu", fallback: "Thu" },
  { value: "fri", labelKey: "core.ui.pipelines.weekday.fri", fallback: "Fri" },
  { value: "sat", labelKey: "core.ui.pipelines.weekday.sat", fallback: "Sat" },
  { value: "sun", labelKey: "core.ui.pipelines.weekday.sun", fallback: "Sun" },
];

export function buildScheduleWeekdayOptions(t: TranslateFn): SelectOption[] {
  return WEEKDAY_DEFS.map((item) => ({ value: item.value, label: t(item.labelKey, {}, item.fallback) }));
}

export const HUMANIZE_ACRONYMS: Record<string, string> = {
  id: "ID",
  fps: "FPS",
  rtsp: "RTSP",
  url: "URL",
  jpeg: "JPEG",
  png: "PNG",
  yolo: "YOLO",
  api: "API",
  ui: "UI",
  sse: "SSE",
  ts: "TS",
};
