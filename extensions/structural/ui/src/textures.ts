type Three = typeof import("three");
type Texture = import("three").Texture;

export type WallTextureId = "none" | "brick" | "concrete";
export type FloorTextureId = "none" | "grass" | "concrete";
export type TextureQuality = "simplified" | "detailed";

function createCanvas(size: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  return canvas;
}

function randomInt(minInclusive: number, maxInclusive: number): number {
  return minInclusive + Math.floor(Math.random() * (maxInclusive - minInclusive + 1));
}

function drawNoise(ctx: CanvasRenderingContext2D, size: number, count: number, alpha: number): void {
  ctx.save();
  ctx.globalAlpha = alpha;
  for (let i = 0; i < count; i++) {
    const x = Math.random() * size;
    const y = Math.random() * size;
    const w = 1 + Math.random() * 2;
    const h = 1 + Math.random() * 2;
    const g = randomInt(0, 255);
    ctx.fillStyle = `rgb(${g},${g},${g})`;
    ctx.fillRect(x, y, w, h);
  }
  ctx.restore();
}

function createBrickTextureCanvas(size: number): HTMLCanvasElement {
  const canvas = createCanvas(size);
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;

  ctx.fillStyle = "#6b7280";
  ctx.fillRect(0, 0, size, size);

  const mortar = "#111827";
  const brickRows = 10;
  const brickHeight = size / brickRows;
  const brickWidth = brickHeight * 2.2;
  const gap = Math.max(1, Math.round(size * 0.01));

  for (let row = 0; row < brickRows; row++) {
    const y0 = row * brickHeight;
    const offset = row % 2 === 0 ? 0 : brickWidth / 2;
    for (let x = -offset; x < size + brickWidth; x += brickWidth) {
      const x0 = Math.floor(x + gap);
      const y1 = Math.floor(y0 + gap);
      const w = Math.ceil(brickWidth - gap * 2);
      const h = Math.ceil(brickHeight - gap * 2);
      const g = randomInt(86, 140);
      ctx.fillStyle = `rgb(${g},${g},${g})`;
      ctx.fillRect(x0, y1, w, h);
    }
  }

  ctx.strokeStyle = mortar;
  ctx.lineWidth = gap;
  ctx.globalAlpha = 0.85;

  for (let row = 0; row <= brickRows; row++) {
    const y = row * brickHeight;
    ctx.beginPath();
    ctx.moveTo(0, y + 0.5);
    ctx.lineTo(size, y + 0.5);
    ctx.stroke();
  }

  for (let row = 0; row < brickRows; row++) {
    const y = row * brickHeight;
    const offset = row % 2 === 0 ? 0 : brickWidth / 2;
    for (let x = -offset; x < size + brickWidth; x += brickWidth) {
      const xx = x;
      ctx.beginPath();
      ctx.moveTo(xx + 0.5, y);
      ctx.lineTo(xx + 0.5, y + brickHeight);
      ctx.stroke();
    }
  }

  drawNoise(ctx, size, Math.floor((size * size) / 10), 0.06);
  return canvas;
}

function createConcreteTextureCanvas(size: number): HTMLCanvasElement {
  const canvas = createCanvas(size);
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;

  ctx.fillStyle = "#9ca3af";
  ctx.fillRect(0, 0, size, size);
  drawNoise(ctx, size, Math.floor((size * size) / 2), 0.08);
  drawNoise(ctx, size, Math.floor((size * size) / 3), 0.06);
  return canvas;
}

function createGrassTextureCanvas(size: number): HTMLCanvasElement {
  const canvas = createCanvas(size);
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;

  ctx.fillStyle = "#166534";
  ctx.fillRect(0, 0, size, size);
  drawNoise(ctx, size, Math.floor((size * size) / 2), 0.10);

  ctx.save();
  ctx.globalAlpha = 0.20;
  ctx.strokeStyle = "#052e16";
  ctx.lineWidth = 1;
  for (let i = 0; i < 360; i++) {
    const x = Math.random() * size;
    const y = Math.random() * size;
    const len = 4 + Math.random() * 10;
    const angle = (-Math.PI / 2) + (Math.random() - 0.5) * 0.8;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + Math.cos(angle) * len, y + Math.sin(angle) * len);
    ctx.stroke();
  }
  ctx.globalAlpha = 0.14;
  ctx.strokeStyle = "#4ade80";
  for (let i = 0; i < 180; i++) {
    const x = Math.random() * size;
    const y = Math.random() * size;
    const len = 3 + Math.random() * 8;
    const angle = (-Math.PI / 2) + (Math.random() - 0.5) * 0.9;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + Math.cos(angle) * len, y + Math.sin(angle) * len);
    ctx.stroke();
  }
  ctx.restore();

  return canvas;
}

function createGrassTextureCanvasDetailed(size: number): HTMLCanvasElement {
  const canvas = createCanvas(size);
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;

  ctx.fillStyle = "#14532d";
  ctx.fillRect(0, 0, size, size);
  drawNoise(ctx, size, Math.floor((size * size) / 2), 0.08);
  drawNoise(ctx, size, Math.floor((size * size) / 3), 0.06);

  ctx.save();
  ctx.globalAlpha = 0.18;
  ctx.strokeStyle = "#052e16";
  ctx.lineWidth = 1;
  for (let i = 0; i < 900; i++) {
    const x = Math.random() * size;
    const y = Math.random() * size;
    const len = 4 + Math.random() * 12;
    const angle = (-Math.PI / 2) + (Math.random() - 0.5) * 0.9;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + Math.cos(angle) * len, y + Math.sin(angle) * len);
    ctx.stroke();
  }
  ctx.globalAlpha = 0.12;
  ctx.strokeStyle = "#86efac";
  for (let i = 0; i < 520; i++) {
    const x = Math.random() * size;
    const y = Math.random() * size;
    const len = 3 + Math.random() * 10;
    const angle = (-Math.PI / 2) + (Math.random() - 0.5) * 1.0;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + Math.cos(angle) * len, y + Math.sin(angle) * len);
    ctx.stroke();
  }
  ctx.restore();

  return canvas;
}

const cache = new Map<string, Texture>();

function getCachedTexture(
  THREE: Three,
  id: string,
  createCanvasFn: (size: number) => HTMLCanvasElement,
  opts?: { size?: number; anisotropy?: number },
): Texture {
  const cached = cache.get(id);
  if (cached) return cached;

  const size = opts?.size ?? 256;
  const canvas = createCanvasFn(size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = opts?.anisotropy ?? 4;
  cache.set(id, tex);
  return tex;
}

export function readWallTextureId(value: unknown, fallback: WallTextureId): WallTextureId {
  return value === "none" || value === "brick" || value === "concrete" ? value : fallback;
}

export function readFloorTextureId(value: unknown, fallback: FloorTextureId): FloorTextureId {
  return value === "none" || value === "grass" || value === "concrete" ? value : fallback;
}

export function getWallTexture(THREE: Three, id: WallTextureId): Texture | null {
  if (id === "none") return null;
  if (id === "brick") return getCachedTexture(THREE, "wall:brick", createBrickTextureCanvas, { size: 256, anisotropy: 4 });
  if (id === "concrete") return getCachedTexture(THREE, "wall:concrete", createConcreteTextureCanvas, { size: 256, anisotropy: 4 });
  return null;
}

export function getFloorTexture(THREE: Three, id: FloorTextureId, quality: TextureQuality = "simplified"): Texture | null {
  if (id === "none") return null;
  if (id === "grass") {
    if (quality === "detailed") return getCachedTexture(THREE, "floor:grass:detailed", createGrassTextureCanvasDetailed, { size: 512, anisotropy: 8 });
    return getCachedTexture(THREE, "floor:grass:simplified", createGrassTextureCanvas, { size: 256, anisotropy: 4 });
  }
  if (id === "concrete") return getCachedTexture(THREE, "floor:concrete", createConcreteTextureCanvas, { size: 256, anisotropy: 4 });
  return null;
}
