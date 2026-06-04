export type Vector3 = { x: number; y: number; z: number };
export type Vector2 = { x: number; y: number };
export type PlanePoint = { x: number; z: number };

export type WallHeightPreset = "low" | "medium" | "high";

export type GraphicsQuality = "simplified" | "detailed";

export type ViewSettings = {
  wallHeightPreset: WallHeightPreset;
  wallHeight: number;
  ghostWalls?: boolean;
  graphicsQuality?: GraphicsQuality;
  renderViewSettings?: Record<string, Record<string, unknown>>;
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

export type NotificationOverlayActions = {
  openImage: (args: { url: string; title?: string; subtitle?: string }) => void;
};

export type NotificationOverlayPointerEvent = {
  kind: "click" | "dblclick" | "longpress";
  intersection: import("three").Intersection;
  notification: Notification;
};

export type Notification3DOverlay = {
  object: import("three").Object3D;
  /**
   * Called before a rendered frame. Return true while the overlay needs
   * continuous frames; return false when it can sleep until the host is
   * invalidated again. A void return is treated as a one-shot tick by hosts
   * that render on demand.
   */
  tick?: (deltaSeconds: number) => boolean | void;
  update?: (notification: Notification) => void;
  onPointerEvent?: (event: NotificationOverlayPointerEvent) => boolean | void;
  dispose?: () => void;
};

export type Notification2DPin = {
  /** World-space anchor coordinates. The pin tip lands here. */
  x: number;
  z: number;
  /** Optional history of past world positions. Drawn as a polyline beneath the pin. */
  trail?: ReadonlyArray<{ x: number; z: number }>;
  /** Used by the host to tint the pin (cyan/medium by default, red/high, gray/low). */
  priority?: "low" | "medium" | "high";
  /** When true the host dims the pin to indicate the notification was closed. */
  closed?: boolean;
};

export type Notification2DContext = {
  /** Composition currently shown in the viewport. Renderers should return null when the pin
   * belongs to another composition. */
  compositionId?: string;
};

export type Notification2DOverlay = {
  /** Resolve the current pin. Called whenever the host needs to redraw — return null to hide. */
  pin: () => Notification2DPin | null;
  /** Called when the same notification arrives with new payload data (e.g. tracking updates). */
  update?: (notification: Notification) => void;
  dispose?: () => void;
};

export type NotificationRenderer = {
  id: string;
  type: string;
  render: (notification: Notification) => import("react").ReactNode;
  create3DOverlay?: (
    ctx: Scene3DContext,
    notification: Notification,
    actions: NotificationOverlayActions,
  ) => Notification3DOverlay | null;
  create2DOverlay?: (
    ctx: Notification2DContext,
    notification: Notification,
    actions: NotificationOverlayActions,
  ) => Notification2DOverlay | null;
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

export type PipelineOperatorPanel = {
  id: string;
  operatorId: string;
  render: (args: {
    i18n: HostI18n;
    operatorId: string;
    stepUid: string;
    nodeId: string;
    config: Record<string, unknown>;
    showAdvanced: boolean;
    updateConfig: (patch: Record<string, unknown>) => void;
    replaceConfig: (next: Record<string, unknown>) => void;
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
  elements: CompositionElement[];
  compositionId?: string;
  requestRender?: () => void;
};

export type Element3DInstance = {
  object: import("three").Object3D;
  /**
   * Called before a rendered frame. Return true while the element needs
   * continuous frames; return false when it can sleep until the host is
   * invalidated again. A void return is treated as a one-shot tick by hosts
   * that render on demand.
   */
  tick?: (deltaSeconds: number) => boolean | void;
  update?: (element: CompositionElement, ctx?: { elements: CompositionElement[] }) => void;
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
    elements: CompositionElement[];
    viewport: Viewport2DContext;
  }) => void;
  getMain2DBounds?: (element: CompositionElement) => BoundsXZ | null;
  renderMain2DVector?: (args: {
    element: CompositionElement;
    elements: CompositionElement[];
    ctx: Main2DVectorContext;
  }) => import("react").ReactNode;
  getMain2DMarker?: (args: { element: CompositionElement }) => Main2DMarker | null;
  subscribeMain2DState?: (args: { element: CompositionElement; invalidate: () => void }) => (() => void) | void;
  getMain2DEffectTargets?: (args: {
    element: CompositionElement;
    elements: CompositionElement[];
  }) => Main2DEffectTarget[];
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
    elements: CompositionElement[];
    elementTypesById: Record<string, ElementType>;
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

export type BoundsXZ = {
  minX: number;
  maxX: number;
  minZ: number;
  maxZ: number;
};

export type Main2DVectorContext = {
  bounds: BoundsXZ;
  scale: number;
  themeId?: string;
};

export type Main2DMarkerState = "on" | "off" | "unknown" | "neutral";

export type Main2DMarker = {
  id?: string;
  elementId?: string;
  x: number;
  z: number;
  title: string;
  subtitle?: string;
  icon?: string;
  state?: Main2DMarkerState | null;
  className?: string;
};

export type Main2DEffectBlendMode = "source-over" | "screen";

export type Main2DEffectTarget = {
  id: string;
  element: CompositionElement;
  baseElement?: CompositionElement;
  signature?: unknown;
  warmupSeconds?: number;
  hideNonLightRenderables?: boolean;
  blendMode?: Main2DEffectBlendMode;
  opacity?: number;
};

export type EditorToolPointerEvent = {
  kind: "down" | "move" | "up" | "cancel" | "dblclick";
  world: PlanePoint;
  rawWorld: PlanePoint;
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
  getElements: () => CompositionElement[];
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
  shouldCapturePointer?: (event: EditorToolPointerEvent) => boolean;
  onPointerEvent?: (event: EditorToolPointerEvent) => void;
  onKeyDown?: (event: KeyboardEvent) => void;
  renderOverlay2D?: (args: { ctx: CanvasRenderingContext2D; viewport: Viewport2DContext }) => void;
  getCursor?: () => string;
  dispose?: () => void;
};

export type EditorFileDropEvent = {
  files: File[];
  world: PlanePoint;
  screen: Vector2;
  viewport: Viewport2DContext;
};

export type FileDropHandlerContext = {
  i18n: HostI18n;
  api: HostApi;
  compositionId?: string;
  elements: CompositionElement[];
  createElement: EditorToolContext["createElement"];
  openEditor: EditorToolContext["openEditor"];
};

export type FileDropHandler = {
  id: string;
  canHandle?: (event: EditorFileDropEvent) => boolean;
  handle: (ctx: FileDropHandlerContext, event: EditorFileDropEvent) => boolean | void | Promise<boolean | void>;
};

export type Viewport2DReplicaProps = {
  className?: string;
  style?: import("react").CSSProperties;
  session?: EditorToolSession | null;
  initialFit?: "content";
  interactionMode?: "navigate" | "select";
  minScale?: number;
  maxScale?: number;
};

export type LiveViewPlayerProps = {
  cameraId?: string;
  liveViewId?: string;
  context?: "thumbnail" | "large" | "fullscreen" | "pip" | "ptz" | "spatial_map";
  className?: string;
  style?: import("react").CSSProperties;
};

export type HostUi = {
  Viewport2DReplica: (props: Viewport2DReplicaProps) => import("react").ReactNode;
  LiveViewPlayer?: (props: LiveViewPlayerProps) => import("react").ReactNode;
};

export type RenderViewContext = {
  compositionId: string;
  compositionName: string;
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  viewSettings: ViewSettings;
  activeNotification?: Notification | null;
  activeNotificationRenderer?: NotificationRenderer | null;
  onElementActivated?: (elementId: string, intent?: "click" | "dblclick" | "longpress") => void;
  onOpenImage?: (args: { url: string; title?: string; subtitle?: string }) => void;
};

export type RenderViewDefinition = {
  id: string;
  name: LocalizedString;
  description?: LocalizedString;
  icon?: string;
  order?: number;
  render: (ctx: RenderViewContext) => import("react").ReactNode;
  renderSettings?: (ctx: {
    i18n: HostI18n;
    settings: Record<string, unknown>;
    updateSettings: (patch: Record<string, unknown>) => void;
  }) => import("react").ReactNode;
};

export type EditorToolGroup = {
  id: string;
  name?: LocalizedString;
  order?: number;
};

export type EditorTool = {
  id: string;
  name: LocalizedString;
  description?: LocalizedString;
  icon?: string;
  group?: EditorToolGroup;
  order?: number;
  createSession: (ctx: EditorToolContext) => EditorToolSession;
};

export type ToposyncHost = {
  registerElementType: (elementType: ElementType) => void;
  registerNotificationRenderer: (renderer: NotificationRenderer) => void;
  registerEditorTool: (tool: EditorTool) => void;
  registerFileDropHandler: (handler: FileDropHandler) => void;
  registerSettingsPanel: (panel: SettingsPanel) => void;
  registerPipelineOperatorPanel: (panel: PipelineOperatorPanel) => void;
  registerRenderView: (view: RenderViewDefinition) => void;
  registerTheme: (theme: ThemeDefinition) => void;
  api: HostApi;
  i18n: HostI18n;
  ui: HostUi;
};

export function getToposyncBasePath(): string;
export function resolveToposyncUrl(url: string): string;
