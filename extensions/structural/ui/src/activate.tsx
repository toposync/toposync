import React, { useMemo } from "react";
import type * as ThreeTypes from "three";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  EditorToolContext,
  EditorToolPointerEvent,
  ElementType,
  HostI18n,
  PlanePoint,
  TopoSyncHost,
} from "@toposync/plugin-api";

const WALL_TYPE = "com.toposync.structural.wall";
const AREA_TYPE = "com.toposync.structural.area";

const DEFAULT_WALL_COLOR = "#94a3b8";
const DEFAULT_AREA_COLOR = "#d1d5db";
const DEFAULT_AREA_OPACITY = 0.22;

const GROUND_Y = 0;
const FLOOR_EPSILON = 0.01;

const AREA_FILL_STORAGE_KEY = "toposync.structural.area.fill.v1";

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(translations);

  host.registerElementType(wallElementType(host.i18n));
  host.registerElementType(areaElementType(host.i18n));

  for (const tool of structuralTools(host.i18n)) host.registerEditorTool(tool);
}

const translations = {
  en: {
    "ext.structural.wall.name": "Wall",
    "ext.structural.wall.desc": "Simple wall (line) in 2D.",
    "ext.structural.area.name": "Area",
    "ext.structural.area.desc": "Area (polygon) in 2D.",

    "ext.structural.tools.wall": "Wall",
    "ext.structural.tools.area_square": "Area (square)",
    "ext.structural.tools.area_polygon": "Area (polygon)",
    "ext.structural.tools.area_square_walls": "Area + walls (square)",
    "ext.structural.tools.area_polygon_walls": "Area + walls (polygon)",

    "ext.structural.editor.wall_color": "Wall color",
    "ext.structural.editor.area_name": "Area name (optional)",
    "ext.structural.editor.area_color": "Area color",
    "ext.structural.editor.transparent": "Transparent",
    "ext.structural.editor.preview": "Preview",
  },
  "pt-BR": {
    "ext.structural.wall.name": "Parede",
    "ext.structural.wall.desc": "Parede simples (linha) em 2D.",
    "ext.structural.area.name": "Área",
    "ext.structural.area.desc": "Área (polígono) em 2D.",

    "ext.structural.tools.wall": "Parede",
    "ext.structural.tools.area_square": "Área (quadrado)",
    "ext.structural.tools.area_polygon": "Área (polígono)",
    "ext.structural.tools.area_square_walls": "Área + paredes (quadrado)",
    "ext.structural.tools.area_polygon_walls": "Área + paredes (polígono)",

    "ext.structural.editor.wall_color": "Cor da parede",
    "ext.structural.editor.area_name": "Nome da área (opcional)",
    "ext.structural.editor.area_color": "Cor da área",
    "ext.structural.editor.transparent": "Transparente",
    "ext.structural.editor.preview": "Prévia",
  },
} as const;

function readNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function readString(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}

function isHexColor(value: string): boolean {
  return /^#[0-9a-fA-F]{6}$/.test(value);
}

function loadAreaFill(): string {
  try {
    const stored = localStorage.getItem(AREA_FILL_STORAGE_KEY);
    if (stored && isHexColor(stored)) return stored;
  } catch {
    // ignore
  }
  return DEFAULT_AREA_COLOR;
}

function saveAreaFill(fill: string): void {
  try {
    if (!isHexColor(fill)) return;
    localStorage.setItem(AREA_FILL_STORAGE_KEY, fill);
  } catch {
    // ignore
  }
}

function asPoint(v: unknown, fallback: PlanePoint): PlanePoint {
  if (!v || typeof v !== "object" || Array.isArray(v)) return fallback;
  const rec = v as Record<string, unknown>;
  return { x: readNumber(rec.x, fallback.x), z: readNumber(rec.z, fallback.z) };
}

function asVertices(v: unknown): PlanePoint[] {
  if (!Array.isArray(v)) return [];
  return v
    .map((it) => asPoint(it, { x: 0, z: 0 }))
    .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.z));
}

function readOptionalPoint(v: unknown): PlanePoint | null {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const rec = v as Record<string, unknown>;
  const x = rec.x;
  const z = rec.z;
  if (typeof x !== "number" || typeof z !== "number") return null;
  if (!Number.isFinite(x) || !Number.isFinite(z)) return null;
  return { x, z };
}

function distance(a: PlanePoint, b: PlanePoint): number {
  const dx = a.x - b.x;
  const dz = a.z - b.z;
  return Math.hypot(dx, dz);
}

function add(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x + b.x, z: a.z + b.z };
}

function sub(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x - b.x, z: a.z - b.z };
}

function mul(v: PlanePoint, s: number): PlanePoint {
  return { x: v.x * s, z: v.z * s };
}

function normalize(v: PlanePoint): PlanePoint {
  const len = Math.hypot(v.x, v.z);
  if (len <= 1e-9) return { x: 1, z: 0 };
  return { x: v.x / len, z: v.z / len };
}

function perp(v: PlanePoint): PlanePoint {
  return { x: -v.z, z: v.x };
}

function cross(a: PlanePoint, b: PlanePoint): number {
  return a.x * b.z - a.z * b.x;
}

function lineIntersection(p0: PlanePoint, d0: PlanePoint, p1: PlanePoint, d1: PlanePoint): PlanePoint | null {
  const denom = cross(d0, d1);
  if (Math.abs(denom) < 1e-9) return null;
  const t = cross(sub(p1, p0), d1) / denom;
  return add(p0, mul(d0, t));
}

function computeMiter(
  vertex: PlanePoint,
  dirIn: PlanePoint,
  dirOut: PlanePoint,
  normalSign: number,
  halfThickness: number,
  miterLimit: number,
  fallbackDir: PlanePoint,
): PlanePoint {
  const nIn = mul(perp(dirIn), normalSign * halfThickness);
  const nOut = mul(perp(dirOut), normalSign * halfThickness);
  const pIn = add(vertex, nIn);
  const pOut = add(vertex, nOut);
  const hit = lineIntersection(pIn, dirIn, pOut, dirOut);
  if (!hit) return add(vertex, mul(perp(fallbackDir), normalSign * halfThickness));
  if (distance(hit, vertex) > halfThickness * miterLimit) {
    return add(vertex, mul(perp(fallbackDir), normalSign * halfThickness));
  }
  return hit;
}

function centerOf(points: PlanePoint[]): PlanePoint {
  if (points.length === 0) return { x: 0, z: 0 };
  const sum = points.reduce((acc, p) => ({ x: acc.x + p.x, z: acc.z + p.z }), { x: 0, z: 0 });
  return { x: sum.x / points.length, z: sum.z / points.length };
}

function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const cleaned = hex.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(cleaned)) return null;
  const r = Number.parseInt(cleaned.slice(0, 2), 16);
  const g = Number.parseInt(cleaned.slice(2, 4), 16);
  const b = Number.parseInt(cleaned.slice(4, 6), 16);
  return { r, g, b };
}

function rgba(hex: string, alpha: number): string {
  const rgb = hexToRgb(hex);
  const a = Math.max(0, Math.min(1, alpha));
  if (!rgb) return `rgba(99,102,241,${a})`;
  return `rgba(${rgb.r},${rgb.g},${rgb.b},${a})`;
}

function wallElementType(i18n: HostI18n): ElementType {
  return {
    type: WALL_TYPE,
    layerGroup: "walls",
    name: { key: "ext.structural.wall.name", fallback: "Wall" },
    description: { key: "ext.structural.wall.desc", fallback: "Simple wall (line) in 2D." },
    defaultProps: {
      color: DEFAULT_WALL_COLOR,
      width: 0.12,
      a: { x: 0, z: 0 },
      b: { x: 1, z: 0 },
    },
    create3D: ({ THREE, view }, element) => {
      const group = new THREE.Group();
      const mat = new THREE.MeshStandardMaterial({
        color: DEFAULT_WALL_COLOR,
        roughness: 0.82,
        metalness: 0.05,
        flatShading: true,
      });
      const mesh = new THREE.Mesh(new THREE.BufferGeometry(), mat);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      group.add(mesh);

      let lastKey = "";

      function apply(el: CompositionElement) {
        const a = asPoint(el.props.a, { x: el.position.x - 0.5, z: el.position.z });
        const b = asPoint(el.props.b, { x: el.position.x + 0.5, z: el.position.z });

        const thicknessWorld = Math.max(0.04, readNumber(el.props.width, 0.12));
        const height = Math.max(0.15, view.wallHeight);

        const color = readString(el.props.color, DEFAULT_WALL_COLOR);

        const half = thicknessWorld / 2;
        const u = normalize(sub(b, a));
        const n = perp(u);

        const aPrev = readOptionalPoint((el.props as any).a_prev);
        const bNext = readOptionalPoint((el.props as any).b_next);

        const miterLimit = 6;

        const aBase = aPrev ? a : sub(a, mul(u, half));
        const bBase = bNext ? b : add(b, mul(u, half));

        const startPlus = aPrev
          ? computeMiter(a, normalize(sub(a, aPrev)), u, +1, half, miterLimit, u)
          : add(aBase, mul(n, +half));
        const startMinus = aPrev
          ? computeMiter(a, normalize(sub(a, aPrev)), u, -1, half, miterLimit, u)
          : add(aBase, mul(n, -half));

        const endPlus = bNext
          ? computeMiter(b, u, normalize(sub(bNext, b)), +1, half, miterLimit, u)
          : add(bBase, mul(n, +half));
        const endMinus = bNext
          ? computeMiter(b, u, normalize(sub(bNext, b)), -1, half, miterLimit, u)
          : add(bBase, mul(n, -half));

        const origin = { x: el.position.x, z: el.position.z };
        const fp = [startPlus, endPlus, endMinus, startMinus].map((p) => sub(p, origin));
        const geomKey = JSON.stringify({
          fp: fp.map((p) => ({ x: Math.round(p.x * 1000) / 1000, z: Math.round(p.z * 1000) / 1000 })),
          h: Math.round(height * 1000) / 1000,
        });

        if (geomKey !== lastKey) {
          lastKey = geomKey;
          const y0 = GROUND_Y;
          const y1 = GROUND_Y + height;

          const positions = new Float32Array(8 * 3);
          for (let i = 0; i < 4; i++) {
            const p = fp[i];
            positions[i * 3 + 0] = p.x;
            positions[i * 3 + 1] = y0;
            positions[i * 3 + 2] = p.z;
          }
          for (let i = 0; i < 4; i++) {
            const p = fp[i];
            const base = (4 + i) * 3;
            positions[base + 0] = p.x;
            positions[base + 1] = y1;
            positions[base + 2] = p.z;
          }

          const indices = [
            // top (clockwise in XZ => +Y)
            4, 5, 6, 4, 6, 7,
            // bottom (reverse)
            0, 2, 1, 0, 3, 2,
            // sides
            0, 1, 5, 0, 5, 4,
            1, 2, 6, 1, 6, 5,
            2, 3, 7, 2, 7, 6,
            3, 0, 4, 3, 4, 7,
          ];

          const nextGeom = new THREE.BufferGeometry();
          nextGeom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
          nextGeom.setIndex(indices);
          nextGeom.computeVertexNormals();

          const old = mesh.geometry as ThreeTypes.BufferGeometry;
          mesh.geometry = nextGeom;
          old.dispose();
        }

        mat.color.set(color);
      }

      apply(element);
      return {
        object: group,
        update: apply,
        dispose: () => {
          (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
          mat.dispose();
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const a = asPoint(element.props.a, { x: element.position.x, z: element.position.z });
      const b = asPoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
      const color = readString(element.props.color, DEFAULT_WALL_COLOR);
      const widthWorld = readNumber(element.props.width, 0.12);

      const pa = viewport.worldToScreen(a);
      const pb = viewport.worldToScreen(b);
      const widthPx = Math.max(2, widthWorld * viewport.scale);

      ctx.save();
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = "rgba(0,0,0,0.35)";
      ctx.lineWidth = widthPx + 3;
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      ctx.lineTo(pb.x, pb.y);
      ctx.stroke();

      ctx.strokeStyle = color;
      ctx.lineWidth = widthPx;
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      ctx.lineTo(pb.x, pb.y);
      ctx.stroke();
      ctx.restore();
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <WallEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

function areaElementType(i18n: HostI18n): ElementType {
  return {
    type: AREA_TYPE,
    layerGroup: "areas",
    name: { key: "ext.structural.area.name", fallback: "Area" },
    description: { key: "ext.structural.area.desc", fallback: "Area (polygon) in 2D." },
    defaultProps: {
      fill: DEFAULT_AREA_COLOR,
      opacity: DEFAULT_AREA_OPACITY,
      vertices: [
        { x: -1, z: -1 },
        { x: 1, z: -1 },
        { x: 1, z: 1 },
        { x: -1, z: 1 },
      ],
    },
    create3D: ({ THREE }, element) => {
      const group = new THREE.Group();

      const mat = new THREE.MeshStandardMaterial({
        color: DEFAULT_AREA_COLOR,
        roughness: 0.95,
        metalness: 0.0,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: DEFAULT_AREA_OPACITY,
        polygonOffset: true,
        polygonOffsetFactor: 1,
        polygonOffsetUnits: 1,
      });

      let mesh: ThreeTypes.Mesh | null = null;
      let lastKey = "";

      function buildGeometry(el: CompositionElement): ThreeTypes.BufferGeometry | null {
        const vertices = asVertices(el.props.vertices);
        if (vertices.length < 3) return null;

        const local = vertices.map((p) => ({ x: p.x - el.position.x, z: p.z - el.position.z }));
        const shape = new THREE.Shape();
        shape.moveTo(local[0].x, -local[0].z);
        for (let i = 1; i < local.length; i++) shape.lineTo(local[i].x, -local[i].z);
        shape.closePath();

        const geom = new THREE.ShapeGeometry(shape);
        geom.rotateX(-Math.PI / 2);
        return geom;
      }

      function apply(el: CompositionElement) {
        const vertices = asVertices(el.props.vertices);
        const localKey = JSON.stringify(
          vertices.map((p) => ({
            x: Math.round((p.x - el.position.x) * 1000) / 1000,
            z: Math.round((p.z - el.position.z) * 1000) / 1000,
          })),
        );

        if (localKey !== lastKey) {
          lastKey = localKey;
          const geom = buildGeometry(el);
          if (geom) {
            if (!mesh) {
              mesh = new THREE.Mesh(geom, mat);
              mesh.receiveShadow = true;
              group.add(mesh);
            } else {
              (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
              mesh.geometry = geom;
            }
          } else if (mesh) {
            group.remove(mesh);
            (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
            mesh = null;
          }
        }

        const fill = readString(el.props.fill, DEFAULT_AREA_COLOR);
        const opacity = Math.max(0, Math.min(1, readNumber(el.props.opacity, DEFAULT_AREA_OPACITY)));
        mat.color.set(fill);
        mat.opacity = opacity;
        mat.transparent = opacity < 0.999;
        mat.depthWrite = opacity >= 0.999;

        if (mesh) mesh.position.y = GROUND_Y + FLOOR_EPSILON;
      }

      apply(element);
      return {
        object: group,
        update: apply,
        dispose: () => {
          if (mesh) (mesh.geometry as ThreeTypes.BufferGeometry).dispose();
          mat.dispose();
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const vertices = asVertices(element.props.vertices);
      if (vertices.length < 3) return;

      const fill = readString(element.props.fill, DEFAULT_AREA_COLOR);
      const opacity = readNumber(element.props.opacity, DEFAULT_AREA_OPACITY);

      const pts = vertices.map((p) => viewport.worldToScreen(p));

      ctx.save();
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.closePath();

      ctx.fillStyle = rgba(fill, opacity);
      ctx.fill();

      ctx.strokeStyle = rgba("#e6e8f2", 0.22);
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.restore();
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <AreaEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

type EditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

function WallEditor({ element, update, remove, close, i18n }: EditorProps): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const color = readString(element.props.color, DEFAULT_WALL_COLOR);

  const numberFmt = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  const length = useMemo(() => {
    const a = asPoint(element.props.a, { x: element.position.x, z: element.position.z });
    const b = asPoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
    return distance(a, b);
  }, [element]);

  return (
    <div>
      <div className="card">
        <div className="cardHeaderRow">
          <div className="cardTitle">{t("ext.structural.wall.name")}</div>
          <div className="cardMeta">{numberFmt.format(length)} m</div>
        </div>
        <div className="cardBody">{t("ext.structural.editor.preview")}</div>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.structural.editor.wall_color")}</div>
          <input
            className="input"
            type="color"
            value={color}
            onChange={(e) => update({ props: { color: e.target.value } })}
          />
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap">
        <button className="dangerButton" type="button" onClick={remove}>
          {t("core.actions.delete")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}

function AreaEditor({ element, update, remove, close, i18n }: EditorProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const fill = readString(element.props.fill, DEFAULT_AREA_COLOR);
  const opacity = readNumber(element.props.opacity, DEFAULT_AREA_OPACITY);
  const transparent = opacity < 0.001;

  return (
    <div>
      <div className="field">
        <div className="label">{t("ext.structural.editor.area_name")}</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.structural.editor.area_color")}</div>
          <input
            className="input"
            type="color"
            value={fill}
            onChange={(e) => {
              const next = e.target.value;
              saveAreaFill(next);
              update({ props: { fill: next } });
            }}
          />
        </div>
        <label className="chipButton" style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          <input
            type="checkbox"
            checked={transparent}
            onChange={(e) => update({ props: { opacity: e.target.checked ? 0 : DEFAULT_AREA_OPACITY } })}
          />
          <span>{t("ext.structural.editor.transparent")}</span>
        </label>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap">
        <button className="dangerButton" type="button" onClick={remove}>
          {t("core.actions.delete")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}

function structuralTools(i18n: HostI18n): EditorTool[] {
  return [
    wallTool(i18n),
    areaSquareTool(i18n, { withWalls: false }),
    areaPolygonTool(i18n, { withWalls: false }),
    areaSquareTool(i18n, { withWalls: true }),
    areaPolygonTool(i18n, { withWalls: true }),
  ];
}

function createWall(
  ctx: EditorToolContext,
  a: PlanePoint,
  b: PlanePoint,
  join?: { aPrev?: PlanePoint; bNext?: PlanePoint },
): string | null {
  const c = { x: (a.x + b.x) / 2, z: (a.z + b.z) / 2 };
  const joinProps: Record<string, unknown> = {};
  if (join?.aPrev) joinProps.a_prev = join.aPrev;
  if (join?.bNext) joinProps.b_next = join.bNext;
  return ctx.createElement(WALL_TYPE, {
    name: "",
    position: { x: c.x, y: 0, z: c.z },
    props: { a, b, color: DEFAULT_WALL_COLOR, width: 0.12, ...joinProps },
  });
}

function createArea(ctx: EditorToolContext, vertices: PlanePoint[]): string | null {
  const c = centerOf(vertices);
  const fill = loadAreaFill();
  return ctx.createElement(AREA_TYPE, {
    name: "",
    position: { x: c.x, y: 0, z: c.z },
    props: { vertices, fill, opacity: DEFAULT_AREA_OPACITY },
  });
}

function edges(vertices: PlanePoint[]): Array<{ a: PlanePoint; b: PlanePoint }> {
  if (vertices.length < 2) return [];
  const out: Array<{ a: PlanePoint; b: PlanePoint }> = [];
  for (let i = 0; i < vertices.length; i++) {
    const a = vertices[i];
    const b = vertices[(i + 1) % vertices.length];
    out.push({ a, b });
  }
  return out;
}

function createWallsForPolygon(ctx: EditorToolContext, vertices: PlanePoint[]): void {
  const n = vertices.length;
  if (n < 2) return;
  for (let i = 0; i < n; i++) {
    const a = vertices[i];
    const b = vertices[(i + 1) % n];
    const aPrev = vertices[(i - 1 + n) % n];
    const bNext = vertices[(i + 2) % n];
    createWall(ctx, a, b, { aPrev, bNext });
  }
}

function wallTool(i18n: HostI18n): EditorTool {
  return {
    id: "com.toposync.structural.tool.wall",
    name: { key: "ext.structural.tools.wall", fallback: "Wall" },
    icon: "ruler-combined",
    createSession: (ctx) => {
      let start: PlanePoint | null = null;
      let current: PlanePoint | null = null;

      function reset() {
        start = null;
        current = null;
      }

      function commit(end: PlanePoint) {
        if (!start) return;
        if (distance(start, end) < 0.05) {
          reset();
          return;
        }
        createWall(ctx, start, end);
        reset();
      }

      return {
        onPointerEvent: (evt) => {
          if (evt.kind === "cancel") {
            reset();
            return;
          }
          if (evt.kind === "move") {
            if (start) current = evt.world;
            return;
          }
          if (evt.kind !== "down") return;
          if (evt.button !== 0) return;

          if (!start) {
            start = evt.world;
            current = evt.world;
            return;
          }
          commit(evt.world);
        },
        onKeyDown: (e) => {
          if (e.key === "Escape") reset();
        },
        renderOverlay2D: ({ ctx: canvas, viewport }) => {
          if (!start || !current) return;
          const a = viewport.worldToScreen(start);
          const b = viewport.worldToScreen(current);
          const width = Math.max(2, 0.12 * viewport.scale);

          canvas.save();
          canvas.setLineDash([8, 6]);
          canvas.lineCap = "round";
          canvas.strokeStyle = rgba("#fbbf24", 0.85);
          canvas.lineWidth = width;
          canvas.beginPath();
          canvas.moveTo(a.x, a.y);
          canvas.lineTo(b.x, b.y);
          canvas.stroke();
          canvas.restore();
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function areaSquareTool(i18n: HostI18n, opts: { withWalls: boolean }): EditorTool {
  return {
    id: opts.withWalls ? "com.toposync.structural.tool.area_square_walls" : "com.toposync.structural.tool.area_square",
    name: {
      key: opts.withWalls ? "ext.structural.tools.area_square_walls" : "ext.structural.tools.area_square",
      fallback: opts.withWalls ? "Area + walls (square)" : "Area (square)",
    },
    icon: opts.withWalls ? "draw-polygon" : "square",
    createSession: (ctx) => {
      let start: PlanePoint | null = null;
      let current: PlanePoint | null = null;

      function reset() {
        start = null;
        current = null;
      }

      function rectVertices(a: PlanePoint, b: PlanePoint): PlanePoint[] {
        const x0 = Math.min(a.x, b.x);
        const x1 = Math.max(a.x, b.x);
        const z0 = Math.min(a.z, b.z);
        const z1 = Math.max(a.z, b.z);
        return [
          { x: x0, z: z0 },
          { x: x1, z: z0 },
          { x: x1, z: z1 },
          { x: x0, z: z1 },
        ];
      }

      function commit(end: PlanePoint) {
        if (!start) return;
        const verts = rectVertices(start, end);
        if (distance(verts[0], verts[2]) < 0.12) {
          reset();
          return;
        }
        const areaId = createArea(ctx, verts);
        if (opts.withWalls) {
          createWallsForPolygon(ctx, verts);
        }
        if (areaId) ctx.openEditor(areaId);
        reset();
      }

      return {
        onPointerEvent: (evt) => {
          if (evt.kind === "cancel") {
            reset();
            return;
          }
          if (evt.kind === "move") {
            if (start) current = evt.world;
            return;
          }
          if (evt.kind !== "down") return;
          if (evt.button !== 0) return;

          if (!start) {
            start = evt.world;
            current = evt.world;
            return;
          }
          commit(evt.world);
        },
        onKeyDown: (e) => {
          if (e.key === "Escape") reset();
        },
        renderOverlay2D: ({ ctx: canvas, viewport }) => {
          if (!start || !current) return;
          const verts = [
            start,
            { x: current.x, z: start.z },
            current,
            { x: start.x, z: current.z },
          ];
          const pts = verts.map((p) => viewport.worldToScreen(p));

          canvas.save();
          canvas.beginPath();
          canvas.moveTo(pts[0].x, pts[0].y);
          for (let i = 1; i < pts.length; i++) canvas.lineTo(pts[i].x, pts[i].y);
          canvas.closePath();
          canvas.fillStyle = rgba("#fbbf24", 0.12);
          canvas.fill();
          canvas.strokeStyle = rgba("#fbbf24", 0.8);
          canvas.lineWidth = 2;
          canvas.setLineDash([6, 6]);
          canvas.stroke();
          canvas.restore();
        },
        getCursor: () => "crosshair",
      };
    },
  };
}

function areaPolygonTool(i18n: HostI18n, opts: { withWalls: boolean }): EditorTool {
  return {
    id: opts.withWalls ? "com.toposync.structural.tool.area_polygon_walls" : "com.toposync.structural.tool.area_polygon",
    name: {
      key: opts.withWalls ? "ext.structural.tools.area_polygon_walls" : "ext.structural.tools.area_polygon",
      fallback: opts.withWalls ? "Area + walls (polygon)" : "Area (polygon)",
    },
    icon: "draw-polygon",
    createSession: (ctx) => {
      const vertices: PlanePoint[] = [];
      let hover: PlanePoint | null = null;

      function reset() {
        vertices.splice(0, vertices.length);
        hover = null;
      }

      function commit() {
        if (vertices.length < 3) {
          reset();
          return;
        }
        const areaId = createArea(ctx, [...vertices]);
        if (opts.withWalls) {
          createWallsForPolygon(ctx, vertices);
        }
        if (areaId) ctx.openEditor(areaId);
        reset();
      }

      function shouldCloseByClick(evt: EditorToolPointerEvent): boolean {
        if (vertices.length < 3) return false;
        const first = vertices[0];
        return distance(evt.world, first) < 0.22;
      }

      return {
        onPointerEvent: (evt) => {
          if (evt.kind === "cancel") {
            reset();
            return;
          }
          if (evt.kind === "move") {
            hover = evt.world;
            return;
          }
          if (evt.kind === "dblclick") {
            commit();
            return;
          }
          if (evt.kind !== "down") return;
          if (evt.button !== 0) return;

          if (shouldCloseByClick(evt)) {
            commit();
            return;
          }
          vertices.push(evt.world);
          hover = evt.world;
        },
        onKeyDown: (e) => {
          if (e.key === "Escape") reset();
          if (e.key === "Enter") commit();
          if ((e.key === "Backspace" || e.key === "Delete") && vertices.length > 0) vertices.pop();
        },
        renderOverlay2D: ({ ctx: canvas, viewport }) => {
          if (vertices.length === 0) return;
          const pts = vertices.map((p) => viewport.worldToScreen(p));
          const preview = hover ? viewport.worldToScreen(hover) : null;

          canvas.save();

          if (vertices.length >= 2) {
            canvas.beginPath();
            canvas.moveTo(pts[0].x, pts[0].y);
            for (let i = 1; i < pts.length; i++) canvas.lineTo(pts[i].x, pts[i].y);
            if (preview) canvas.lineTo(preview.x, preview.y);
            canvas.strokeStyle = rgba("#fbbf24", 0.85);
            canvas.lineWidth = 2;
            canvas.setLineDash([6, 6]);
            canvas.stroke();
          }

          // Vertices
          for (let i = 0; i < pts.length; i++) {
            const p = pts[i];
            const isFirst = i === 0 && vertices.length >= 3;
            canvas.beginPath();
            canvas.arc(p.x, p.y, isFirst ? 6 : 5, 0, Math.PI * 2);
            canvas.fillStyle = isFirst ? rgba("#22c55e", 0.85) : rgba("#fbbf24", 0.85);
            canvas.fill();
            canvas.strokeStyle = "rgba(0,0,0,0.35)";
            canvas.lineWidth = 2;
            canvas.stroke();
          }

          canvas.restore();
        },
        getCursor: () => "crosshair",
      };
    },
  };
}
