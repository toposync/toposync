import { useSyncExternalStore } from "react";

export type Locale = "en" | "pt-BR";

export type Translations = Record<string, string>;
export type TranslationBundle = Partial<Record<Locale, Translations>>;

export type LocalizedString =
  | string
  | {
      key: string;
      params?: Record<string, unknown>;
      fallback?: string;
    };

type I18nApi = {
  getLocale: () => Locale;
  setLocale: (locale: Locale) => void;
  subscribe: (listener: () => void) => () => void;
  registerTranslations: (bundle: TranslationBundle) => void;
  t: (key: string, params?: Record<string, unknown>, fallback?: string) => string;
  useI18n: () => { locale: Locale; t: I18nApi["t"]; setLocale: I18nApi["setLocale"] };
};

const STORAGE_KEY = "toposync.locale";

const translationsByLocale: Record<Locale, Translations> = {
  en: {
    "core.actions.add": "Add",
    "core.actions.apply": "Apply",
    "core.actions.back": "Back",
    "core.actions.cancel": "Cancel",
    "core.actions.close": "Close",
    "core.actions.delete": "Delete",
    "core.actions.edit": "Edit",
    "core.actions.rename": "Rename",
    "core.actions.save": "Save",

    "core.ui.rendering": "Rendering",
    "core.ui.composition": "Composition",
    "core.ui.notifications": "Notifications",
    "core.ui.notifications.aria_open": "Open notifications",
    "core.ui.notifications.aria_close": "Close notifications",
    "core.ui.notifications.show_low": "Show low priority",
    "core.ui.notifications.hide_low": "Hide low priority",
    "core.ui.notifications.low_hidden": "{{count}} low priority notifications hidden.",
    "core.ui.layers": "Layers",
    "core.ui.add": "Add",
    "core.ui.tools": "Tools",
    "core.ui.action": "Action",
    "core.ui.view_settings.title": "View",
    "core.ui.view_settings.aria": "View settings",
    "core.ui.view_settings.wall_height": "Wall height",
    "core.ui.view_settings.interactivity": "Interactivity",
    "core.ui.view_settings.ghost_walls": "Ghost walls",
    "core.ui.view_settings.ghost_walls_desc": "Make walls semi-transparent and allow clicking elements through them.",
    "core.ui.view_settings.graphics_quality": "Graphics",
    "core.ui.graphics_quality.simplified": "Simplified",
    "core.ui.graphics_quality.simplified_desc": "Better performance. Lighter effects.",
    "core.ui.graphics_quality.detailed": "Detailed",
    "core.ui.graphics_quality.detailed_desc": "More detail and effects. Higher GPU/CPU usage.",
    "core.ui.settings.title": "Settings",
    "core.ui.settings.aria": "Settings",
    "core.ui.settings.sections.view": "View options",
    "core.ui.settings.sections.view_desc": "Walls, interactivity and graphics.",
    "core.ui.settings.sections.core": "Core",
    "core.ui.settings.sections.core_desc": "Language and general preferences.",
    "core.ui.settings.no_extensions": "No extension settings yet.",
    "core.ui.settings.backend_offline_title": "Backend offline",
    "core.ui.settings.backend_offline_desc": "Settings changes won't be persisted until the backend is running.",
    "core.ui.settings.language": "Language",
    "core.ui.settings.language.pt": "Português (Brasil)",
    "core.ui.settings.language.pt_desc": "Portuguese interface.",
    "core.ui.settings.language.en": "English",
    "core.ui.settings.language.en_desc": "English interface.",
    "core.ui.settings.theme": "Theme",
    "core.ui.settings.theme.default": "Default",
    "core.ui.settings.theme.default_desc": "Toposync default theme.",
    "core.ui.settings.save_changes": "Save changes",
    "core.ui.settings.save_all_changes": "Save all changes",
    "core.ui.settings.discard_changes": "Discard changes",
    "core.ui.settings.discard_and_close": "Discard and close",
    "core.ui.settings.changes_saved": "Saved",
    "core.ui.settings.unsaved_changes": "Unsaved changes",
    "core.ui.settings.unsaved_changes_in": "Unsaved: {{sections}}",
    "core.ui.settings.saving": "Saving…",
    "core.ui.settings.confirm_discard_title": "Discard changes?",
    "core.ui.settings.confirm_discard_desc": "Discard all your pending changes in Settings?",
    "core.ui.settings.confirm_close_title": "Discard and close?",
    "core.ui.settings.confirm_close_desc": "You have unsaved changes. Discard them and close Settings?",
    "core.ui.settings.confirm_open_pipelines_title": "Discard changes and open Pipelines?",
    "core.ui.settings.confirm_open_processing_servers_title": "Discard changes and open Processing servers?",
    "core.ui.settings.confirm_discard_continue": "Discard and continue",
    "core.ui.settings.confirm_discard_continue_desc": "You have unsaved settings{{suffix}}. Discard them and continue?",

    "core.ui.wall_height.low": "Low",
    "core.ui.wall_height.low_desc": "Low walls for quick overview.",
    "core.ui.wall_height.medium": "Medium",
    "core.ui.wall_height.medium_desc": "Medium height for planning.",
    "core.ui.wall_height.high": "High",
    "core.ui.wall_height.high_desc": "Full height (typical wall).",

    "core.ui.layers_group_walls": "Walls",
    "core.ui.layers_group_areas": "Areas",
    "core.ui.layers_group_background": "Background",
    "core.ui.layers.hide": "Hide layer",
    "core.ui.layers.show": "Show layer",
    "core.ui.layers.lock": "Lock movement",
    "core.ui.layers.unlock": "Unlock movement",
    "core.ui.layers.reorder": "Reorder layer",

    "core.tools.navigate": "Navigate",
    "core.tools.navigate_desc": "Pan around the canvas.",
    "core.tools.select": "Select",
    "core.tools.select_desc": "Select and move elements.",

    "core.ui.empty_title": "Nothing configured yet",
    "core.ui.empty_desc": "Click “Edit” to add elements to the composition.",
    "core.ui.notifications_empty": "No notifications yet.",
    "core.ui.image_preview": "Image",
    "core.ui.loading": "Loading…",
    "core.ui.error": "Error",
    "core.ui.element_types_empty": "No extensions have registered elements yet.",
    "core.ui.layers_empty": "No elements added yet.",

    "core.ui.render_modal.title": "Rendering",
    "core.ui.render_modal.option_3d.title": "3D (ThreeJS)",
    "core.ui.render_modal.option_3d.desc": "Interactive 3D view.",
    "core.ui.render_modal.option_2d.title": "2D (Snapshot)",
    "core.ui.render_modal.option_2d.desc": "Top-down snapshot with Home Assistant overlays.",

    "core.ui.main2d.cluster.title": "Multiple items ({{count}})",
    "core.ui.main2d.cluster.tooltip": "{{count}} items",

    "core.ui.action_unavailable": "No actions available for this element.",

    "core.compositions.modal.title": "Compositions",
    "core.compositions.section.new": "New composition",
    "core.compositions.section.list": "Your compositions",
    "core.compositions.new.placeholder": "Name (e.g. Ground, Upstairs...)",
    "core.compositions.delete_confirm": "Delete “{{name}}”?",
    "core.compositions.cannot_delete_last": "You can’t delete the last composition",
    "core.compositions.aria.create": "Create composition",
    "core.compositions.aria.save_name": "Save name",
    "core.compositions.aria.cancel": "Cancel",
    "core.compositions.aria.rename": "Rename composition",
    "core.compositions.aria.delete": "Delete composition",
    "core.compositions.aria.cancel_delete": "Cancel delete",
    "core.compositions.aria.confirm_delete": "Confirm delete",
    "core.compositions.error.create": "Failed to create composition",
    "core.compositions.error.activate": "Failed to switch composition",
    "core.compositions.error.rename": "Failed to rename composition",
    "core.compositions.error.delete": "Failed to delete composition",

    "core.element_editor.title": "Edit element",
    "core.element_editor.name": "Name",
    "core.element_editor.pos_x": "Position X",
    "core.element_editor.pos_y": "Position Y",
    "core.element_editor.pos_z": "Position Z",
    "core.element_editor.rot_x": "Rotation X (degrees)",
    "core.element_editor.rot_y": "Rotation Y (degrees)",
    "core.element_editor.rot_z": "Rotation Z (degrees)",
    "core.element_editor.delete": "Delete element",

    "core.modal.aria.close": "Close",

    "core.ui.pipelines.title": "Pipelines",
    "core.ui.pipelines.aria.toggle_list": "Toggle pipelines list",
    "core.ui.pipelines.aria.close_list": "Close pipelines list",
    "core.ui.pipelines.create.placeholder_name": "Pipeline name",
    "core.ui.pipelines.create.button": "Create",
    "core.ui.pipelines.type.final": "final",
    "core.ui.pipelines.type.reuse": "reuse",
    "core.ui.pipelines.sidebar.processing_servers.title": "Processing servers",
    "core.ui.pipelines.sidebar.processing_servers.desc": "Configure remote servers in Settings.",
    "core.ui.pipelines.sidebar.processing_servers.manage": "Manage servers",
    "core.ui.pipelines.empty": "Select or create a pipeline.",
    "core.ui.pipelines.actions.apply_template": "Apply template",
    "core.ui.pipelines.actions.compile": "Compile",
    "core.ui.pipelines.stats.title": "Step stats",
    "core.ui.pipelines.stats.window_hint": "Rolling window: last ~{{days}} days.",
    "core.ui.pipelines.stats.reset": "Reset stats",
    "core.ui.pipelines.stats.confirm_reset": "Reset stats for '{{name}}'?",
    "core.ui.pipelines.stats.step.outputs_tooltip": "Outputs (packets) in the rolling window.",
    "core.ui.pipelines.stats.loading": "Loading stats…",
    "core.ui.pipelines.stats.unavailable": "Stats unavailable: {{error}}",
    "core.ui.pipelines.stats.no_data": "No data yet.",
    "core.ui.pipelines.analysis.failed": "Pipeline analysis failed: {{error}}",
    "core.ui.pipelines.analysis.loading": "Analyzing pipeline…",
    "core.ui.pipelines.recommendations.title": "Recommendations",
    "core.ui.pipelines.recommendations.node": "Node: {{node_id}}",
    "core.ui.pipelines.form.type": "Type",
    "core.ui.pipelines.form.enabled": "Enabled",
    "core.ui.pipelines.form.processing_server": "Processing server",
    "core.ui.pipelines.form.processing_server.manage": "Manage…",
    "core.ui.pipelines.modes.interactive": "Interactive",
    "core.ui.pipelines.modes.json": "JSON",
    "core.ui.pipelines.modes.python_one_way": "Python (one-way)",
    "core.ui.pipelines.operator_count": "Operators available: {{count}}",
    "core.ui.pipelines.compile_output.title": "Compile output",
    "core.ui.pipelines.confirm_delete": "Delete pipeline '{{name}}'?",

    "core.ui.pipelines.operator_name.camera.source": "Camera source",
    "core.ui.pipelines.operator_name.core.schedule_gate": "Schedule gate",
    "core.ui.pipelines.operator_name.camera.motion_gate": "Motion detection gate",
    "core.ui.pipelines.operator_name.core.lifecycle_from_boolean": "Lifecycle from boolean",
    "core.ui.pipelines.operator_name.core.fps_reducer": "FPS reducer",
    "core.ui.pipelines.operator_name.camera.image_crop": "Crop frame",
    "core.ui.pipelines.operator_name.vision.object_tracking_yolo": "YOLO tracking",
    "core.ui.pipelines.operator_name.vision.object_detection_yolo": "YOLO detection",
    "core.ui.pipelines.operator_name.core.category_gate": "Category gate",
    "core.ui.pipelines.operator_name.core.filter": "Filter",
    "core.ui.pipelines.operator_name.camera.object_segmentation": "Object segmentation",
    "core.ui.pipelines.operator_name.camera.camera_mapping": "Camera mapping",
    "core.ui.pipelines.operator_name.camera.area_restriction": "Area restriction",
    "core.ui.pipelines.operator_name.camera.velocity_estimation": "Velocity estimation",
    "core.ui.pipelines.operator_name.camera.best_frame_selector": "Best frame selector",
    "core.ui.pipelines.operator_name.camera.image_adjust": "Image adjust",
    "core.ui.pipelines.operator_name.camera.image_resize": "Resize images",
    "core.ui.pipelines.operator_name.core.throttle": "Throttle",
    "core.ui.pipelines.operator_name.core.debounce": "Debounce",
    "core.ui.pipelines.operator_name.core.debug": "Debug",
    "core.ui.pipelines.operator_name.core.store_images": "Store images",
    "core.ui.pipelines.operator_name.core.notify": "Notification",

    "core.ui.pipelines.artifacts.frame_original": "Full frame",
    "core.ui.pipelines.artifacts.frame_cropped": "Cropped frame",
    "core.ui.pipelines.artifacts.frame_adjusted": "Adjusted frame",
    "core.ui.pipelines.artifacts.best_frame": "Best frame",
    "core.ui.pipelines.artifacts.segmented": "Segmented",
    "core.ui.pipelines.artifacts.treated": "Treated",
    "core.ui.pipelines.artifacts.original": "Original",
    "core.ui.pipelines.artifacts.face": "Face",
    "core.ui.pipelines.artifacts.pose": "Pose",

    "core.ui.pipelines.weekday.mon": "Mon",
    "core.ui.pipelines.weekday.tue": "Tue",
    "core.ui.pipelines.weekday.wed": "Wed",
    "core.ui.pipelines.weekday.thu": "Thu",
    "core.ui.pipelines.weekday.fri": "Fri",
    "core.ui.pipelines.weekday.sat": "Sat",
    "core.ui.pipelines.weekday.sun": "Sun",

    "core.ui.pipelines.editor.add_step": "Add step",
    "core.ui.pipelines.editor.no_steps": "No steps yet. Add operators to build the pipeline chain.",
    "core.ui.pipelines.editor.step.expand": "Expand",
    "core.ui.pipelines.editor.step.collapse": "Collapse",
    "core.ui.pipelines.editor.step.show_advanced": "Show advanced",
    "core.ui.pipelines.editor.step.hide_advanced": "Hide advanced",
    "core.ui.pipelines.editor.step.remove": "Remove step",
    "core.ui.pipelines.editor.step.step_id": "Step ID",
    "core.ui.pipelines.editor.step.step_id_hint": "Internal identifier used in storage paths, logs, and diagnostics.",
    "core.ui.pipelines.editor.step.step_id_placeholder": "stepId",
    "core.ui.pipelines.editor.step.config_json": "Config (JSON)",
    "core.ui.pipelines.editor.step.config_json_hint": "Use Advanced only when needed; most fields should be inferred from previous steps.",
    "core.ui.pipelines.editor.step.invalid_config_json": "Invalid config JSON: {{error}}",
    "core.ui.pipelines.editor.step.config_must_be_object": "Config must be a JSON object.",
    "core.ui.pipelines.editor.step.capabilities_prefix": "caps:",

    "core.ui.pipelines.template_apply.title": "Apply template",
    "core.ui.pipelines.template_apply.title_with_name": "Apply template: {{name}}",
    "core.ui.pipelines.template_apply.select_first": "Select a template pipeline first.",
    "core.ui.pipelines.template_apply.only_reuse": "Only reuse pipelines can be applied as templates.",
    "core.ui.pipelines.template_apply.cameras": "Cameras",
    "core.ui.pipelines.template_apply.cameras.placeholder": "Select cameras…",
    "core.ui.pipelines.template_apply.cameras.empty": "No cameras found…",
    "core.ui.pipelines.template_apply.hint": "Creates one final pipeline per camera (same graph, only camera_id changes).",
    "core.ui.pipelines.template_apply.processing_server": "Processing server",
    "core.ui.pipelines.template_apply.enable_created": "Enable created pipelines",
    "core.ui.pipelines.template_apply.conflict": "When name exists",
    "core.ui.pipelines.template_apply.conflict.skip": "Skip",
    "core.ui.pipelines.template_apply.conflict.replace": "Replace",
    "core.ui.pipelines.template_apply.conflict.error": "Error",
    "core.ui.pipelines.template_apply.applying": "Applying…",
    "core.ui.pipelines.template_apply.apply": "Apply",
    "core.ui.pipelines.template_apply.result.title": "Result",
    "core.ui.pipelines.template_apply.result.created": "Created",
    "core.ui.pipelines.template_apply.result.updated": "Updated",
    "core.ui.pipelines.template_apply.result.skipped": "Skipped",

    "core.ui.pipelines.panels.schedule_gate.enabled": "Enabled",
    "core.ui.pipelines.panels.schedule_gate.days": "Days",
    "core.ui.pipelines.panels.schedule_gate.days_placeholder": "No days (closed)",
    "core.ui.pipelines.panels.schedule_gate.start_time": "Start time",
    "core.ui.pipelines.panels.schedule_gate.end_time": "End time",
    "core.ui.pipelines.panels.schedule_gate.hint": "Place this before Camera source to pause RTSP reads while the gate is closed.",
    "core.ui.pipelines.panels.schedule_gate.timezone_optional": "Time zone (optional)",
    "core.ui.pipelines.panels.schedule_gate.timezone_placeholder": "Leave empty for local time",

    "core.ui.pipelines.panels.category_gate.mode": "Mode",
    "core.ui.pipelines.panels.category_gate.mode.include_only": "Include only",
    "core.ui.pipelines.panels.category_gate.mode.exclude": "Exclude",
    "core.ui.pipelines.panels.category_gate.categories": "Categories",
    "core.ui.pipelines.panels.category_gate.categories_placeholder": "All categories",
    "core.ui.pipelines.panels.category_gate.hint":
      "Matches payload.object_category_label (set by YOLO operators). Empty selection means “all categories”.",

    "core.ui.pipelines.panels.filter.preset": "Preset",
    "core.ui.pipelines.panels.filter.preset.custom.label": "Custom expression",
    "core.ui.pipelines.panels.filter.preset.custom.hint": "Write a safe expression referencing payload/metadata.",
    "core.ui.pipelines.panels.filter.preset.object_category_in.label": "Object category in list",
    "core.ui.pipelines.panels.filter.preset.object_category_in.hint": "Matches payload.object_category_label (YOLO).",
    "core.ui.pipelines.panels.filter.preset.object_category_not_in.label": "Object category not in list",
    "core.ui.pipelines.panels.filter.preset.object_category_not_in.hint": "Excludes payload.object_category_label (YOLO).",
    "core.ui.pipelines.panels.filter.preset.lifecycle_is.label": "Lifecycle is",
    "core.ui.pipelines.panels.filter.preset.lifecycle_is.hint": "Filters by packet lifecycle (open/update/close).",
    "core.ui.pipelines.panels.filter.preset.has_artifact.label": "Has artifact",
    "core.ui.pipelines.panels.filter.preset.has_artifact.hint": "Requires at least one artifact name to be present.",
    "core.ui.pipelines.panels.filter.expression": "Expression",
    "core.ui.pipelines.panels.filter.expression_hint":
      "Available names: payload, metadata, stream_id, lifecycle, artifacts. No function calls; only boolean logic, comparisons, and literals.",
    "core.ui.pipelines.panels.filter.categories": "Categories",
    "core.ui.pipelines.panels.filter.categories_placeholder": "All categories",
    "core.ui.pipelines.panels.filter.lifecycles": "Lifecycles",
    "core.ui.pipelines.panels.filter.lifecycles_placeholder": "All lifecycles",
    "core.ui.pipelines.panels.filter.artifacts": "Artifacts",
    "core.ui.pipelines.panels.filter.artifacts_placeholder": "Select artifacts…",
    "core.ui.pipelines.panels.filter.invert": "Invert",
    "core.ui.pipelines.panels.filter.hint":
      "Tip: place Filter before camera.source only when you are filtering gate packets (schedule, HA, etc.).",

    "core.ui.pipelines.panels.throttle.interval_seconds": "Interval (seconds)",
    "core.ui.pipelines.panels.throttle.mode": "Mode",
    "core.ui.pipelines.panels.throttle.mode.first": "First (recommended)",
    "core.ui.pipelines.panels.throttle.key": "Key",
    "core.ui.pipelines.panels.throttle.key.stream_id": "Stream (per object/camera)",
    "core.ui.pipelines.panels.throttle.key.tracking_id": "Tracking ID",
    "core.ui.pipelines.panels.throttle.key.correlation_id": "Correlation ID",
    "core.ui.pipelines.panels.throttle.key.camera_id": "Camera ID",
    "core.ui.pipelines.panels.throttle.hint":
      "Emits OPEN/CLOSE packets always. Mode “first” emits the first UPDATE in each interval window (keyed).",

    "core.ui.pipelines.panels.debounce.quiet_period_seconds": "Quiet period (seconds)",
    "core.ui.pipelines.panels.debounce.mode": "Mode",
    "core.ui.pipelines.panels.debounce.mode.first": "First (recommended)",
    "core.ui.pipelines.panels.debounce.key": "Key",
    "core.ui.pipelines.panels.debounce.key.stream_id": "Stream (per object/camera)",
    "core.ui.pipelines.panels.debounce.key.tracking_id": "Tracking ID",
    "core.ui.pipelines.panels.debounce.key.correlation_id": "Correlation ID",
    "core.ui.pipelines.panels.debounce.key.camera_id": "Camera ID",
    "core.ui.pipelines.panels.debounce.hint":
      "Emits OPEN/CLOSE packets always. Mode “first” emits the first UPDATE right away, then debounces subsequent updates.",

    "core.ui.pipelines.panels.debug.enabled": "Enabled",
    "core.ui.pipelines.panels.debug.hint": "Prints packets to stdout and optionally writes images to a temporary folder.",
    "core.ui.pipelines.panels.debug.save_images": "Save images",
    "core.ui.pipelines.panels.debug.max_images_per_packet": "Max images per packet",
    "core.ui.pipelines.panels.debug.output_dir": "Output dir (optional)",
    "core.ui.pipelines.panels.debug.output_dir_placeholder": "System temp",
    "core.ui.pipelines.panels.debug.print_payload": "Print payload",
    "core.ui.pipelines.panels.debug.print_metadata": "Print metadata",
    "core.ui.pipelines.panels.debug.print_artifacts": "Print artifacts",

    "core.ui.pipelines.panels.store_images.artifacts": "Artifacts",
    "core.ui.pipelines.panels.store_images.artifacts_placeholder": "Full frame",
    "core.ui.pipelines.panels.store_images.image_with_fallback": "Image (fallback order)",
    "core.ui.pipelines.panels.store_images.image_with_fallback_placeholder": "Segmented → Treated → Original",
    "core.ui.pipelines.panels.store_images.hint":
      "Stores one image locally on the origin and attaches a reference. Add this step more than once to store multiple images.",
    "core.ui.pipelines.panels.store_images.using_explicit_artifact_names":
      "This step is using explicit artifact_names (advanced). Fallback selection is ignored until you clear them.",
    "core.ui.pipelines.panels.store_images.use_fallback_button": "Use fallback (recommended)",
    "core.ui.pipelines.panels.store_images.subdir": "Subdir",
    "core.ui.pipelines.panels.store_images.format": "Format",
    "core.ui.pipelines.panels.store_images.jpeg_quality": "JPG quality",
    "core.ui.pipelines.panels.store_images.drop_data_after_store": "Drop pixel data after store",
    "core.ui.pipelines.panels.store_images.drop_data_after_store_hint": "Recommended. Keeps memory stable under load.",
    "core.ui.pipelines.panels.store_images.keep_data": "Keep data in memory",
    "core.ui.pipelines.panels.store_images.keep_data_hint": "If disabled, pixel data is dropped after storing to keep memory stable.",
    "core.ui.pipelines.panels.store_images.overwrite": "Overwrite existing files",

    "core.ui.pipelines.panels.notify.title_template": "Title template",
    "core.ui.pipelines.panels.notify.title_placeholder": "{{object_category_label}} detected",
    "core.ui.pipelines.panels.notify.template_hint_prefix": "Use templates like",
    "core.ui.pipelines.panels.notify.description_template": "Description template",
    "core.ui.pipelines.panels.notify.description_placeholder": "Optional",
    "core.ui.pipelines.panels.notify.priority": "Priority",
    "core.ui.pipelines.panels.notify.priority.low": "Low",
    "core.ui.pipelines.panels.notify.priority.medium": "Medium",
    "core.ui.pipelines.panels.notify.priority.high": "High",
    "core.ui.pipelines.panels.notify.realtime": "Realtime updates",
    "core.ui.pipelines.panels.notify.update_interval_seconds": "Update interval (seconds)",
    "core.ui.pipelines.panels.notify.update_interval_hint":
      "Avoids spamming UI updates while an event is open. Set to 0 to emit every change.",
    "core.ui.pipelines.panels.notify.thumbnail_fallback": "Thumbnail fallback",
    "core.ui.pipelines.panels.notify.thumbnail_placeholder": "Best frame → Face → Segmented → Full frame",
    "core.ui.pipelines.panels.notify.thumbnail_hint":
      "Registers notifications only (never stores images). To include images, add Store Images before this step.",
    "core.ui.pipelines.panels.notify.notification_type": "Notification type",
    "core.ui.pipelines.panels.notify.dedupe_key_template": "Dedupe key template",
    "core.ui.pipelines.panels.notify.dedupe_key_placeholder": "Leave empty for default",
    "core.ui.pipelines.panels.notify.dedupe_key_hint_prefix": "Use templates like",

    "core.ui.pipelines.panels.camera_source.camera": "Camera",
    "core.ui.pipelines.panels.camera_source.camera_placeholder": "Select a camera…",
    "core.ui.pipelines.panels.camera_source.hint_infer":
      "RTSP URL, credentials, and FPS are inferred from the camera registry. Toggle Advanced to override.",
    "core.ui.pipelines.panels.camera_source.hint_no_cameras":
      "No cameras found. Configure cameras in the Cameras extension settings.",
    "core.ui.pipelines.panels.camera_source.backend": "Backend",
    "core.ui.pipelines.panels.camera_source.backend.auto": "Auto (recommended)",
    "core.ui.pipelines.panels.camera_source.hint_backend":
      "Auto selects the best available backend and falls back automatically if one fails to initialize.",

    "core.ui.pipelines.panels.camera_mapping.hint":
      "Uses control points defined in your compositions to map image → world coordinates. Configure control points in the Composition editor.",
    "core.ui.pipelines.panels.camera_mapping.select_camera_error":
      "Select a camera in the Camera Source step to show mapping status.",
    "core.ui.pipelines.panels.camera_mapping.mapping_ready": "mapping ready",
    "core.ui.pipelines.panels.camera_mapping.mapping_missing": "missing mapping",
    "core.ui.pipelines.panels.camera_mapping.areas_count": "areas: {{count}}",
    "core.ui.pipelines.panels.camera_mapping.camera_nodes": "camera nodes: {{names}}",
    "core.ui.pipelines.panels.camera_mapping.load_failed": "Failed to load camera contexts: {{error}}",
    "core.ui.pipelines.panels.camera_mapping.loading": "Loading camera contexts…",

    "core.ui.pipelines.panels.area_restriction.areas": "Areas",
    "core.ui.pipelines.panels.area_restriction.select_camera_first": "Select a camera first…",
    "core.ui.pipelines.panels.area_restriction.select_areas": "Select areas…",
    "core.ui.pipelines.panels.area_restriction.select_camera_step_error":
      "Select a camera in the Camera Source step first.",
    "core.ui.pipelines.panels.area_restriction.load_failed": "Failed to load camera contexts: {{error}}",
    "core.ui.pipelines.panels.area_restriction.loading": "Loading camera contexts…",
    "core.ui.pipelines.panels.area_restriction.no_areas": "No areas found in compositions for this camera.",
    "core.ui.pipelines.panels.area_restriction.hint_areas":
      "Uses areas from the compositions where the selected camera is present.",
    "core.ui.pipelines.panels.area_restriction.invalid_areas":
      "Some selected areas are not available for this camera: {{areas}}",

    "core.ui.pipelines.panels.velocity.flow_mode": "Flow mode",
    "core.ui.pipelines.panels.velocity.mode.annotate.label": "Annotate only",
    "core.ui.pipelines.panels.velocity.mode.annotate.hint": "Always emit packets; adds velocity payload.",
    "core.ui.pipelines.panels.velocity.mode.stopped_now.label": "Only when stopped",
    "core.ui.pipelines.panels.velocity.mode.stopped_now.hint": "Emit packets only while the object is stopped.",
    "core.ui.pipelines.panels.velocity.mode.moving_now.label": "Only when moving",
    "core.ui.pipelines.panels.velocity.mode.moving_now.hint": "Emit packets only while the object is moving.",
    "core.ui.pipelines.panels.velocity.mode.stopped_once.label": "Only after it stopped once",
    "core.ui.pipelines.panels.velocity.mode.stopped_once.hint": "Drops packets until it stops at least once, then passes all.",
    "core.ui.pipelines.panels.velocity.mode.always_moving.label": "Only while it never stopped",
    "core.ui.pipelines.panels.velocity.mode.always_moving.hint": "Passes packets until it stops once, then drops the rest.",
    "core.ui.pipelines.panels.velocity.stopped_threshold": "Stopped threshold (km/h)",
    "core.ui.pipelines.panels.velocity.hint":
      "Computes speed from mapped world coordinates (Camera Mapping step). Uses m/s internally and also displays km/h.",
    "core.ui.pipelines.panels.velocity.mapping_required": "Add Camera Mapping before this step to get world speed.",

    "core.ui.pipelines.panels.image_crop.hint":
      "Crops the frame for downstream analysis (YOLO). The original full frame is preserved as original.",
    "core.ui.pipelines.panels.image_crop.units": "Units",
    "core.ui.pipelines.panels.image_crop.units.percent": "Percent (0–100)",
    "core.ui.pipelines.panels.image_crop.units.pixels": "Pixels",
    "core.ui.pipelines.panels.image_crop.left": "Left",
    "core.ui.pipelines.panels.image_crop.top": "Top",
    "core.ui.pipelines.panels.image_crop.right": "Right",
    "core.ui.pipelines.panels.image_crop.bottom": "Bottom",
    "core.ui.pipelines.panels.image_crop.rectangle_hint":
      "Rectangle is defined as Left/Top/Right/Bottom (percent of frame or pixels from top-left).",
    "core.ui.pipelines.panels.image_crop.reset": "Reset",
    "core.ui.pipelines.panels.image_crop.output_artifact_name": "Output artifact name",
    "core.ui.pipelines.panels.image_crop.min_crop_size_px": "Min crop size (px)",
    "core.ui.pipelines.panels.image_crop.use_cropped_frame": "Use cropped frame for downstream",

    "core.ui.pipelines.panels.image_adjust.input_artifacts": "Input artifacts (fallback order)",
    "core.ui.pipelines.panels.image_adjust.input_artifacts_placeholder": "Full frame",
    "core.ui.pipelines.panels.image_adjust.input_artifacts_hint": "Uses the first available image. Keep original as fallback.",
    "core.ui.pipelines.panels.image_adjust.saturation": "Saturation",
    "core.ui.pipelines.panels.image_adjust.brightness": "Brightness",
    "core.ui.pipelines.panels.image_adjust.contrast": "Contrast",
    "core.ui.pipelines.panels.image_adjust.gamma": "Gamma",
    "core.ui.pipelines.panels.image_adjust.brightness_hint":
      "Brightness is an additive offset in normalized space (e.g. 0.10 = +10%).",
    "core.ui.pipelines.panels.image_adjust.output_artifact_name": "Output artifact name",
    "core.ui.pipelines.panels.image_adjust.apply_stream_frame": "Apply to stream frame",
    "core.ui.pipelines.panels.image_adjust.fallback_stream_frame": "Fallback to stream frame",
    "core.ui.pipelines.panels.image_adjust.preserve_alpha": "Preserve alpha channel",

    "core.ui.pipelines.panels.image_resize.artifacts": "Artifacts",
    "core.ui.pipelines.panels.image_resize.artifacts_placeholder": "Full frame",
    "core.ui.pipelines.panels.image_resize.hint":
      "Resizes artifacts in-memory before storage to keep file sizes reasonable.",
    "core.ui.pipelines.panels.image_resize.max_edge_px": "Max edge (px)",
    "core.ui.pipelines.panels.image_resize.allow_upscale": "Allow upscale",

    "core.ui.pipelines.panels.yolo.min_confidence": "Min confidence",
    "core.ui.pipelines.panels.yolo.min_confidence_hint": "Filters low-confidence detections/tracks (default: 0.40).",
    "core.ui.pipelines.panels.yolo.categories": "Categories",
    "core.ui.pipelines.panels.yolo.categories_placeholder": "All categories",
    "core.ui.pipelines.panels.yolo.categories_hint": "Empty selection means “all categories”.",
    "core.ui.pipelines.panels.yolo.update_interval_tracking": "Update interval (seconds)",
    "core.ui.pipelines.panels.yolo.update_interval_detection": "Event interval (seconds)",
    "core.ui.pipelines.panels.yolo.update_interval_hint":
      "Min seconds between emits per camera + category. Use 0 only if you really want “every frame” (can overload notify/storage/debug).",
    "core.ui.pipelines.panels.yolo.close_after_seconds": "Close after (seconds)",
    "core.ui.pipelines.panels.yolo.close_after_hint":
      "Closes a track if the object is not seen for this long (higher = more stable, slower close).",

    "core.ui.processing_servers.title": "Processing servers",
    "core.ui.processing_servers.add_server": "Add server",
    "core.ui.processing_servers.description":
      "Configure remote processing servers to run heavy operators (YOLO). Storage and notifications still happen on the origin server.",
    "core.ui.processing_servers.none": "No processing servers configured.",
    "core.ui.processing_servers.built_in": "(built-in)",
    "core.ui.processing_servers.status.testing": "testing…",
    "core.ui.processing_servers.status.online": "online",
    "core.ui.processing_servers.status.offline": "offline",
    "core.ui.processing_servers.actions.test_connection": "Test connection",
    "core.ui.processing_servers.actions.edit_server": "Edit server",
    "core.ui.processing_servers.actions.delete_server": "Delete server",
    "core.ui.processing_servers.confirm_delete": "Delete processing server '{{id}}'?",

    "core.ui.processing_server_modal.title_add": "Add processing server",
    "core.ui.processing_server_modal.title_edit": "Edit processing server",
    "core.ui.processing_server_modal.hint":
      "Run the processing server on another machine and connect it here. Storage and notifications still happen on the origin server.",
    "core.ui.processing_server_modal.field.id": "ID",
    "core.ui.processing_server_modal.field.name": "Name (optional)",
    "core.ui.processing_server_modal.field.scheme": "Scheme",
    "core.ui.processing_server_modal.field.host": "Host / IP",
    "core.ui.processing_server_modal.field.port": "Port",
    "core.ui.processing_server_modal.field.username": "Username (optional)",
    "core.ui.processing_server_modal.field.password": "Password (optional)",
    "core.ui.processing_server_modal.url_preview": "URL preview: {{url}}",
    "core.ui.processing_server_modal.suggested_id": "Suggested id: {{id}}",
    "core.ui.processing_server_modal.remote_command": "Remote command:",
    "core.ui.processing_server_modal.connection_ok": "Connection: OK",
    "core.ui.processing_server_modal.connection_failed": "Connection: {{error}}",
    "core.ui.processing_server_modal.actions.test_connection": "Test connection",

    "core.ui.settings.nav.pipelines.title": "Pipelines",
    "core.ui.settings.nav.pipelines.desc": "Create and edit pipelines.",
    "core.ui.settings.nav.processing_servers.title": "Processing servers",
    "core.ui.settings.nav.processing_servers.desc": "Manage remote processing servers.",
  },
  "pt-BR": {
    "core.actions.add": "Adicionar",
    "core.actions.apply": "Aplicar",
    "core.actions.back": "Voltar",
    "core.actions.cancel": "Cancelar",
    "core.actions.close": "Fechar",
    "core.actions.delete": "Excluir",
    "core.actions.edit": "Editar",
    "core.actions.rename": "Renomear",
    "core.actions.save": "Salvar",

    "core.ui.rendering": "Renderização",
    "core.ui.composition": "Composição",
    "core.ui.notifications": "Notificações",
    "core.ui.notifications.aria_open": "Abrir notificações",
    "core.ui.notifications.aria_close": "Fechar notificações",
    "core.ui.notifications.show_low": "Mostrar baixa prioridade",
    "core.ui.notifications.hide_low": "Ocultar baixa prioridade",
    "core.ui.notifications.low_hidden": "{{count}} notificações de baixa prioridade ocultas.",
    "core.ui.layers": "Camadas",
    "core.ui.add": "Adicionar",
    "core.ui.tools": "Ferramentas",
    "core.ui.action": "Ação",
    "core.ui.view_settings.title": "Visualização",
    "core.ui.view_settings.aria": "Configurações de visualização",
    "core.ui.view_settings.wall_height": "Altura da parede",
    "core.ui.view_settings.interactivity": "Interatividade",
    "core.ui.view_settings.ghost_walls": "Paredes transparentes",
    "core.ui.view_settings.ghost_walls_desc": "Deixa as paredes semi-transparentes e permite clicar nos elementos através delas.",
    "core.ui.view_settings.graphics_quality": "Gráficos",
    "core.ui.graphics_quality.simplified": "Simplificados",
    "core.ui.graphics_quality.simplified_desc": "Mais leve e com melhor performance.",
    "core.ui.graphics_quality.detailed": "Detalhados",
    "core.ui.graphics_quality.detailed_desc": "Mais detalhes e efeitos. Exige mais do computador.",
    "core.ui.settings.title": "Configurações",
    "core.ui.settings.aria": "Configurações",
    "core.ui.settings.sections.view": "Opções de visualização",
    "core.ui.settings.sections.view_desc": "Paredes, interatividade e gráficos.",
    "core.ui.settings.sections.core": "Base",
    "core.ui.settings.sections.core_desc": "Idioma e preferências gerais.",
    "core.ui.settings.no_extensions": "Nenhuma extensão adicionou configurações ainda.",
    "core.ui.settings.backend_offline_title": "Backend indisponível",
    "core.ui.settings.backend_offline_desc": "As alterações não serão persistidas até o backend estar rodando.",
    "core.ui.settings.language": "Idioma",
    "core.ui.settings.language.pt": "Português (Brasil)",
    "core.ui.settings.language.pt_desc": "Interface em português.",
    "core.ui.settings.language.en": "English",
    "core.ui.settings.language.en_desc": "Interface in English.",
    "core.ui.settings.theme": "Tema",
    "core.ui.settings.theme.default": "Padrão",
    "core.ui.settings.theme.default_desc": "Tema padrão do Toposync.",
    "core.ui.settings.save_changes": "Salvar alterações",
    "core.ui.settings.save_all_changes": "Salvar todas as alterações",
    "core.ui.settings.discard_changes": "Descartar alterações",
    "core.ui.settings.discard_and_close": "Descartar e fechar",
    "core.ui.settings.changes_saved": "Salvo",
    "core.ui.settings.unsaved_changes": "Alterações não salvas",
    "core.ui.settings.unsaved_changes_in": "Não salvo: {{sections}}",
    "core.ui.settings.saving": "Salvando…",
    "core.ui.settings.confirm_discard_title": "Descartar alterações?",
    "core.ui.settings.confirm_discard_desc": "Descartar todas as alterações pendentes nas configurações?",
    "core.ui.settings.confirm_close_title": "Descartar e fechar?",
    "core.ui.settings.confirm_close_desc": "Você tem alterações não salvas. Descartar e fechar as configurações?",
    "core.ui.settings.confirm_open_pipelines_title": "Descartar alterações e abrir Pipelines?",
    "core.ui.settings.confirm_open_processing_servers_title": "Descartar alterações e abrir Servidores de processamento?",
    "core.ui.settings.confirm_discard_continue": "Descartar e continuar",
    "core.ui.settings.confirm_discard_continue_desc": "Você tem configurações não salvas{{suffix}}. Descartar e continuar?",

    "core.ui.wall_height.low": "Baixa",
    "core.ui.wall_height.low_desc": "Baixa para facilitar a visualização.",
    "core.ui.wall_height.medium": "Média",
    "core.ui.wall_height.medium_desc": "Média para planejar.",
    "core.ui.wall_height.high": "Alta",
    "core.ui.wall_height.high_desc": "Alta (altura normal de parede).",

    "core.ui.layers_group_walls": "Paredes",
    "core.ui.layers_group_areas": "Áreas",
    "core.ui.layers_group_background": "Fundo",
    "core.ui.layers.hide": "Ocultar camada",
    "core.ui.layers.show": "Mostrar camada",
    "core.ui.layers.lock": "Bloquear movimentação",
    "core.ui.layers.unlock": "Desbloquear movimentação",
    "core.ui.layers.reorder": "Reordenar camada",

    "core.tools.navigate": "Navegar",
    "core.tools.navigate_desc": "Mover o canvas.",
    "core.tools.select": "Selecionar",
    "core.tools.select_desc": "Selecionar e mover elementos.",

    "core.ui.empty_title": "Nada configurado ainda",
    "core.ui.empty_desc": "Clique em “Editar” para adicionar elementos na composição.",
    "core.ui.notifications_empty": "Nenhuma notificação por enquanto.",
    "core.ui.image_preview": "Imagem",
    "core.ui.loading": "Carregando…",
    "core.ui.error": "Erro",
    "core.ui.element_types_empty": "Nenhuma extensão registrou elementos ainda.",
    "core.ui.layers_empty": "Nenhum elemento adicionado ainda.",

    "core.ui.render_modal.title": "Renderização",
    "core.ui.render_modal.option_3d.title": "3D (ThreeJS)",
    "core.ui.render_modal.option_3d.desc": "Visualização 3D interativa.",
    "core.ui.render_modal.option_2d.title": "2D (Captura)",
    "core.ui.render_modal.option_2d.desc": "Captura de cima com overlays do Home Assistant.",

    "core.ui.main2d.cluster.title": "Vários itens ({{count}})",
    "core.ui.main2d.cluster.tooltip": "{{count}} itens",

    "core.ui.action_unavailable": "Sem ações disponíveis para este elemento.",

    "core.compositions.modal.title": "Composições",
    "core.compositions.section.new": "Nova composição",
    "core.compositions.section.list": "Suas composições",
    "core.compositions.new.placeholder": "Nome (ex: Térreo, Superior...)",
    "core.compositions.delete_confirm": "Excluir “{{name}}”?",
    "core.compositions.cannot_delete_last": "Não é possível excluir a última composição",
    "core.compositions.aria.create": "Criar composição",
    "core.compositions.aria.save_name": "Salvar nome",
    "core.compositions.aria.cancel": "Cancelar",
    "core.compositions.aria.rename": "Renomear composição",
    "core.compositions.aria.delete": "Excluir composição",
    "core.compositions.aria.cancel_delete": "Cancelar exclusão",
    "core.compositions.aria.confirm_delete": "Confirmar exclusão",
    "core.compositions.error.create": "Falha ao criar composição",
    "core.compositions.error.activate": "Falha ao trocar composição",
    "core.compositions.error.rename": "Falha ao renomear composição",
    "core.compositions.error.delete": "Falha ao excluir composição",

    "core.element_editor.title": "Editar elemento",
    "core.element_editor.name": "Nome",
    "core.element_editor.pos_x": "Posição X",
    "core.element_editor.pos_y": "Posição Y",
    "core.element_editor.pos_z": "Posição Z",
    "core.element_editor.rot_x": "Rotação X (graus)",
    "core.element_editor.rot_y": "Rotação Y (graus)",
    "core.element_editor.rot_z": "Rotação Z (graus)",
    "core.element_editor.delete": "Excluir elemento",

    "core.modal.aria.close": "Fechar",

    "core.ui.pipelines.title": "Pipelines",
    "core.ui.pipelines.aria.toggle_list": "Alternar lista de pipelines",
    "core.ui.pipelines.aria.close_list": "Fechar lista de pipelines",
    "core.ui.pipelines.create.placeholder_name": "Nome do pipeline",
    "core.ui.pipelines.create.button": "Criar",
    "core.ui.pipelines.type.final": "final",
    "core.ui.pipelines.type.reuse": "reuso",
    "core.ui.pipelines.sidebar.processing_servers.title": "Servidores de processamento",
    "core.ui.pipelines.sidebar.processing_servers.desc": "Configure servidores remotos nas Configurações.",
    "core.ui.pipelines.sidebar.processing_servers.manage": "Gerenciar servidores",
    "core.ui.pipelines.empty": "Selecione ou crie um pipeline.",
    "core.ui.pipelines.actions.apply_template": "Aplicar template",
    "core.ui.pipelines.actions.compile": "Compilar",
    "core.ui.pipelines.stats.title": "Estatísticas por etapa",
    "core.ui.pipelines.stats.window_hint": "Janela móvel: últimos ~{{days}} dias.",
    "core.ui.pipelines.stats.reset": "Resetar estatísticas",
    "core.ui.pipelines.stats.confirm_reset": "Resetar estatísticas de '{{name}}'?",
    "core.ui.pipelines.stats.step.outputs_tooltip": "Saídas (pacotes) na janela móvel.",
    "core.ui.pipelines.stats.loading": "Carregando estatísticas…",
    "core.ui.pipelines.stats.unavailable": "Estatísticas indisponíveis: {{error}}",
    "core.ui.pipelines.stats.no_data": "Sem dados ainda.",
    "core.ui.pipelines.analysis.failed": "Falha ao analisar pipeline: {{error}}",
    "core.ui.pipelines.analysis.loading": "Analisando pipeline…",
    "core.ui.pipelines.recommendations.title": "Recomendações",
    "core.ui.pipelines.recommendations.node": "Nó: {{node_id}}",
    "core.ui.pipelines.form.type": "Tipo",
    "core.ui.pipelines.form.enabled": "Ativado",
    "core.ui.pipelines.form.processing_server": "Servidor de processamento",
    "core.ui.pipelines.form.processing_server.manage": "Gerenciar…",
    "core.ui.pipelines.modes.interactive": "Interativo",
    "core.ui.pipelines.modes.json": "JSON",
    "core.ui.pipelines.modes.python_one_way": "Python (sem volta)",
    "core.ui.pipelines.operator_count": "Operadores disponíveis: {{count}}",
    "core.ui.pipelines.compile_output.title": "Resultado da compilação",
    "core.ui.pipelines.confirm_delete": "Excluir pipeline '{{name}}'?",

    "core.ui.pipelines.operator_name.camera.source": "Fonte da câmera",
    "core.ui.pipelines.operator_name.core.schedule_gate": "Gate de horário",
    "core.ui.pipelines.operator_name.camera.motion_gate": "Gate de movimento",
    "core.ui.pipelines.operator_name.core.lifecycle_from_boolean": "Lifecycle a partir de boolean",
    "core.ui.pipelines.operator_name.core.fps_reducer": "Redutor de FPS",
    "core.ui.pipelines.operator_name.camera.image_crop": "Recortar frame",
    "core.ui.pipelines.operator_name.vision.object_tracking_yolo": "Tracking YOLO",
    "core.ui.pipelines.operator_name.vision.object_detection_yolo": "Detecção YOLO",
    "core.ui.pipelines.operator_name.core.category_gate": "Gate de categorias",
    "core.ui.pipelines.operator_name.core.filter": "Filtro",
    "core.ui.pipelines.operator_name.camera.object_segmentation": "Segmentação de objeto",
    "core.ui.pipelines.operator_name.camera.camera_mapping": "Mapeamento de câmera",
    "core.ui.pipelines.operator_name.camera.area_restriction": "Restrição de área",
    "core.ui.pipelines.operator_name.camera.velocity_estimation": "Estimativa de velocidade",
    "core.ui.pipelines.operator_name.camera.best_frame_selector": "Melhor frame",
    "core.ui.pipelines.operator_name.camera.image_adjust": "Ajuste de imagem",
    "core.ui.pipelines.operator_name.camera.image_resize": "Redimensionar imagens",
    "core.ui.pipelines.operator_name.core.throttle": "Throttle",
    "core.ui.pipelines.operator_name.core.debounce": "Debounce",
    "core.ui.pipelines.operator_name.core.debug": "Debug",
    "core.ui.pipelines.operator_name.core.store_images": "Armazenar imagens",
    "core.ui.pipelines.operator_name.core.notify": "Notificação",

    "core.ui.pipelines.artifacts.frame_original": "Frame completo",
    "core.ui.pipelines.artifacts.frame_cropped": "Frame recortado",
    "core.ui.pipelines.artifacts.frame_adjusted": "Frame ajustado",
    "core.ui.pipelines.artifacts.best_frame": "Melhor frame",
    "core.ui.pipelines.artifacts.segmented": "Segmentado",
    "core.ui.pipelines.artifacts.treated": "Tratado",
    "core.ui.pipelines.artifacts.original": "Original",
    "core.ui.pipelines.artifacts.face": "Rosto",
    "core.ui.pipelines.artifacts.pose": "Pose",

    "core.ui.pipelines.weekday.mon": "Seg",
    "core.ui.pipelines.weekday.tue": "Ter",
    "core.ui.pipelines.weekday.wed": "Qua",
    "core.ui.pipelines.weekday.thu": "Qui",
    "core.ui.pipelines.weekday.fri": "Sex",
    "core.ui.pipelines.weekday.sat": "Sáb",
    "core.ui.pipelines.weekday.sun": "Dom",

    "core.ui.pipelines.editor.add_step": "Adicionar etapa",
    "core.ui.pipelines.editor.no_steps": "Nenhuma etapa ainda. Adicione operadores para montar o pipeline.",
    "core.ui.pipelines.editor.step.expand": "Expandir",
    "core.ui.pipelines.editor.step.collapse": "Recolher",
    "core.ui.pipelines.editor.step.show_advanced": "Mostrar avançado",
    "core.ui.pipelines.editor.step.hide_advanced": "Ocultar avançado",
    "core.ui.pipelines.editor.step.remove": "Remover etapa",
    "core.ui.pipelines.editor.step.step_id": "ID da etapa",
    "core.ui.pipelines.editor.step.step_id_hint": "Identificador interno usado em caminhos de armazenamento, logs e diagnósticos.",
    "core.ui.pipelines.editor.step.step_id_placeholder": "stepId",
    "core.ui.pipelines.editor.step.config_json": "Config (JSON)",
    "core.ui.pipelines.editor.step.config_json_hint":
      "Use o modo Avançado apenas quando necessário; a maioria dos campos deve ser inferida de etapas anteriores.",
    "core.ui.pipelines.editor.step.invalid_config_json": "JSON de configuração inválido: {{error}}",
    "core.ui.pipelines.editor.step.config_must_be_object": "A configuração deve ser um objeto JSON.",
    "core.ui.pipelines.editor.step.capabilities_prefix": "caps:",

    "core.ui.pipelines.template_apply.title": "Aplicar template",
    "core.ui.pipelines.template_apply.title_with_name": "Aplicar template: {{name}}",
    "core.ui.pipelines.template_apply.select_first": "Selecione um pipeline template primeiro.",
    "core.ui.pipelines.template_apply.only_reuse": "Apenas pipelines de reuso podem ser aplicados como templates.",
    "core.ui.pipelines.template_apply.cameras": "Câmeras",
    "core.ui.pipelines.template_apply.cameras.placeholder": "Selecionar câmeras…",
    "core.ui.pipelines.template_apply.cameras.empty": "Nenhuma câmera encontrada…",
    "core.ui.pipelines.template_apply.hint": "Cria um pipeline final por câmera (mesmo graph; apenas camera_id muda).",
    "core.ui.pipelines.template_apply.processing_server": "Servidor de processamento",
    "core.ui.pipelines.template_apply.enable_created": "Ativar pipelines criados",
    "core.ui.pipelines.template_apply.conflict": "Quando o nome já existe",
    "core.ui.pipelines.template_apply.conflict.skip": "Pular",
    "core.ui.pipelines.template_apply.conflict.replace": "Substituir",
    "core.ui.pipelines.template_apply.conflict.error": "Erro",
    "core.ui.pipelines.template_apply.applying": "Aplicando…",
    "core.ui.pipelines.template_apply.apply": "Aplicar",
    "core.ui.pipelines.template_apply.result.title": "Resultado",
    "core.ui.pipelines.template_apply.result.created": "Criados",
    "core.ui.pipelines.template_apply.result.updated": "Atualizados",
    "core.ui.pipelines.template_apply.result.skipped": "Ignorados",

    "core.ui.pipelines.panels.schedule_gate.enabled": "Ativado",
    "core.ui.pipelines.panels.schedule_gate.days": "Dias",
    "core.ui.pipelines.panels.schedule_gate.days_placeholder": "Nenhum dia (fechado)",
    "core.ui.pipelines.panels.schedule_gate.start_time": "Hora de início",
    "core.ui.pipelines.panels.schedule_gate.end_time": "Hora de término",
    "core.ui.pipelines.panels.schedule_gate.hint": "Coloque antes do Camera source para pausar leituras RTSP enquanto o gate estiver fechado.",
    "core.ui.pipelines.panels.schedule_gate.timezone_optional": "Fuso horário (opcional)",
    "core.ui.pipelines.panels.schedule_gate.timezone_placeholder": "Deixe vazio para o horário local",

    "core.ui.pipelines.panels.category_gate.mode": "Modo",
    "core.ui.pipelines.panels.category_gate.mode.include_only": "Incluir apenas",
    "core.ui.pipelines.panels.category_gate.mode.exclude": "Excluir",
    "core.ui.pipelines.panels.category_gate.categories": "Categorias",
    "core.ui.pipelines.panels.category_gate.categories_placeholder": "Todas as categorias",
    "core.ui.pipelines.panels.category_gate.hint":
      "Compara com payload.object_category_label (definido pelos operadores YOLO). Seleção vazia significa “todas as categorias”.",

    "core.ui.pipelines.panels.filter.preset": "Preset",
    "core.ui.pipelines.panels.filter.preset.custom.label": "Expressão customizada",
    "core.ui.pipelines.panels.filter.preset.custom.hint": "Escreva uma expressão segura referenciando payload/metadata.",
    "core.ui.pipelines.panels.filter.preset.object_category_in.label": "Categoria do objeto na lista",
    "core.ui.pipelines.panels.filter.preset.object_category_in.hint": "Compara com payload.object_category_label (YOLO).",
    "core.ui.pipelines.panels.filter.preset.object_category_not_in.label": "Categoria do objeto fora da lista",
    "core.ui.pipelines.panels.filter.preset.object_category_not_in.hint": "Exclui payload.object_category_label (YOLO).",
    "core.ui.pipelines.panels.filter.preset.lifecycle_is.label": "Lifecycle é",
    "core.ui.pipelines.panels.filter.preset.lifecycle_is.hint": "Filtra pelo lifecycle do pacote (open/update/close).",
    "core.ui.pipelines.panels.filter.preset.has_artifact.label": "Possui artefato",
    "core.ui.pipelines.panels.filter.preset.has_artifact.hint": "Requer ao menos um nome de artefato presente.",
    "core.ui.pipelines.panels.filter.expression": "Expressão",
    "core.ui.pipelines.panels.filter.expression_hint":
      "Nomes disponíveis: payload, metadata, stream_id, lifecycle, artifacts. Sem chamadas de função; apenas lógica booleana, comparações e literais.",
    "core.ui.pipelines.panels.filter.categories": "Categorias",
    "core.ui.pipelines.panels.filter.categories_placeholder": "Todas as categorias",
    "core.ui.pipelines.panels.filter.lifecycles": "Lifecycles",
    "core.ui.pipelines.panels.filter.lifecycles_placeholder": "Todos os lifecycles",
    "core.ui.pipelines.panels.filter.artifacts": "Artefatos",
    "core.ui.pipelines.panels.filter.artifacts_placeholder": "Selecionar artefatos…",
    "core.ui.pipelines.panels.filter.invert": "Inverter",
    "core.ui.pipelines.panels.filter.hint":
      "Dica: coloque Filter antes de camera.source apenas quando estiver filtrando pacotes de gate (schedule, HA, etc.).",

    "core.ui.pipelines.panels.throttle.interval_seconds": "Intervalo (segundos)",
    "core.ui.pipelines.panels.throttle.mode": "Modo",
    "core.ui.pipelines.panels.throttle.mode.first": "Primeiro (recomendado)",
    "core.ui.pipelines.panels.throttle.key": "Chave",
    "core.ui.pipelines.panels.throttle.key.stream_id": "Stream (por objeto/câmera)",
    "core.ui.pipelines.panels.throttle.key.tracking_id": "Tracking ID",
    "core.ui.pipelines.panels.throttle.key.correlation_id": "Correlation ID",
    "core.ui.pipelines.panels.throttle.key.camera_id": "Camera ID",
    "core.ui.pipelines.panels.throttle.hint":
      "Emite pacotes OPEN/CLOSE sempre. No modo “first”, emite o primeiro UPDATE de cada janela (por chave).",

    "core.ui.pipelines.panels.debounce.quiet_period_seconds": "Período de silêncio (segundos)",
    "core.ui.pipelines.panels.debounce.mode": "Modo",
    "core.ui.pipelines.panels.debounce.mode.first": "Primeiro (recomendado)",
    "core.ui.pipelines.panels.debounce.key": "Chave",
    "core.ui.pipelines.panels.debounce.key.stream_id": "Stream (por objeto/câmera)",
    "core.ui.pipelines.panels.debounce.key.tracking_id": "Tracking ID",
    "core.ui.pipelines.panels.debounce.key.correlation_id": "Correlation ID",
    "core.ui.pipelines.panels.debounce.key.camera_id": "Camera ID",
    "core.ui.pipelines.panels.debounce.hint":
      "Emite pacotes OPEN/CLOSE sempre. No modo “first”, emite o primeiro UPDATE imediatamente e depois faz debounce nos seguintes.",

    "core.ui.pipelines.panels.debug.enabled": "Ativado",
    "core.ui.pipelines.panels.debug.hint": "Imprime pacotes no stdout e opcionalmente grava imagens em uma pasta temporária.",
    "core.ui.pipelines.panels.debug.save_images": "Salvar imagens",
    "core.ui.pipelines.panels.debug.max_images_per_packet": "Máximo de imagens por pacote",
    "core.ui.pipelines.panels.debug.output_dir": "Diretório de saída (opcional)",
    "core.ui.pipelines.panels.debug.output_dir_placeholder": "Temp do sistema",
    "core.ui.pipelines.panels.debug.print_payload": "Imprimir payload",
    "core.ui.pipelines.panels.debug.print_metadata": "Imprimir metadata",
    "core.ui.pipelines.panels.debug.print_artifacts": "Imprimir artefatos",

    "core.ui.pipelines.panels.store_images.artifacts": "Artefatos",
    "core.ui.pipelines.panels.store_images.artifacts_placeholder": "Frame completo",
    "core.ui.pipelines.panels.store_images.image_with_fallback": "Imagem (ordem de fallback)",
    "core.ui.pipelines.panels.store_images.image_with_fallback_placeholder": "Segmentado → Tratado → Original",
    "core.ui.pipelines.panels.store_images.hint":
      "Salva uma imagem localmente na origem e anexa a referência. Adicione esta etapa mais de uma vez para salvar várias imagens.",
    "core.ui.pipelines.panels.store_images.using_explicit_artifact_names":
      "Esta etapa está usando artifact_names explícitos (avançado). A seleção de fallback é ignorada até você limpá-los.",
    "core.ui.pipelines.panels.store_images.use_fallback_button": "Usar fallback (recomendado)",
    "core.ui.pipelines.panels.store_images.subdir": "Subdiretório",
    "core.ui.pipelines.panels.store_images.format": "Formato",
    "core.ui.pipelines.panels.store_images.jpeg_quality": "Qualidade do JPG",
    "core.ui.pipelines.panels.store_images.drop_data_after_store": "Descartar pixels após salvar",
    "core.ui.pipelines.panels.store_images.drop_data_after_store_hint": "Recomendado. Mantém a memória estável sob carga.",
    "core.ui.pipelines.panels.store_images.keep_data": "Manter dados na memória",
    "core.ui.pipelines.panels.store_images.keep_data_hint": "Se desativado, os pixels são descartados após salvar para manter a memória estável.",
    "core.ui.pipelines.panels.store_images.overwrite": "Sobrescrever arquivos existentes",

    "core.ui.pipelines.panels.notify.title_template": "Template do título",
    "core.ui.pipelines.panels.notify.title_placeholder": "{{object_category_label}} detectado",
    "core.ui.pipelines.panels.notify.template_hint_prefix": "Use templates como",
    "core.ui.pipelines.panels.notify.description_template": "Template da descrição",
    "core.ui.pipelines.panels.notify.description_placeholder": "Opcional",
    "core.ui.pipelines.panels.notify.priority": "Prioridade",
    "core.ui.pipelines.panels.notify.priority.low": "Baixa",
    "core.ui.pipelines.panels.notify.priority.medium": "Média",
    "core.ui.pipelines.panels.notify.priority.high": "Alta",
    "core.ui.pipelines.panels.notify.realtime": "Atualizações em tempo real",
    "core.ui.pipelines.panels.notify.update_interval_seconds": "Intervalo de atualização (segundos)",
    "core.ui.pipelines.panels.notify.update_interval_hint":
      "Evita spam de atualizações na UI enquanto o evento está aberto. Use 0 para emitir toda mudança.",
    "core.ui.pipelines.panels.notify.thumbnail_fallback": "Fallback de miniatura",
    "core.ui.pipelines.panels.notify.thumbnail_placeholder": "Best frame → Face → Segmented → Full frame",
    "core.ui.pipelines.panels.notify.thumbnail_hint":
      "Apenas registra notificações (nunca armazena imagens). Para incluir imagens, adicione Store Images antes desta etapa.",
    "core.ui.pipelines.panels.notify.notification_type": "Tipo de notificação",
    "core.ui.pipelines.panels.notify.dedupe_key_template": "Template da chave de dedupe",
    "core.ui.pipelines.panels.notify.dedupe_key_placeholder": "Deixe vazio para o padrão",
    "core.ui.pipelines.panels.notify.dedupe_key_hint_prefix": "Use templates como",

    "core.ui.pipelines.panels.camera_source.camera": "Câmera",
    "core.ui.pipelines.panels.camera_source.camera_placeholder": "Selecionar uma câmera…",
    "core.ui.pipelines.panels.camera_source.hint_infer":
      "RTSP URL, credenciais e FPS são inferidos do cadastro da câmera. Ative Avançado para sobrescrever.",
    "core.ui.pipelines.panels.camera_source.hint_no_cameras":
      "Nenhuma câmera encontrada. Configure câmeras nas configurações da extensão Cameras.",
    "core.ui.pipelines.panels.camera_source.backend": "Backend",
    "core.ui.pipelines.panels.camera_source.backend.auto": "Auto (recomendado)",
    "core.ui.pipelines.panels.camera_source.hint_backend":
      "Auto seleciona o melhor backend disponível e faz fallback automaticamente se um falhar ao iniciar.",

    "core.ui.pipelines.panels.camera_mapping.hint":
      "Usa pontos de controle definidos nas composições para mapear coordenadas imagem → mundo. Configure pontos de controle no editor de Composição.",
    "core.ui.pipelines.panels.camera_mapping.select_camera_error":
      "Selecione uma câmera na etapa Camera Source para mostrar o status do mapeamento.",
    "core.ui.pipelines.panels.camera_mapping.mapping_ready": "mapeamento pronto",
    "core.ui.pipelines.panels.camera_mapping.mapping_missing": "mapeamento ausente",
    "core.ui.pipelines.panels.camera_mapping.areas_count": "áreas: {{count}}",
    "core.ui.pipelines.panels.camera_mapping.camera_nodes": "nós de câmera: {{names}}",
    "core.ui.pipelines.panels.camera_mapping.load_failed": "Falha ao carregar contextos da câmera: {{error}}",
    "core.ui.pipelines.panels.camera_mapping.loading": "Carregando contextos da câmera…",

    "core.ui.pipelines.panels.area_restriction.areas": "Áreas",
    "core.ui.pipelines.panels.area_restriction.select_camera_first": "Selecione uma câmera primeiro…",
    "core.ui.pipelines.panels.area_restriction.select_areas": "Selecionar áreas…",
    "core.ui.pipelines.panels.area_restriction.select_camera_step_error": "Selecione uma câmera na etapa Camera Source primeiro.",
    "core.ui.pipelines.panels.area_restriction.load_failed": "Falha ao carregar contextos da câmera: {{error}}",
    "core.ui.pipelines.panels.area_restriction.loading": "Carregando contextos da câmera…",
    "core.ui.pipelines.panels.area_restriction.no_areas": "Nenhuma área encontrada nas composições para esta câmera.",
    "core.ui.pipelines.panels.area_restriction.hint_areas": "Usa áreas das composições em que a câmera selecionada está presente.",
    "core.ui.pipelines.panels.area_restriction.invalid_areas":
      "Algumas áreas selecionadas não estão disponíveis para esta câmera: {{areas}}",

    "core.ui.pipelines.panels.velocity.flow_mode": "Modo de fluxo",
    "core.ui.pipelines.panels.velocity.mode.annotate.label": "Apenas anotar",
    "core.ui.pipelines.panels.velocity.mode.annotate.hint": "Sempre emite pacotes; adiciona payload de velocidade.",
    "core.ui.pipelines.panels.velocity.mode.stopped_now.label": "Somente quando parado",
    "core.ui.pipelines.panels.velocity.mode.stopped_now.hint": "Emite pacotes apenas enquanto o objeto estiver parado.",
    "core.ui.pipelines.panels.velocity.mode.moving_now.label": "Somente quando em movimento",
    "core.ui.pipelines.panels.velocity.mode.moving_now.hint": "Emite pacotes apenas enquanto o objeto estiver em movimento.",
    "core.ui.pipelines.panels.velocity.mode.stopped_once.label": "Somente após parar uma vez",
    "core.ui.pipelines.panels.velocity.mode.stopped_once.hint": "Descarta pacotes até parar ao menos uma vez; depois passa todos.",
    "core.ui.pipelines.panels.velocity.mode.always_moving.label": "Somente enquanto nunca parou",
    "core.ui.pipelines.panels.velocity.mode.always_moving.hint": "Passa pacotes até parar uma vez; depois descarta o restante.",
    "core.ui.pipelines.panels.velocity.stopped_threshold": "Limite de parado (km/h)",
    "core.ui.pipelines.panels.velocity.hint":
      "Calcula a velocidade a partir de coordenadas no mundo (etapa Camera Mapping). Usa m/s internamente e também exibe km/h.",
    "core.ui.pipelines.panels.velocity.mapping_required": "Adicione Camera Mapping antes desta etapa para obter velocidade no mundo.",

    "core.ui.pipelines.panels.image_crop.hint":
      "Recorta o frame para análise downstream (YOLO). O frame cheio original é preservado como original.",
    "core.ui.pipelines.panels.image_crop.units": "Unidades",
    "core.ui.pipelines.panels.image_crop.units.percent": "Porcentagem (0–100)",
    "core.ui.pipelines.panels.image_crop.units.pixels": "Pixels",
    "core.ui.pipelines.panels.image_crop.left": "Esquerda",
    "core.ui.pipelines.panels.image_crop.top": "Topo",
    "core.ui.pipelines.panels.image_crop.right": "Direita",
    "core.ui.pipelines.panels.image_crop.bottom": "Baixo",
    "core.ui.pipelines.panels.image_crop.rectangle_hint":
      "O retângulo é definido por Left/Top/Right/Bottom (porcentagem do frame ou pixels a partir do canto superior esquerdo).",
    "core.ui.pipelines.panels.image_crop.reset": "Resetar",
    "core.ui.pipelines.panels.image_crop.output_artifact_name": "Nome do artefato de saída",
    "core.ui.pipelines.panels.image_crop.min_crop_size_px": "Tamanho mínimo do recorte (px)",
    "core.ui.pipelines.panels.image_crop.use_cropped_frame": "Usar frame recortado no fluxo",

    "core.ui.pipelines.panels.image_adjust.input_artifacts": "Artefatos de entrada (ordem de fallback)",
    "core.ui.pipelines.panels.image_adjust.input_artifacts_placeholder": "Full frame",
    "core.ui.pipelines.panels.image_adjust.input_artifacts_hint": "Usa a primeira imagem disponível. Mantenha original como fallback.",
    "core.ui.pipelines.panels.image_adjust.saturation": "Saturação",
    "core.ui.pipelines.panels.image_adjust.brightness": "Brilho",
    "core.ui.pipelines.panels.image_adjust.contrast": "Contraste",
    "core.ui.pipelines.panels.image_adjust.gamma": "Gamma",
    "core.ui.pipelines.panels.image_adjust.brightness_hint":
      "Brilho é um offset aditivo em espaço normalizado (ex.: 0.10 = +10%).",
    "core.ui.pipelines.panels.image_adjust.output_artifact_name": "Nome do artefato de saída",
    "core.ui.pipelines.panels.image_adjust.apply_stream_frame": "Aplicar no frame do fluxo",
    "core.ui.pipelines.panels.image_adjust.fallback_stream_frame": "Fallback para frame do fluxo",
    "core.ui.pipelines.panels.image_adjust.preserve_alpha": "Preservar canal alfa",

    "core.ui.pipelines.panels.image_resize.artifacts": "Artefatos",
    "core.ui.pipelines.panels.image_resize.artifacts_placeholder": "Full frame",
    "core.ui.pipelines.panels.image_resize.hint": "Redimensiona artefatos em memória antes do storage para manter tamanhos razoáveis.",
    "core.ui.pipelines.panels.image_resize.max_edge_px": "Maior borda (px)",
    "core.ui.pipelines.panels.image_resize.allow_upscale": "Permitir upscale",

    "core.ui.pipelines.panels.yolo.min_confidence": "Confiança mínima",
    "core.ui.pipelines.panels.yolo.min_confidence_hint": "Filtra detecções/tracks de baixa confiança (padrão: 0.40).",
    "core.ui.pipelines.panels.yolo.categories": "Categorias",
    "core.ui.pipelines.panels.yolo.categories_placeholder": "Todas as categorias",
    "core.ui.pipelines.panels.yolo.categories_hint": "Seleção vazia significa “todas as categorias”.",
    "core.ui.pipelines.panels.yolo.update_interval_tracking": "Intervalo de update (segundos)",
    "core.ui.pipelines.panels.yolo.update_interval_detection": "Intervalo de evento (segundos)",
    "core.ui.pipelines.panels.yolo.update_interval_hint":
      "Mínimo de segundos entre emissões por câmera + categoria. Use 0 apenas se quiser “todo frame” (pode sobrecarregar notify/storage/debug).",
    "core.ui.pipelines.panels.yolo.close_after_seconds": "Fechar após (segundos)",
    "core.ui.pipelines.panels.yolo.close_after_hint":
      "Fecha um track se o objeto não for visto por esse tempo (maior = mais estável, fecha mais devagar).",

    "core.ui.processing_servers.title": "Servidores de processamento",
    "core.ui.processing_servers.add_server": "Adicionar servidor",
    "core.ui.processing_servers.description":
      "Configure servidores de processamento remotos para executar operadores pesados (YOLO). Armazenamento e notificações continuam na origem.",
    "core.ui.processing_servers.none": "Nenhum servidor de processamento configurado.",
    "core.ui.processing_servers.built_in": "(integrado)",
    "core.ui.processing_servers.status.testing": "testando…",
    "core.ui.processing_servers.status.online": "online",
    "core.ui.processing_servers.status.offline": "offline",
    "core.ui.processing_servers.actions.test_connection": "Testar conexão",
    "core.ui.processing_servers.actions.edit_server": "Editar servidor",
    "core.ui.processing_servers.actions.delete_server": "Excluir servidor",
    "core.ui.processing_servers.confirm_delete": "Excluir servidor de processamento '{{id}}'?",

    "core.ui.processing_server_modal.title_add": "Adicionar servidor de processamento",
    "core.ui.processing_server_modal.title_edit": "Editar servidor de processamento",
    "core.ui.processing_server_modal.hint":
      "Rode o servidor de processamento em outra máquina e conecte aqui. Armazenamento e notificações continuam na origem.",
    "core.ui.processing_server_modal.field.id": "ID",
    "core.ui.processing_server_modal.field.name": "Nome (opcional)",
    "core.ui.processing_server_modal.field.scheme": "Esquema",
    "core.ui.processing_server_modal.field.host": "Host / IP",
    "core.ui.processing_server_modal.field.port": "Porta",
    "core.ui.processing_server_modal.field.username": "Usuário (opcional)",
    "core.ui.processing_server_modal.field.password": "Senha (opcional)",
    "core.ui.processing_server_modal.url_preview": "Prévia da URL: {{url}}",
    "core.ui.processing_server_modal.suggested_id": "ID sugerido: {{id}}",
    "core.ui.processing_server_modal.remote_command": "Comando remoto:",
    "core.ui.processing_server_modal.connection_ok": "Conexão: OK",
    "core.ui.processing_server_modal.connection_failed": "Conexão: {{error}}",
    "core.ui.processing_server_modal.actions.test_connection": "Testar conexão",

    "core.ui.settings.nav.pipelines.title": "Pipelines",
    "core.ui.settings.nav.pipelines.desc": "Crie e edite pipelines.",
    "core.ui.settings.nav.processing_servers.title": "Servidores de processamento",
    "core.ui.settings.nav.processing_servers.desc": "Gerencie servidores de processamento remotos.",
  },
};

let locale: Locale = resolveInitialLocale();
const listeners = new Set<() => void>();

function isLocale(value: unknown): value is Locale {
  return value === "en" || value === "pt-BR";
}

function safeGetStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSetStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // ignore
  }
}

function resolveInitialLocale(): Locale {
  const stored = safeGetStorage(STORAGE_KEY);
  if (isLocale(stored)) return stored;

  const nav = typeof navigator !== "undefined" ? navigator.language : "en";
  const lower = String(nav).toLowerCase();
  if (lower.startsWith("pt")) return "pt-BR";
  return "en";
}

function notify(): void {
  for (const l of listeners) l();
}

function interpolate(template: string, params: Record<string, unknown>): string {
  return template.replace(/{{\s*([\w.-]+)\s*}}/g, (_m, key: string) => {
    const value = params[key];
    if (value === null || value === undefined) return "";
    return String(value);
  });
}

export const i18n: I18nApi = {
  getLocale(): Locale {
    return locale;
  },
  setLocale(next: Locale): void {
    if (next === locale) return;
    locale = next;
    safeSetStorage(STORAGE_KEY, next);
    notify();
  },
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  registerTranslations(bundle: TranslationBundle): void {
    let changed = false;
    for (const [locKey, resources] of Object.entries(bundle)) {
      if (!isLocale(locKey)) continue;
      if (!resources) continue;
      Object.assign(translationsByLocale[locKey], resources);
      changed = true;
    }
    if (changed) notify();
  },
  t(key: string, params: Record<string, unknown> = {}, fallback?: string): string {
    const dict = translationsByLocale[locale];
    const base = dict[key] ?? translationsByLocale.en[key] ?? fallback ?? key;
    return Object.keys(params).length ? interpolate(base, params) : base;
  },
  useI18n(): { locale: Locale; t: I18nApi["t"]; setLocale: I18nApi["setLocale"] } {
    const current = useSyncExternalStore<Locale>(i18n.subscribe, i18n.getLocale, i18n.getLocale);
    return { locale: current, t: i18n.t, setLocale: i18n.setLocale };
  },
};

export function resolveLocalizedString(value: LocalizedString | undefined): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  return i18n.t(value.key, value.params ?? {}, value.fallback);
}
