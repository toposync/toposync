declare const require: any;
import { decodePairedFrame, projectInspectionBoxes, projectInspectionVisuals, readPairedResponse, type PairedInspectionMetadata, type PixelGeometry } from "./pairedInspection";
const test: (name: string, fn: () => void | Promise<void>) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

function fixture(): PairedInspectionMetadata {
  const frame = { source_id: "main", device_id: "camera", capture_instance: "capture", generation: 1, sequence: 7 };
  const geometry: PixelGeometry = { image_size: [100, 100], source_size: [100, 100], to_source: [[1,0,0],[0,1,0],[0,0,1]], valid_domains: [] };
  return { schema_version: 1, transport: "paired_jpeg", expires_at: 101,
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

function encode(metadata: PairedInspectionMetadata): Uint8Array {
  const json = new TextEncoder().encode(JSON.stringify(metadata));
  const bytes = new Uint8Array(json.length + 8);
  new DataView(bytes.buffer).setUint32(0, json.length);
  bytes.set(json, 4); bytes.set([255,216,255,217], json.length + 4);
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
    connections: [[0,1],[1,2],[0,2]], subject_id: "track:1", label: "person", score: 0.7, score_kind: "pose_model_score" }];
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
    connections: [[0,1]], subject_id: "track:1", label: null, score: null, score_kind: null }];
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
