import type { EditorTool, HostI18n } from "@toposync/plugin-api";

import { ADD_HOME_ASSISTANT_TOOL_ID, DEFAULT_AIRFLOW_INTENSITY, DEFAULT_LAMP_COLOR, DEFAULT_LAMP_INTENSITY, HOME_ASSISTANT_ELEMENT_TYPE_ID } from "../constants";

const TOOL_GROUP_DEVICES: NonNullable<EditorTool["group"]> = {
  id: "devices",
  name: { key: "core.ui.tools.group.devices", fallback: "Devices" },
  order: 40,
};

export function createAddHomeAssistantTool(i18n: HostI18n): EditorTool {
  return {
    id: ADD_HOME_ASSISTANT_TOOL_ID,
    name: { key: "ext.home_assistant.tool.add", fallback: "Device" },
    description: { key: "ext.home_assistant.tool.add_desc" },
    icon: "house-signal",
    group: TOOL_GROUP_DEVICES,
    order: 20,
    createSession: ({ createElement, openEditor }) => ({
      onPointerEvent: (event) => {
        if (event.kind !== "down") return;
        if (event.button !== 0) return;
        const id = createElement(HOME_ASSISTANT_ELEMENT_TYPE_ID, {
          name: "",
          position: { x: event.world.x, y: 0, z: event.world.z },
          props: {
            server_id: "",
            items: [],
            icon: "house",
            primary_entity_id: "",
            primary_state: "",
            view_mode: "floor",
            special_view: "none",
            lamp_intensity: DEFAULT_LAMP_INTENSITY,
            lamp_color: DEFAULT_LAMP_COLOR,
            airflow_intensity: DEFAULT_AIRFLOW_INTENSITY,
          },
        });
        if (id) openEditor(id);
      },
    }),
  };
}
