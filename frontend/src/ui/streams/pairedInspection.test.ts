declare const require: any;
import { decodePairedFrame, expirePrivateVisuals, projectInspectionBoxes, projectInspectionMasks, projectInspectionVisuals, readPairedResponse, type PairedInspectionMetadata, type PixelGeometry } from "./pairedInspection";
const test: (name: string, fn: () => void | Promise<void>) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

function fixture(): PairedInspectionMetadata {
  const frame = { source_id: "main", device_id: "camera", capture_instance: "capture", generation: 1, sequence: 7 };
  const geometry: PixelGeometry = { image_size: [100, 100], source_size: [100, 100], to_source: [[1,0,0],[0,1,0],[0,0,1]], valid_domains: [] };
  return { schema_version: 2, transport: "paired_jpeg", expires_at: 101, jpeg_length: 4, masks: [],
    presentation: { frame, image_size: [100,100], geometry },
    snapshot: { session: "session", sequence: 1, pipeline: "test", revision: "revision", dropped: 0,
      alignment: "analyzed_frame_only", data_class: "diagnostic_private", access_scope: "owner", policy_revision: null,
      nodes: [{ node_id: "detect", physical_node_id: "shared", producer_epoch: "runtime",
      operator: "vision.detect", state: "analyzed", reason: null, sequence: 1, motion: null, image_available: true,
      image_sequence: 1, received_count: 1, produced_count: 1, filtered_count: 0,
      frame: { ...frame }, geometry: structuredClone(geometry), model_id: "detector", expires_at: 101,
      visuals: [], detections: [{ box: [0.2,0.2,0.6,0.6], score: 0.9, label: "square", score_kind: "detector_score" }] }] } };
}

test("paired frame identity rejects older results, source switches and reconnections", () => {
  for (const key of ["source_id", "device_id", "capture_instance", "generation", "sequence"]) {
    const value = fixture();
    value.snapshot.nodes[0].frame![key] = typeof value.snapshot.nodes[0].frame![key] === "number" ? 2 : "other";
    assert.equal(projectInspectionBoxes(value, 100).length, 0, key);
  }
  assert.equal(projectInspectionBoxes(fixture(), 101).length, 0);
  const expired = fixture(); expired.snapshot.nodes[0].expires_at = 99;
  assert.equal(projectInspectionBoxes(expired, 100).length, 0);
});

test("physical sharing deduplicates geometry but different models remain separate", () => {
  const value = fixture();
  value.snapshot.nodes.push({ ...value.snapshot.nodes[0], node_id: "consumer" });
  assert.equal(projectInspectionBoxes(value, 100).length, 1);
  value.snapshot.nodes.push({ ...value.snapshot.nodes[0], model_id: "other-model" });
  assert.equal(projectInspectionBoxes(value, 100).length, 2);
});

test("known crop resize and rotation project onto published pixels", () => {
  const value = fixture();
  value.snapshot.nodes[0].geometry!.to_source = [[0, -0.5, 80], [0.5, 0, 10], [0, 0, 1]];
  const box = projectInspectionBoxes(value, 100)[0];
  assert.deepEqual(box.points, [[70,20],[70,40],[50,40],[50,20]]);
  value.presentation.geometry!.to_source = [[2,0,0],[0,2,0],[0,0,1]];
  assert.deepEqual(projectInspectionBoxes(value, 100)[0].points, [[35,10],[35,20],[25,20],[25,10]]);
});

test("missing lens transform, unsupported domain and singular matrix never invent geometry", () => {
  let value = fixture(); value.presentation.geometry = null;
  assert.equal(projectInspectionBoxes(value, 100).length, 0);
  value = fixture(); value.presentation.geometry!.to_source = [[0,0,0],[0,0,0],[0,0,0]];
  assert.equal(projectInspectionBoxes(value, 100).length, 0);
  value = fixture(); value.presentation.geometry!.valid_domains = [{ size: [10,10], matrix: [[1,0,0],[0,1,0],[0,0,1]] }];
  assert.equal(projectInspectionBoxes(value, 100).length, 0);
  value = fixture(); value.snapshot.nodes[0].geometry!.source_size = [99,100];
  assert.equal(projectInspectionBoxes(value, 100).length, 0);
});

function encode(metadata: PairedInspectionMetadata, attachments: number[] = []): Uint8Array {
  const json = new TextEncoder().encode(JSON.stringify(metadata));
  const bytes = new Uint8Array(json.length + 8 + attachments.length);
  new DataView(bytes.buffer).setUint32(0, json.length);
  bytes.set(json, 4); bytes.set([255,216,255,217], json.length + 4);
  bytes.set(attachments, json.length + 8);
  return bytes;
}
test("binary envelope keeps metadata and jpeg paired and rejects malformed dimensions", () => {
  const value = fixture();
  assert.deepEqual(decodePairedFrame(encode(value)).metadata, value);
  assert.deepEqual([...decodePairedFrame(encode(value)).jpeg], [255,216,255,217]);
  value.presentation.image_size = [8192,8192];
  assert.throws(() => decodePairedFrame(encode(value)), /invalid/);
  assert.throws(() => decodePairedFrame(new Uint8Array(400001)), /capacity/);
  assert.throws(() => decodePairedFrame(new Uint8Array(8)), /invalid/);
});

function maskedFixture(): PairedInspectionMetadata {
  const value = fixture();
  value.snapshot.nodes[0].visuals = [{ layer: "masks", kind: "raster", points: [], connections: [],
    label: "square", score: 0.9, score_kind: "segmentation_model_score", subject_id: null,
    local_id: null, segment_id: null, state: "sampled_mask", sample_sequences: [], access_scope: "diagnostic_private", properties: {},
    raster: { index: 0, width: 25, height: 25, sampling: "nearest_pixel_centres" } }];
  value.masks = [{ node_id: "detect", image_sequence: 1, index: 0, width: 25, height: 25, length: 8 }];
  return value;
}

test("binary mask attachments have explicit lengths and bounded dimensions", () => {
  const value = maskedFixture(), pixels = [1,2,3,4,5,6,7,8];
  const decoded = decodePairedFrame(encode(value, pixels));
  assert.deepEqual([...decoded.jpeg], [255,216,255,217]);
  assert.deepEqual([...decoded.masks[0]], pixels);
  assert.throws(() => decodePairedFrame(encode(value, pixels.slice(1))), /invalid/);
  value.masks[0].width = 161;
  assert.throws(() => decodePairedFrame(encode(value, pixels)), /invalid/);
  value.masks[0].width = 25; value.masks[0].length = -1;
  assert.throws(() => decodePairedFrame(encode(value, pixels)), /invalid/);
});

test("mask pixel edges preserve resize rotation and clipping, without cross-frame reuse", () => {
  const value = maskedFixture();
  assert.deepEqual(projectInspectionMasks(value, 100)[0].matrix, [4,0,0,4,0,0]);
  assert.deepEqual(projectInspectionMasks(value, 100)[0].clips, [[[0,0],[100,0],[100,100],[0,100]]]);
  value.snapshot.nodes[0].geometry!.to_source = [[0,-0.5,80],[0.5,0,10],[0,0,1]];
  assert.deepEqual(projectInspectionMasks(value, 100)[0].matrix, [0,2,-2,0,80.75,10.25]);
  value.presentation.geometry!.valid_domains = [{ size: [60,60], matrix: [[1,0,0],[0,1,0],[0,0,1]] }];
  assert.deepEqual(projectInspectionMasks(value, 100)[0].clips[1], [[0,0],[60,0],[60,60],[0,60]]);
  assert.deepEqual(projectInspectionMasks(value, 100, new Set()), []);
  assert.deepEqual(projectInspectionMasks(value, 101), []);
  value.masks[0].image_sequence = 0;
  assert.deepEqual(projectInspectionMasks(value, 100), []);
  value.masks[0].image_sequence = 1;
  value.snapshot.nodes[0].frame!.generation = 2;
  assert.deepEqual(projectInspectionMasks(value, 100), []);
  value.snapshot.nodes[0].frame!.generation = 1;
  (value.snapshot.nodes[0].geometry as PixelGeometry).to_source[2][0] = 0.001;
  assert.deepEqual(projectInspectionMasks(value, 100), []);
});

test("logical consumers share a mask only from the same physical producer and model", () => {
  const value = maskedFixture();
  value.snapshot.nodes.push({ ...value.snapshot.nodes[0], node_id: "consumer", image_sequence: 2 });
  assert.equal(projectInspectionMasks(value, 100).length, 1);
  assert.equal(projectInspectionMasks(value, 100, new Set(["consumer"])).length, 1);
  value.snapshot.nodes[1].model_id = "different-model";
  assert.equal(projectInspectionMasks(value, 100, new Set(["consumer"])).length, 0);
  value.snapshot.nodes[1].model_id = value.snapshot.nodes[0].model_id;
  value.snapshot.nodes[1].producer_epoch = "new-runtime";
  assert.equal(projectInspectionMasks(value, 100, new Set(["consumer"])).length, 0);
});

test("private frozen layers expire without discarding paired public geometry", () => {
  const value = maskedFixture();
  value.snapshot.nodes[0].visuals.push({ ...value.snapshot.nodes[0].visuals[0], kind: "points", raster: null,
    access_scope: "vision.identity", layer: "identity", label: "Synthetic person" });
  assert.equal(expirePrivateVisuals(value, 100), value);
  const filtered = expirePrivateVisuals(value, 101);
  assert.equal(filtered.snapshot.nodes[0].visuals.length, 1);
  assert.equal(filtered.snapshot.nodes[0].visuals[0].layer, "masks");
  assert.equal(filtered.presentation, value.presentation);
  assert.equal(filtered.snapshot.nodes[0].detections.length, 1);
  assert.equal(expirePrivateVisuals(filtered, 102), filtered);
});

test("reader cancels an oversized stream even without Content-Length", async () => {
  let canceled = false;
  const stream = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(400001)); }, cancel() { canceled = true; } });
  await assert.rejects(readPairedResponse(new Response(stream)), /capacity/);
  assert.equal(canceled, true);
  const bytes = encode(fixture());
  assert.deepEqual(await readPairedResponse(new Response(new Uint8Array(bytes).buffer)), bytes);
});

test("pose joints preserve missing slots and require the exact capture and selected operator", () => {
  const value = fixture();
  value.snapshot.nodes[0].visuals = [{ layer: "pose", kind: "points", points: [[0.2,0.3], null, [0.8,0.9]],
    connections: [[0,1],[1,2],[0,2]], subject_id: "track:1", label: "person", score: 0.7, score_kind: "pose_model_score",
    local_id: null, segment_id: null, state: null, sample_sequences: [], raster: null, access_scope: "diagnostic_private", properties: {} }];
  const layers = new Set(["pose"]);
  const [visual] = projectInspectionVisuals(value, 100, layers);
  assert.deepEqual(visual.points, [[20,30], null, [80,90]]);
  assert.deepEqual(visual.connections, [[0,2]]);
  assert.equal(visual.subject_id, "track:1");
  assert.deepEqual(projectInspectionVisuals(value, 100, layers, new Set()), []);
  assert.deepEqual(projectInspectionVisuals(value, 100, new Set()), []);
  assert.deepEqual(projectInspectionBoxes(value, 100, new Set()), []);
  value.snapshot.nodes[0].frame!.sequence = 6;
  assert.deepEqual(projectInspectionVisuals(value, 100, layers), []);
});

test("pose crop and rotation, unsupported joints and shared execution stay bounded", () => {
  const value = fixture();
  const node = value.snapshot.nodes[0];
  node.visuals = [{ layer: "pose", kind: "points", points: [[0.2,0.3], [0.8,0.9]],
    connections: [[0,1]], subject_id: "track:1", label: null, score: null, score_kind: null,
    local_id: null, segment_id: null, state: null, sample_sequences: [], raster: null, access_scope: "diagnostic_private", properties: {} }];
  node.geometry!.to_source = [[0, -0.5, 80], [0.5, 0, 10], [0, 0, 1]];
  value.snapshot.nodes.push({ ...node, node_id: "logical-consumer" });
  const layers = new Set(["pose"]);
  assert.equal(projectInspectionVisuals(value, 100, layers).length, 1);
  assert.deepEqual(projectInspectionVisuals(value, 100, layers)[0].points, [[65,20],[35,50]]);
  assert.equal(projectInspectionVisuals(value, 100, layers, new Set(["logical-consumer"])).length, 1);
  value.presentation.geometry!.valid_domains = [{ size: [40,60], matrix: [[1,0,0],[0,1,0],[0,0,1]] }];
  const result = projectInspectionVisuals(value, 100, layers)[0];
  assert.deepEqual(result.points, [null,[35,50]]);
  assert.deepEqual(result.connections, []);
  value.presentation.geometry = null;
  assert.deepEqual(projectInspectionVisuals(value, 100, layers), []);
});
