const assert = require("node:assert/strict");
const { test, after } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const typescript = require("typescript");

const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "toposync-panorama-projection-"));
const sourceFile = path.resolve(__dirname, "../src/elements/panoramaProjection.ts");
const compiledFile = path.join(temporaryDirectory, "panoramaProjection.cjs");
fs.writeFileSync(compiledFile, typescript.transpileModule(fs.readFileSync(sourceFile, "utf8"), {
  compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2022 },
}).outputText);
const { createPanoramaProjector } = require(compiledFile);
after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));

// Generate fresh canonical vectors from the existing Python implementation.
// All observations are synthetic; held-out probes never enter the estimator.
const repository = path.resolve(__dirname, "../../../..");
const vectors = JSON.parse(execFileSync(path.join(repository, ".venv/bin/python"), ["-c", `
import json, math
import numpy as np
from toposync_ext_cameras.processing.panorama_mapping import (
    estimate_panorama_mapping, map_world_to_ray, ray_to_panorama_pixel, map_ray_to_world,
)
physical = np.array([[0.94, -0.18, 0.9], [0.2, 0.88, -0.4], [0.08, 0.22, -2.7]])
def point(identifier, x, z, role):
    ray = physical @ [x, z, 1]
    ray /= np.linalg.norm(ray)
    return dict(id=identifier, role=role, world_x=x, world_z=z, ray=ray.tolist())
points = [point(str(i), x, z, 'fit') for i, (x,z) in enumerate(
    [(-6,-6), (6,-6), (6,6), (-6,6), (0,-6), (0,6), (-6,0), (6,0)])]
points += [point('check-one', 2, 1, 'check'), point('check-two', -2, -1, 'check')]
solution = estimate_panorama_mapping(points)
probes = []
for x,z in [(-4, .01), (-4, -.01), (4,2), (-5,5), (0,0), (1.125,-3.25)]:
    ray = map_world_to_ray(solution, x, z)
    probes.append(dict(world=[x,z], panorama=ray_to_panorama_pixel(ray), recovered=map_ray_to_world(solution, ray)))
print(json.dumps(dict(solution=solution, provisional=estimate_panorama_mapping(points[:-2]), probes=probes)))
`], { encoding: "utf8", cwd: repository }));

function mask() { return { width: 64, height: 32, data: new Uint8Array(64 * 32).fill(255) }; }
function close(actual, expected) { assert.ok(Math.abs(actual - expected) < 1e-8, `${actual} should equal ${expected}`); }
function groundSolution() {
  return { ...structuredClone(vectors.solution), matrix: [[1, 0, 0], [0, 1, 0], [0, 0, -3]], inverse_matrix: [[1, 0, 0], [0, 1, 0], [0, 0, -1 / 3]] };
}

test("bidirectional held-out probes match Python spherical geometry before and after checks", () => {
  for (const solution of [vectors.solution, vectors.provisional]) {
    const before = structuredClone(solution);
    const projector = createPanoramaProjector(solution, mask());
    assert.ok(projector);
    for (const probe of vectors.probes) {
      const panorama = projector.worldToPanorama({ x: probe.world[0], z: probe.world[1] });
      close(panorama.x, probe.panorama[0]);
      close(panorama.y, probe.panorama[1]);
      const world = projector.panoramaToWorld({ x: probe.panorama[0], y: probe.panorama[1] });
      close(world.x, probe.recovered[0]);
      close(world.z, probe.recovered[1]);
      close(world.x, probe.world[0]);
      close(world.z, probe.world[1]);
    }
    assert.deepEqual(solution, before);
  }
});

test("full-circle seam, pole, horizon, opposite ray and unsupported ground are explicit", () => {
  const projector = createPanoramaProjector(groundSolution(), mask());
  const left = projector.panoramaToWorld({ x: 0, y: 0.75 });
  const right = projector.panoramaToWorld({ x: 1, y: 0.75 });
  close(left.x, -3);
  close(right.x, left.x);
  close(right.z, left.z);
  assert.deepEqual(projector.worldToPanorama({ x: 0, z: 0 }), { x: 0.5, y: 1 });
  for (const point of [{ x: 0.5, y: 0.5 }, { x: 0.5, y: 0.5 + 1e-13 }, { x: 0.5, y: 0.25 }, { x: 0.5, y: 0.55 }]) {
    assert.equal(projector.panoramaToWorld(point), null);
  }
  assert.equal(projector.worldToPanorama({ x: 8, z: 0 }), null);
  for (const invalid of [NaN, Infinity, "0", undefined]) {
    assert.equal(projector.worldToPanorama({ x: invalid, z: 0 }), null);
    assert.equal(projector.panoramaToWorld({ x: 0.5, y: invalid }), null);
  }
  assert.equal(projector.panoramaToWorld({ x: 1.01, y: 0.75 }), null);
});

test("captured coverage is mandatory and evaluated in both directions at pixel edges", () => {
  for (const invalid of [null, {}, { ...mask(), data: new Uint8Array(1) }, { ...mask(), width: NaN }]) {
    assert.equal(createPanoramaProjector(groundSolution(), invalid), null);
  }
  const coverage = mask();
  const projector = createPanoramaProjector(groundSolution(), coverage);
  const point = projector.worldToPanorama({ x: 3, z: 0 });
  coverage.data[Math.floor(point.y * coverage.height) * coverage.width + Math.floor(point.x * coverage.width)] = 0;
  assert.equal(projector.worldToPanorama({ x: 3, z: 0 }), null);
  assert.equal(projector.panoramaToWorld(point), null);
  assert.ok(projector.panoramaToWorld({ x: 1, y: 0.75 }));
  coverage.data[31 * 64 + 32] = 0;
  assert.equal(projector.worldToPanorama({ x: 0, z: 0 }), null);
});

test("partial coverage at canonical seam and pole aliases never produces a one-way preview", () => {
  const coverage = mask();
  const projector = createPanoramaProjector(groundSolution(), coverage);
  coverage.data[24 * 64] = 0;
  assert.equal(projector.panoramaToWorld({ x: 1, y: 0.75 }), null);
  assert.equal(projector.worldToPanorama({ x: -3, z: 0 }), null);
  coverage.data[31 * 64 + 32] = 0;
  assert.equal(projector.panoramaToWorld({ x: 0.25, y: 1 }), null);
  assert.equal(projector.worldToPanorama({ x: 0, z: 0 }), null);
});

test("explicit quality gate rejects stale eligibility, unknown reasons and malformed geometry", () => {
  const mutations = [
    (solution) => { solution.preview = undefined; },
    (solution) => { solution.preview.eligible = false; },
    (solution) => { solution.quality = undefined; },
    (solution) => { solution.quality.number_of_inliers = 5; },
    (solution) => { solution.quality.inlier_ratio = 0.79; },
    (solution) => { solution.quality.p95_fit_error_radians = 1; },
    (solution) => { solution.quality.reasons = ["future_unknown_reason"]; },
    (solution) => { solution.quality.reasons = ["check_error_exceeds_threshold"]; },
    (solution) => { solution.quality.maximum_check_error_radians = 0.1; },
    (solution) => { solution.quality.number_of_check_points = 0; },
    (solution) => { solution.matrix[0][0] = NaN; },
    (solution) => { solution.matrix = [[1, 2]]; },
    (solution) => { solution.inverse_matrix[0][0] += 1; },
    (solution) => { solution.support_polygon = [[0, 0], [1, 1], [2, 2]]; },
    (solution) => { solution.support_polygon = [[0, 0], [1, 1], [0, 1], [1, 0]]; },
  ];
  for (const mutate of mutations) {
    const solution = structuredClone(vectors.solution);
    mutate(solution);
    assert.equal(createPanoramaProjector(solution, mask()), null, mutate.toString());
  }
});
