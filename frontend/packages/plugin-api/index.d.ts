export type Vector3 = { x: number; y: number; z: number };
export type Vector2 = { x: number; y: number };
export type PlanePoint = { x: number; z: number };

export type WallHeightPreset = "low" | "medium" | "high";

export type ViewSettings = {
  wallHeightPreset: WallHeightPreset;
  wallHeight: number;
};

export type Locale = "en" | "pt-BR";

export type LocalizedString =
  | string
  | {
      key: string;
      params?: Record<string, unknown>;
      fallback?: string;
    };

export type TranslationBundle = Partial<Record<Locale, Record<string, string>>>;

export type HostI18n = {
  getLocale: () => Locale;
  setLocale: (locale: Locale) => void;
  t: (key: string, params?: Record<string, unknown>, fallback?: string) => string;
  registerTranslations: (bundle: TranslationBundle) => void;
  subscribe: (listener: () => void) => () => void;
  useI18n: () => {
    locale: Locale;
    t: (key: string, params?: Record<string, unknown>, fallback?: string) => string;
    setLocale: (locale: Locale) => void;
  };
};

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
  description?: string;
  imageUrl?: string;
  createdAt?: string;
  updatedAt?: string;
  payload?: unknown;
};

export type NotificationRenderer = {
  id: string;
  type: string;
  render: (notification: Notification) => import("react").ReactNode;
};

export type SettingsPanel = {
  id: string;
  name: LocalizedString;
  description?: LocalizedString;
  icon?: string;
  render: (args: {
    i18n: HostI18n;
    api: HostApi;
    settings: Record<string, unknown>;
    updateSettings: (patch: Record<string, unknown>) => void;
  }) => import("react").ReactNode;
};

export type ThemeDefinition = {
  id: string;
  name: LocalizedString;
  description?: LocalizedString;
  vars?: Record<string, string>;
  css?: string;
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
  view: ViewSettings;
};

export type Element3DInstance = {
  object: import("three").Object3D;
  tick?: (deltaSeconds: number) => void;
  update?: (element: CompositionElement) => void;
  dispose?: () => void;
};

export type ElementType = {
  type: string;
  name: LocalizedString;
  description?: LocalizedString;
  layerGroup?: string;
  placeable?: boolean;
  defaultProps?: Record<string, unknown>;
  primaryAction?: (args: {
    element: CompositionElement;
    api: HostApi;
    update: (patch: CompositionElementPatch) => void;
  }) => Promise<boolean> | boolean;
  create3D?: (ctx: Scene3DContext, element: CompositionElement) => Element3DInstance;
  render2D?: (args: {
    ctx: CanvasRenderingContext2D;
    element: CompositionElement;
    viewport: Viewport2DContext;
  }) => void;
  hitTest2D?: (args: { element: CompositionElement; world: PlanePoint; viewport: Viewport2DContext }) => boolean;
  translate2D?: (args: { element: CompositionElement; delta: PlanePoint }) => CompositionElementPatch;
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

export type Viewport2DContext = {
  canvas: HTMLCanvasElement;
  width: number;
  height: number;
  dpr: number;
  worldToScreen: (p: PlanePoint) => Vector2;
  screenToWorld: (p: Vector2) => PlanePoint;
  scale: number;
};

export type EditorToolPointerEvent = {
  kind: "down" | "move" | "up" | "cancel" | "dblclick";
  world: PlanePoint;
  screen: Vector2;
  button: number;
  buttons: number;
  pointerType: string;
  shiftKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
};

export type EditorToolContext = {
  i18n: HostI18n;
  createElement: (
    typeId: string,
    init?: Partial<Omit<CompositionElement, "id" | "type">>,
  ) => string | null;
  updateElement: (elementId: string, patch: CompositionElementPatch) => void;
  removeElement: (elementId: string) => void;
  openEditor: (elementId: string) => void;
  closeEditor: () => void;
};

export type EditorToolSession = {
  onPointerEvent?: (event: EditorToolPointerEvent) => void;
  onKeyDown?: (event: KeyboardEvent) => void;
  renderOverlay2D?: (args: { ctx: CanvasRenderingContext2D; viewport: Viewport2DContext }) => void;
  getCursor?: () => string;
  dispose?: () => void;
};

export type Viewport2DReplicaProps = {
  className?: string;
  style?: import("react").CSSProperties;
  session?: EditorToolSession | null;
};

export type HostUi = {
  Viewport2DReplica: (props: Viewport2DReplicaProps) => import("react").ReactNode;
};

export type EditorTool = {
  id: string;
  name: LocalizedString;
  description?: LocalizedString;
  icon?: string;
  createSession: (ctx: EditorToolContext) => EditorToolSession;
};

export type TopoSyncHost = {
  registerElementType: (elementType: ElementType) => void;
  registerNotificationRenderer: (renderer: NotificationRenderer) => void;
  registerEditorTool: (tool: EditorTool) => void;
  registerSettingsPanel: (panel: SettingsPanel) => void;
  registerTheme: (theme: ThemeDefinition) => void;
  api: HostApi;
  i18n: HostI18n;
  ui: HostUi;
};
