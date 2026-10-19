import type { InspectionNode, InspectionSnapshot, InspectionVisual } from "@toposync/plugin-api";

export type FrameReference = {
  source_id: string; device_id: string | null; capture_instance: string; generation: number; sequence: number;
};
export type PixelGeometry = {
  image_size: [number, number]; source_size: [number, number]; to_source: number[][];
  valid_domains: Array<{ size: [number, number]; matrix: number[][] }>;
};
export type PairedInspectionMetadata = {
  schema_version: 2; transport: "paired_jpeg"; expires_at: number; jpeg_length: number;
  masks: Array<{ node_id: string; image_sequence: number; index: number; width: number; height: number; length: number }>;
  presentation: { frame: FrameReference; image_size: [number, number]; geometry: PixelGeometry | null };
  snapshot: InspectionSnapshot;
};
export type InspectionBox = { points: number[][]; label: string; score: number; node: InspectionNode };
export type ProjectedInspectionVisual = Omit<InspectionVisual, "points"> & { points: Array<number[] | null>; node: InspectionNode };
export const maximumPairedBytes = 400_000;

function finite(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function size(value: unknown): value is [number, number] {
  return Array.isArray(value) && value.length === 2 && value.every(n => Number.isInteger(n) && n > 0 && n <= 8192);
}
function matrix(value: unknown): value is number[][] {
  return Array.isArray(value) && value.length === 3 && value.every(row => Array.isArray(row) && row.length === 3 && row.every(finite));
}
function geometry(value: unknown): value is PixelGeometry {
  const g = value as PixelGeometry | null;
  return Boolean(g && size(g.image_size) && size(g.source_size) && matrix(g.to_source) && Array.isArray(g.valid_domains)
    && g.valid_domains.length <= 64 && g.valid_domains.every(domain => size(domain.size) && matrix(domain.matrix)));
}
function reference(value: unknown): value is FrameReference {
  const r = value as FrameReference | null;
  return Boolean(r && typeof r.source_id === "string" && r.source_id.length <= 128
    && (r.device_id === null || typeof r.device_id === "string" && r.device_id.length <= 128)
    && typeof r.capture_instance === "string" && r.capture_instance.length <= 128
    && Number.isSafeInteger(r.generation) && r.generation >= 0 && Number.isSafeInteger(r.sequence) && r.sequence > 0);
}

export async function readPairedResponse(response: Response): Promise<Uint8Array> {
  if (!response.body || Number(response.headers.get("content-length")) > maximumPairedBytes)
    throw new Error("inspection_frame_capacity");
  const reader = response.body.getReader(), chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > maximumPairedBytes) throw new Error("inspection_frame_capacity");
      chunks.push(value);
    }
  } finally { await reader.cancel(); reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return bytes;
}

export function decodePairedFrame(bytes: Uint8Array): { metadata: PairedInspectionMetadata; jpeg: Uint8Array; masks: Uint8Array[] } {
  if (bytes.length < 8 || bytes.length > maximumPairedBytes) throw new Error("inspection_frame_capacity");
  const length = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0);
  if (length < 2 || length > 262144 || length + 4 >= bytes.length) throw new Error("inspection_frame_invalid");
  const metadata = JSON.parse(new TextDecoder().decode(bytes.subarray(4, length + 4))) as PairedInspectionMetadata;
  const p = metadata.presentation, s = metadata.snapshot;
  if (metadata.schema_version !== 2 || metadata.transport !== "paired_jpeg" || !finite(metadata.expires_at)
      || !p || !reference(p.frame) || !size(p.image_size) || p.image_size[0] * p.image_size[1] * 4 > 16_777_216
      || p.geometry !== null && !geometry(p.geometry)
      || !s || typeof s.session !== "string" || !Array.isArray(s.nodes) || s.nodes.length > 128
      || !Number.isSafeInteger(s.sequence) || s.sequence < 0
      || !Number.isSafeInteger(metadata.jpeg_length) || metadata.jpeg_length < 4
      || !Array.isArray(metadata.masks) || metadata.masks.length > 4
      || metadata.masks.some(mask => !mask || typeof mask.node_id !== "string" || mask.node_id.length > 128
        || !Number.isSafeInteger(mask.image_sequence) || mask.image_sequence < 0
        || !Number.isInteger(mask.index) || mask.index < 0 || mask.index > 3
        || !Number.isInteger(mask.width) || mask.width < 1 || mask.width > 160
        || !Number.isInteger(mask.height) || mask.height < 1 || mask.height > 90
        || !Number.isSafeInteger(mask.length) || mask.length < 8))
    throw new Error("inspection_frame_invalid");
  if (length + 4 + metadata.jpeg_length + metadata.masks.reduce((sum, mask) => sum + mask.length, 0) !== bytes.length)
    throw new Error("inspection_frame_invalid");
  let offset = length + 4 + metadata.jpeg_length;
  const masks = metadata.masks.map(mask => { const bytesForMask = bytes.subarray(offset, offset + mask.length); offset += mask.length; return bytesForMask; });
  return { metadata, jpeg: bytes.subarray(length + 4, length + 4 + metadata.jpeg_length), masks };
}

export function sameCaptureFrame(a: unknown, b: unknown): boolean {
  if (!reference(a) || !reference(b)) return false;
  return a.source_id === b.source_id && a.device_id === b.device_id && a.capture_instance === b.capture_instance
    && a.generation === b.generation && a.sequence === b.sequence;
}

function transform(point: number[], m: number[][]): number[] | null {
  const [x, y] = point, divisor = m[2][0] * x + m[2][1] * y + m[2][2];
  if (Math.abs(divisor) < 1e-8) return null;
  const out = [(m[0][0] * x + m[0][1] * y + m[0][2]) / divisor, (m[1][0] * x + m[1][1] * y + m[1][2]) / divisor];
  return out.every(finite) ? out : null;
}
function inverse(m: number[][]): number[][] | null {
  const [[a,b,c],[d,e,f],[g,h,i]] = m;
  const determinant = a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g);
  if (Math.abs(determinant) < 1e-12) return null;
  return [[e*i-f*h,c*h-b*i,b*f-c*e],[f*g-d*i,a*i-c*g,c*d-a*f],[d*h-e*g,b*g-a*h,a*e-b*d]]
    .map(row => row.map(n => n/determinant));
}
function supported(point: number[], g: PixelGeometry): boolean {
  return point[0] >= 0 && point[1] >= 0 && point[0] <= g.image_size[0] && point[1] <= g.image_size[1]
    && g.valid_domains.every(domain => {
      const p = transform(point, domain.matrix);
      return p !== null && p[0] >= 0 && p[1] >= 0 && p[0] <= domain.size[0] && p[1] <= domain.size[1];
    });
}

function* inspectionProjections(metadata: PairedInspectionMetadata, now: number, selectedNodes?: ReadonlySet<string>) {
  const target = metadata.presentation.geometry;
  if (now >= metadata.expires_at || !geometry(target)
      || target.image_size.some((n, index) => n !== metadata.presentation.image_size[index])) return;
  const backwards = inverse(target.to_source);
  if (!backwards) return;
  const seen = new Set<string>();
  for (const node of metadata.snapshot.nodes) {
    if (selectedNodes && !selectedNodes.has(node.node_id)) continue;
    const origin = node.geometry;
    if (!sameCaptureFrame(node.frame, metadata.presentation.frame) || node.expires_at === null || now >= node.expires_at
        || !geometry(origin) || origin.source_size.some((n, index) => n !== target.source_size[index])) continue;
    const key = JSON.stringify([node.physical_node_id, node.producer_epoch, node.model_id, node.frame]);
    if (seen.has(key)) continue;
    seen.add(key);
    yield { node, origin, target, backwards, project: (point: number[]): number[] | null => {
      if (point.length !== 2 || !point.every(n => finite(n) && n >= 0 && n <= 1)) return null;
      const local = [point[0] * origin.image_size[0], point[1] * origin.image_size[1]];
      if (!supported(local, origin)) return null;
      const source = transform(local, origin.to_source);
      const presented = source && transform(source, backwards);
      return presented && supported(presented, target) ? presented : null;
    } };
  }
}

export function projectInspectionBoxes(metadata: PairedInspectionMetadata, now: number, selectedNodes?: ReadonlySet<string>): InspectionBox[] {
  const result: InspectionBox[] = [];
  for (const { node, project } of inspectionProjections(metadata, now, selectedNodes)) {
    for (const detection of node.detections.slice(0, 32)) {
      const box = detection.box;
      if (!Array.isArray(box) || box.length !== 4 || !box.every(n => finite(n) && n >= 0 && n <= 1)
          || box[0] > box[2] || box[1] > box[3] || !finite(detection.score)) continue;
      const corners = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]];
      const points = corners.map(project);
      if (points.every((point): point is number[] => point !== null))
        result.push({ points, label: detection.label, score: detection.score, node });
    }
  }
  return result;
}

export function projectInspectionVisuals(metadata: PairedInspectionMetadata, now: number, layers: ReadonlySet<string>, selectedNodes?: ReadonlySet<string>): ProjectedInspectionVisual[] {
  const result: ProjectedInspectionVisual[] = [];
  for (const { node, project } of inspectionProjections(metadata, now, selectedNodes)) {
    for (const visual of (node.visuals || []).slice(0, 32)) {
      if (!layers.has(visual.layer) || !["points", "polygon", "polyline"].includes(visual.kind)
          || !Array.isArray(visual.points) || visual.points.length > 128
          || !Array.isArray(visual.connections) || visual.connections.length > 128) continue;
      const points = visual.points.map(point => Array.isArray(point) ? project(point) : null);
      // Missing joints retain their slots; never connect different joints across an absent one.
      if (visual.kind !== "points" && points.some(point => point === null)) continue;
      const connections = visual.connections.filter(edge => Array.isArray(edge) && edge.length === 2
        && edge.every(index => Number.isSafeInteger(index) && index >= 0 && index < points.length && points[index] !== null));
      result.push({ ...visual, points, connections, node });
    }
  }
  return result;
}

function multiply(a: number[][], b: number[][]): number[][] {
  return a.map(row => [0,1,2].map(column => row.reduce((sum, value, index) => sum + value * b[index][column], 0)));
}

export function projectInspectionMasks(metadata: PairedInspectionMetadata, now: number, selectedNodes?: ReadonlySet<string>) {
  const result: Array<{ attachment: number; matrix: [number, number, number, number, number, number]; clips: number[][][] }> = [];
  for (const { node, origin, target, backwards } of inspectionProjections(metadata, now, selectedNodes)) {
    const combined = multiply(backwards, origin.to_source);
    // shortcut: Canvas supports affine masks; add a projective renderer before accepting other warps.
    if (Math.abs(combined[2][0]) > 1e-12 || Math.abs(combined[2][1]) > 1e-12 || Math.abs(combined[2][2]) < 1e-12) continue;
    const [[a,c,e],[b,d,f]] = combined.map(row => row.map(n => n / combined[2][2]));
    const clipDomains = [
      { size: origin.image_size, matrix: combined },
      ...origin.valid_domains.map(domain => {
        const reversed = inverse(domain.matrix);
        return { size: domain.size, matrix: reversed && multiply(combined, reversed) };
      }),
      ...target.valid_domains.map(domain => ({ size: domain.size, matrix: inverse(domain.matrix) })),
    ];
    const clips: number[][][] = [];
    for (const domain of clipDomains) {
      if (!domain.matrix) break;
      const corners = [[-0.5,-0.5],[domain.size[0]-0.5,-0.5],[domain.size[0]-0.5,domain.size[1]-0.5],[-0.5,domain.size[1]-0.5]];
      const divisors = corners.map(([x,y]) => domain.matrix![2][0]*x+domain.matrix![2][1]*y+domain.matrix![2][2]);
      if (!divisors.every(value => value > 1e-8) && !divisors.every(value => value < -1e-8)) break;
      const points = corners.map(point => transform(point, domain.matrix!));
      if (!points.every((point): point is number[] => point !== null)) break;
      clips.push(points.map(([x,y]) => [x+0.5,y+0.5]));
    }
    if (clips.length !== clipDomains.length) continue;
    for (const visual of node.visuals) {
      const raster = visual.raster;
      if (visual.kind !== "raster" || !raster || raster.sampling !== "nearest_pixel_centres") continue;
      const attachment = metadata.masks.findIndex(mask => {
        const producer = metadata.snapshot.nodes.find(value => value.node_id === mask.node_id);
        return producer && producer.image_sequence === mask.image_sequence && sameCaptureFrame(producer.frame, node.frame)
          && producer.physical_node_id === node.physical_node_id && producer.producer_epoch === node.producer_epoch
          && producer.model_id === node.model_id && mask.index === raster.index && mask.width === raster.width && mask.height === raster.height;
      });
      if (attachment < 0) continue;
      const sx = origin.image_size[0] / raster.width, sy = origin.image_size[1] / raster.height;
      result.push({ attachment, clips, matrix: [a*sx,b*sx,c*sy,d*sy,e-(a+c)/2+0.5,f-(b+d)/2+0.5] });
    }
  }
  return result;
}
