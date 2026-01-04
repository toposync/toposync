export type Vector3 = { x: number; y: number; z: number };

export type CompositionElement = {
  id: string;
  type: string;
  name: string;
  position: Vector3;
  rotation: Vector3;
  props: Record<string, unknown>;
};

export type CompositionElementPatch = Partial<Omit<CompositionElement, "position" | "rotation" | "props">> & {
  position?: Partial<Vector3>;
  rotation?: Partial<Vector3>;
  props?: Record<string, unknown>;
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

export type HostApi = {
  emitEvent: (eventName: string, payload: unknown, context?: Record<string, unknown>) => Promise<EmitEventResponse>;
  getDevice: (deviceId: string) => Promise<{ device_id: string; state: boolean }>;
};

export type Scene3DContext = {
  THREE: typeof import("three");
  scene: import("three").Scene;
  camera: import("three").Camera;
  renderer: import("three").WebGLRenderer;
};

export type Element3DInstance = {
  object: import("three").Object3D;
  update?: (element: CompositionElement) => void;
  dispose?: () => void;
};

export type ElementType = {
  type: string;
  name: string;
  description?: string;
  defaultProps?: Record<string, unknown>;
  create3D?: (ctx: Scene3DContext, element: CompositionElement) => Element3DInstance;
  renderActionModal?: (args: {
    element: CompositionElement;
    update: (patch: CompositionElementPatch) => void;
    close: () => void;
    api: HostApi;
  }) => import("react").ReactNode;
  renderEditorModal?: (args: {
    element: CompositionElement;
    update: (patch: CompositionElementPatch) => void;
    remove: () => void;
    close: () => void;
  }) => import("react").ReactNode;
};

export type TopoSyncHost = {
  registerElementType: (elementType: ElementType) => void;
  registerNotificationRenderer: (renderer: NotificationRenderer) => void;
  api: HostApi;
};
