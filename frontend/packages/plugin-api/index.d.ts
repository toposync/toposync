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

/** Source publication/capture provenance; media timestamps are not Unix clocks. */
export type NotificationCaptureEvidence = {
  capture_instance: string;
  generation: number;
  sequence: number;
  published_at: number;
  physical_timestamp_verified?: boolean;
  [key: string]: unknown;
};

/** In-memory selected-detail image only. Never persist or merge into notification lists. */
export type NotificationEphemeralImage = {
  schemaVersion: 1;
  packetId: string;
  parentPacketId: string | null;
  cameraId: string;
  sourceStreamId: string;
  artifactName: string;
  mediaTimestamp: number;
  captureEvidence: NotificationCaptureEvidence;
  imageGeometry: {
    image_size: [number, number];
    source_size: [number, number];
    to_source: [[number, number, number], [number, number, number], [number, number, number]];
    capture_evidence: NotificationCaptureEvidence;
    [key: string]: unknown;
  };
  width: number;
  height: number;
  mimeType: "image/jpeg" | "image/png" | "image/webp";
  dataBase64: string;
  /** Unix milliseconds, at most 750 ms after original source publication. */
  expiresAt: number;
};

export type Notification = {
  id: string;
  type: string;
  title: string;
  description?: string;
  imageUrl?: string;
  createdAt?: string;
  updatedAt?: string;
  priority?: "low" | "medium" | "high";
  payload?: unknown;
  ephemeralImage?: NotificationEphemeralImage;
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
  /** Bounds of valid spatial evidence, excluding decorative marker geometry. */
  bounds?: () => { minX: number; maxX: number; minZ: number; maxZ: number } | null;
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
  /** Independent subjects and continuous segments; never connect separate entries. */
  trails?: ReadonlyArray<ReadonlyArray<{ x: number; z: number }>>;
  /** Used by the host to tint the pin (cyan/medium by default, red/high, gray/low). */
  priority?: "low" | "medium" | "high";
  /** When true the host dims the pin to indicate the notification was closed. */
  closed?: boolean;
};

export type Notification2DContext = {
  /** Composition currently shown in the viewport. Renderers should return null when the pin
   * belongs to another composition. */
  compositionId?: string;
  /** Current displayed elements, including unsaved map edits. */
  elements?: CompositionElement[];
  /** Invalidate a cached pin after asynchronous changes such as evidence expiry. */
  requestRender?: () => void;
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
  /** Optional selected-detail content. Only this surface receives ephemeral pixels. */
  renderDetails?: (notification: Notification) => import("react").ReactNode;
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

/** Adds authorized details without replacing a notification's image or overlays. */
export type NotificationDetailPanel = {
  /** Present a structured card summary; full details and original payload remain unchanged. */
  replacesRendererSummary?: boolean;
  id: string;
  supports: (notification: Notification) => boolean;
  render: (notification: Notification) => import("react").ReactNode;
  /** Passive content only: the host card already owns its button. */
  renderSummary?: (notification: Notification) => import("react").ReactNode;
};

export type SettingsPanel = {
  id: string;
  managesOwnPersistence?: boolean;
  pipelineAssistant?: boolean;
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

/** Read-only review of a graph owned by an extension; editing stays with the host. */
export type PipelineReviewPanel = {
  id: string;
  supports: (graph: unknown) => boolean;
  render: (graph: unknown) => import("react").ReactNode;
};

export type PipelineOperatorPanel = {
  id: string;
  operatorId: string;
  placement?: "replace" | "after";
  renderSummary?: (config: Record<string, unknown>) => import("react").ReactNode;
  renderStatus?: (status: Record<string, unknown>) => import("react").ReactNode;
  render: (args: {
    i18n: HostI18n;
    operatorId: string;
    stepUid: string;
    nodeId: string;
    /** Execution host chosen by this pipeline; older hosts may omit this field. */
    processingServerId?: string;
    cameraId?: string;
    config: Record<string, unknown>;
    showAdvanced: boolean;
    purpose?: "automatic" | "manual";
    sources?: Array<{ camera_id: string; source_id: string; camera_name: string; source_name: string }>;
    observationCategories?: string[];
    onValidityChange?: (valid: boolean) => void;
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
  fetch: (input: string, init?: RequestInit) => Promise<Response>;
  resolveUrl: (url: string) => string;
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
    /** Composition owning the element, for contextual navigation and return. */
    compositionId?: string;
    /** Transient action selected by the extension when opening this editor. */
    initialAction?: string;
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
  /** Centre this view without changing its scale or display rotation. */
  centerOn?: (point: PlanePoint) => void;
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
  /** Optional starting center in composition coordinates. */
  initialCenter?: PlanePoint;
  /** Pixels per meter used with initialCenter. */
  initialScale?: number;
  /** Initial visible span in metres on the shorter viewport axis; takes precedence over initialScale. */
  initialSpan?: number;
  interactionMode?: "navigate" | "select";
  minScale?: number;
  maxScale?: number;
  /** Presentation-only clockwise rotation. It never changes composition coordinates. */
  displayRotationDegrees?: 0 | 90 | 180 | 270;
};

/** A frame presented by the existing player, with local media provenance only. */
export type LiveViewFrame = {
  opticalSourceSize?: { width: number; height: number };
  contentRect?: { x: number; y: number; width: number; height: number };
  image: HTMLVideoElement | HTMLCanvasElement;
  width: number;
  height: number;
  epoch: string;
  sequence: number;
  mediaTime: number;
  presentedAt: number;
  timingBasis: "browser_presented_frame";
  cameraId?: string;
  sourceId?: string;
};

export type LiveViewPlayerProps = {
  sourceId?: string;
  /** Whether transport, decode and presented-frame delivery should be active. */
  active?: boolean;
  controls?: boolean;
  onFrame?: (frame: LiveViewFrame | null) => void;
  cameraId?: string;
  liveViewId?: string;
  context?: "thumbnail" | "large" | "fullscreen" | "pip" | "ptz" | "spatial_map";
  className?: string;
  style?: import("react").CSSProperties;
};

/** Allowlisted passive inspection. A complete snapshot replaces previous state. */
export type InspectionNode = {
  node_id: string;
  physical_node_id: string;
  operator: string;
  state: "waiting" | "received" | "started" | "analyzed" | "passed" | "filtered" | "emitted" | "delivered" | "dropped" | "error" | "expired" | "closed" | "completed";
  reason: string | null;
  sequence: number;
  producer_epoch: string;
  frame: Record<string, unknown> | null;
  detections: Array<{ box: number[]; label: string; score: number; score_kind: string }>;
  geometry: Record<string, unknown> | null;
  motion: Record<string, number> | null;
  expires_at: number | null;
  image_available: boolean;
  image_sequence: number | null;
  received_count: number;
  produced_count: number;
  filtered_count: number;
  model_id: string | null;
};

export type InspectionSnapshot = {
  session: string;
  pipeline: string;
  revision: string;
  sequence: number;
  nodes: InspectionNode[];
  dropped: number;
  alignment: "analyzed_frame_only";
  data_class: "diagnostic_private";
  access_scope: "owner";
  policy_revision: string | null;
};

export type NavigableViewportPoint = { x: number; y: number };
export type NavigableViewportBounds = NavigableViewportPoint & { width: number; height: number };
export type NavigableViewportState = { center: NavigableViewportPoint; scale: number; zoom: number };
export type NavigableViewportController = {
  fit: (bounds?: NavigableViewportBounds) => void;
  zoomBy: (factor: number) => void;
  centerOn: (point: NavigableViewportPoint) => void;
  contentToScreen: (point: NavigableViewportPoint) => NavigableViewportPoint;
  screenToContent: (point: NavigableViewportPoint) => NavigableViewportPoint;
};
/** A content plane in arbitrary units. Navigation never mutates the content. */
export type NavigableViewportProps = {
  contentSize: { width: number; height: number };
  contentKey?: string;
  initialBounds?: NavigableViewportBounds;
  minZoom?: number;
  maxZoom?: number;
  interactionMode?: "navigate" | "interact";
  controllerRef?: import("react").MutableRefObject<NavigableViewportController | null>;
  viewportRef?: import("react").Ref<HTMLDivElement>;
  className?: string;
  style?: import("react").CSSProperties;
  label: string;
  children: import("react").ReactNode | ((state: NavigableViewportState) => import("react").ReactNode);
  onViewChange?: (state: NavigableViewportState) => void;
  /** Cancel any in-progress editing gesture when navigation takes ownership. */
  onNavigationStart?: () => void;
  onContentClick?: (point: NavigableViewportPoint, event: import("react").PointerEvent<HTMLDivElement>) => void;
  onContentPointerMove?: (point: NavigableViewportPoint, event: import("react").PointerEvent<HTMLDivElement>) => void;
  /** Called first. preventDefault reserves a key for the consumer's tool. */
  onKeyDown?: (event: import("react").KeyboardEvent<HTMLDivElement>) => void;
  onPointerLeave?: () => void;
  onBlur?: () => void;
};

/** Decoder continuity, not certification of the physical camera pose. */
export type CameraSnapshotBinding = {
  camera_id: string;
  source_id: string;
  capture_instance: string;
  generation: number;
  width: number;
  height: number;
};

export type ImageMaskEditorProps = {
  wrapHorizontal?: boolean;
  open: boolean;
  imageUrl: string;
  title: string;
  strokes: { op: "paint" | "erase"; points01: [number, number][]; diameter01: number }[];
  onApply: (strokes: ImageMaskEditorProps["strokes"]) => void;
  onClose: () => void;
};

export type CameraRegionEditorProps = {
  open: boolean;
  cameraId: string;
  sourceId: string;
  values: { left: number; top: number; right: number; bottom: number };
  onChange: (values: CameraRegionEditorProps["values"]) => void;
  /** Null while loading, on missing evidence, or after changing the source. */
  onSnapshotBindingChange?: (binding: CameraSnapshotBinding | null) => void;
  onConfirm: (values: CameraRegionEditorProps["values"], binding: CameraSnapshotBinding) => void;
  onClose: () => void;
};

export type HostUi = {
  ImageMaskEditor: import("react").ComponentType<ImageMaskEditorProps>;
  openNotificationHistory?: (selection: { id: string; value: string }, returnToNotification?: string) => void;
  /** Draw a region in percent on a snapshot of the explicitly selected source. */
  CameraRegionEditor?: (props: CameraRegionEditorProps) => import("react").ReactNode;
  NavigableViewport: (props: NavigableViewportProps) => import("react").ReactNode;
  Viewport2DReplica: (props: Viewport2DReplicaProps) => import("react").ReactNode;
  LiveViewPlayer?: (props: LiveViewPlayerProps) => import("react").ReactNode;
};

export type RenderViewContext = {
  onConfigureElement?: (elementId?: string, initialAction?: string) => void;
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

export type RenderViewToolbarSelector = {
  /** Title used by the host-owned selection modal. */
  title: LocalizedString;
  /** Compact control rendered in the main toolbar, in place of the composition selector. */
  renderTrigger: (ctx: { open: () => void }) => import("react").ReactNode;
  /** Contents rendered inside the host-owned modal. */
  renderContent: (ctx: { close: () => void }) => import("react").ReactNode;
};

export type RenderViewDefinition = {
  group?: "space" | "cameras" | "panorama";
  /** Preconditions only: an available view still validates its sources when opened. */
  availability?: (context: { elements: CompositionElement[] }) => {
    available: boolean; reason: LocalizedString; elementId?: string; initialAction?: string;
  };
  id: string;
  name: LocalizedString;
  description?: LocalizedString;
  icon?: string;
  order?: number;
  toolbarSelector?: RenderViewToolbarSelector;
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

/** Extension-owned selection; endpoint returns an authorized NotificationsPage.
 * Selection is opaque to the host. Missing providers must never fall back globally.
 */
export type NotificationHistoryFilter = {
  id: string;
  renderSummary?: (context: { value: string }) => import("react").ReactNode;
  render: (context: { value: string | null; onChange: (value: string | null) => void }) => import("react").ReactNode;
  endpoint: (value: string) => string;
  subscribe?: (invalidate: () => void) => () => void;
};

/** Optional extension-owned history consultation. Opening a source loads its authorized details. */
export type NotificationHistoryAssistant = {
  id: string;
  name: LocalizedString;
  render: (context: {
    query: string;
    dataFilter?: Record<string, string>;
    selection?: { id: string; value: string };
    priorities: string[];
    types: string[];
    visual: boolean;
    createdAfter?: number;
    createdBefore?: number;
    onOpenNotification: (id: string) => Promise<void>;
  }) => import("react").ReactNode;
};

export type ToposyncHost = {
  registerElementType: (elementType: ElementType) => void;
  registerNotificationRenderer: (renderer: NotificationRenderer) => void;
  registerNotificationHistoryFilter?: (filter: NotificationHistoryFilter) => void;
  registerNotificationHistoryAssistant?: (assistant: NotificationHistoryAssistant) => void;
  registerNotificationDetailPanel?: (panel: NotificationDetailPanel) => void;
  registerEditorTool: (tool: EditorTool) => void;
  registerFileDropHandler: (handler: FileDropHandler) => void;
  registerSettingsPanel: (panel: SettingsPanel) => void;
  registerPipelineReviewPanel?: (panel: PipelineReviewPanel) => void;
  registerPipelineOperatorPanel: (panel: PipelineOperatorPanel) => void;
  registerRenderView: (view: RenderViewDefinition) => void;
  registerTheme: (theme: ThemeDefinition) => void;
  api: HostApi;
  i18n: HostI18n;
  ui: HostUi;
};

export function getToposyncBasePath(): string;
export function resolveToposyncUrl(url: string): string;
export function requestJson<T = unknown>(input: string, init?: RequestInit): Promise<T>;
export function requestVoid(input: string, init?: RequestInit): Promise<void>;
export function requestForm<T = unknown>(input: string, form: FormData, init?: RequestInit): Promise<T>;
/** Share keyboard ownership between host and extension body-portal dialogs.
 * Call on open and invoke the returned cleanup on close/unmount.
 * Additive local API: older hosts may not export these functions at runtime.
 */
export declare function activateModalFocus(panel: HTMLElement, onClose: () => void): () => void;
export declare function isActiveModalFocus(panel: HTMLElement | null): boolean;
