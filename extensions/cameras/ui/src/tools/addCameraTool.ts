import type { EditorTool, HostI18n } from "@toposync/plugin-api";

import { ADD_CAMERA_TOOL_ID, CAMERA_ELEMENT_TYPE_ID } from "../constants";

const TOOL_GROUP_DEVICES: NonNullable<EditorTool["group"]> = {
  id: "devices",
  name: { key: "core.ui.tools.group.devices", fallback: "Devices" },
  order: 40,
};

export function createAddCameraTool(i18n: HostI18n): EditorTool {
  return {
    id: ADD_CAMERA_TOOL_ID,
    name: { key: "ext.cameras.tool.add", fallback: "Camera" },
    description: { key: "ext.cameras.tool.add_desc" },
    icon: "video",
    group: TOOL_GROUP_DEVICES,
    order: 10,
    createSession: ({ createElement, openEditor }) => ({
      onPointerEvent: (event) => {
        if (event.kind !== "down") return;
        if (event.button !== 0) return;
        const createdElementId = createElement(CAMERA_ELEMENT_TYPE_ID, {
          position: { x: event.world.x, y: 0, z: event.world.z },
          props: { camera_id: "", camera_name: "", view_mode: "ceiling" },
        });
        if (createdElementId) openEditor(createdElementId);
      },
    }),
  };
}
