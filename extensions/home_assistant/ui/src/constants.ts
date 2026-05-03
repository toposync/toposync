export const HOME_ASSISTANT_EXTENSION_ID = "com.toposync.home_assistant";
export const HOME_ASSISTANT_ELEMENT_TYPE_ID = "com.toposync.home_assistant.item";
export const ADD_HOME_ASSISTANT_TOOL_ID = "com.toposync.home_assistant.tool.add";

export const PRIMARY_TOGGLE_DOMAINS = new Set([
  "light",
  "switch",
  "fan",
  "input_boolean",
  "lock",
  "cover",
  "climate",
  "humidifier",
]);

export const BOOLEAN_STATE_DOMAINS = new Set([...PRIMARY_TOGGLE_DOMAINS, "binary_sensor"]);

export const LAMP_COMPATIBLE_DOMAINS = new Set(["light", "switch", "fan", "input_boolean", "humidifier", "binary_sensor"]);
export const DEFAULT_LAMP_COLOR = "#ffe8b0";
export const MIN_LAMP_INTENSITY = 0.2;
export const MAX_LAMP_INTENSITY = 10.0;
export const DEFAULT_LAMP_INTENSITY = 3.0;

export const AIRFLOW_COMPATIBLE_DOMAINS = new Set(["climate"]);
export const DEFAULT_AIRFLOW_INTENSITY = 1.0;

export const HOME_ASSISTANT_LIVE_DEBUG_STORAGE_KEY = "toposync:debug_ha";
export const HOME_ASSISTANT_STREAM_MAX_ENTITY_IDS = 300;
export const HOME_ASSISTANT_STREAM_REFRESH_DELAY_MS = 180;
export const HOME_ASSISTANT_REST_REFRESH_DELAY_MS = 60;
export const HOME_ASSISTANT_RECONNECT_INITIAL_DELAY_MS = 500;
export const HOME_ASSISTANT_RECONNECT_MAX_DELAY_MS = 10_000;
