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

export const NODE_ID_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

export const OPERATOR_FRIENDLY_NAMES: Record<string, string> = {
  "core.schedule_gate": "Schedule gate",
  "camera.source": "Camera source",
  "camera.motion_bgsub_adaptive": "Adaptive motion detector",
  "camera.motion_sample_bg": "Sample-based motion detector",
  "camera.motion_gate": "Motion gate",
  "core.lifecycle_from_boolean": "Lifecycle from state",
  "core.fps_reducer": "Frame rate reducer",
  "camera.image_crop": "Frame crop",
  "camera.artifact_privacy": "Image privacy guard",
  "camera.image_privacy": "Privacy region",
  "camera.image_perspective_crop": "Perspective crop",
  "camera.image_adjust": "Image adjustment",
  "camera.local_contrast_clahe": "Local contrast",
  "camera.unsharp_mask": "Sharpen image",
  "camera.denoise_luma": "Reduce luma noise",
  "camera.auto_gamma": "Auto gamma",
  "camera.global_stabilize": "Frame stabilization",
  "vision.track": "Object tracking",
  "vision.classify_image": "Image classification",
  "vision.detect": "Object detection",
  "vision.segment_instances": "Instance segmentation",
  "vision.crop_objects": "Object crop",
  "vision.pose_estimate": "Pose estimation",
  "core.category_gate": "Category filter",
  "core.filter": "Rule filter",
  "camera.camera_mapping": "World mapping",
  "camera.area_restriction": "Area restriction",
  "camera.velocity_estimation": "Speed estimation",
  "camera.image_resize": "Resize images",
  "core.throttle": "Rate limit",
  "core.velocity_throttle": "Speed-aware rate limit",
  "core.debounce": "Debounce",
  "core.debug": "Debug tap",
  "core.store_images": "Store images",
  "core.notify": "Send notification",
  "home_assistant.notify": "Home Assistant push",
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
