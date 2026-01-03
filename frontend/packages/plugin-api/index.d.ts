export type ToolContribution = {
  id: string;
  label: string;
  onTrigger: () => void | Promise<void>;
};

export type PanelContribution = {
  id: string;
  title: string;
  render: () => import("react").ReactNode;
};

export type Overlay3DContribution = {
  id: string;
  mount: (ctx: {
    THREE: typeof import("three");
    scene: import("three").Scene;
    camera: import("three").Camera;
    renderer: import("three").WebGLRenderer;
  }) => void | (() => void);
};

export type Notification = {
  id: string;
  type: string;
  title: string;
  createdAt?: string;
  payload?: unknown;
};

export type NotificationRenderer = {
  id: string;
  type: string;
  render: (notification: Notification) => import("react").ReactNode;
};

export type EmitEventResponse = {
  payload: unknown;
  result: any;
  prevented_default: boolean;
  stopped: boolean;
};

export type TopoSyncHost = {
  registerTool: (tool: ToolContribution) => void;
  registerPanel: (panel: PanelContribution) => void;
  registerOverlay3D: (overlay: Overlay3DContribution) => void;
  registerNotificationRenderer: (renderer: NotificationRenderer) => void;
  api: {
    emitEvent: (eventName: string, payload: unknown, context?: Record<string, unknown>) => Promise<EmitEventResponse>;
    getDevice: (deviceId: string) => Promise<{ device_id: string; state: boolean }>;
  };
};
