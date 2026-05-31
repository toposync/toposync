declare const require: any;

import type { CompositionElement, EditorToolContext, EditorToolPointerEvent, EditorToolSession, PlanePoint } from "@toposync/plugin-api";

import { DEFAULT_WALL_COLOR, DEFAULT_WALL_WIDTH, WALL_ELEMENT_TYPE_ID, WALL_TOOL_ID } from "./constants";
import { distanceBetweenPoints } from "./geometry";
import { createStructuralTools } from "./tools/structuralTools";
import { buildWallFootprints, buildWallIntervalFootprint } from "./wallGeometry";

const test: (name: string, fn: () => void | Promise<void>) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

function wall(id: string, a: PlanePoint, b: PlanePoint, width = DEFAULT_WALL_WIDTH): CompositionElement {
  return {
    id,
    type: WALL_ELEMENT_TYPE_ID,
    name: id,
    position: { x: (a.x + b.x) / 2, y: 0, z: (a.z + b.z) / 2 },
    rotation: { x: 0, y: 0, z: 0 },
    props: { a, b, width, color: DEFAULT_WALL_COLOR, openings: [] },
  };
}

function pointer(
  kind: EditorToolPointerEvent["kind"],
  rawWorld: PlanePoint,
  options: Partial<EditorToolPointerEvent> = {},
): EditorToolPointerEvent {
  return {
    kind,
    world: options.world ?? rawWorld,
    rawWorld,
    screen: options.screen ?? { x: rawWorld.x * 100, y: rawWorld.z * 100 },
    button: options.button ?? 0,
    buttons: options.buttons ?? 1,
    pointerType: options.pointerType ?? "mouse",
    shiftKey: options.shiftKey ?? false,
    altKey: options.altKey ?? false,
    metaKey: options.metaKey ?? false,
    ctrlKey: options.ctrlKey ?? false,
  };
}

function assertClose(actual: number, expected: number, epsilon = 1e-6): void {
  assert.ok(Math.abs(actual - expected) <= epsilon, `expected ${actual} to be within ${epsilon} of ${expected}`);
}

function assertPointClose(actual: PlanePoint, expected: PlanePoint, epsilon = 1e-6): void {
  assertClose(actual.x, expected.x, epsilon);
  assertClose(actual.z, expected.z, epsilon);
}

function allFinite(points: PlanePoint[]): boolean {
  return points.every((point) => Number.isFinite(point.x) && Number.isFinite(point.z));
}

function must<T>(value: T | null | undefined): T {
  if (value == null) throw new Error("expected value to be present");
  return value;
}

test("L-shaped 90 degree walls share the same joined corner points", () => {
  const footprints = buildWallFootprints([
    wall("horizontal", { x: 0, z: 0 }, { x: 1, z: 0 }),
    wall("vertical", { x: 1, z: 0 }, { x: 1, z: 1 }),
  ]);

  const horizontal = must(footprints.get("horizontal"));
  const vertical = must(footprints.get("vertical"));
  assertPointClose(horizontal.endLeft, vertical.startLeft);
  assertPointClose(horizontal.endRight, vertical.startRight);
});

test("acute angle falls back to local bevel cap when the miter would exceed the limit", () => {
  const angle = (10 * Math.PI) / 180;
  const footprints = buildWallFootprints([
    wall("base", { x: -1, z: 0 }, { x: 0, z: 0 }),
    wall("acute", { x: 0, z: 0 }, { x: -Math.cos(angle), z: Math.sin(angle) }),
  ]);

  const base = must(footprints.get("base"));
  assertPointClose(base.endLeft, { x: 0, z: DEFAULT_WALL_WIDTH / 2 });
  assertPointClose(base.endRight, { x: 0, z: -DEFAULT_WALL_WIDTH / 2 });
});

test("obtuse angle keeps a finite miter instead of falling back to the straight cap", () => {
  const angle = (45 * Math.PI) / 180;
  const footprints = buildWallFootprints([
    wall("base", { x: -1, z: 0 }, { x: 0, z: 0 }),
    wall("obtuse", { x: 0, z: 0 }, { x: Math.cos(angle), z: Math.sin(angle) }),
  ]);

  const base = must(footprints.get("base"));
  const obtuse = must(footprints.get("obtuse"));
  assertPointClose(base.endLeft, obtuse.startLeft);
  assertPointClose(base.endRight, obtuse.startRight);
  assert.ok(base.endLeft.x < -0.005, "expected obtuse miter to move away from the straight cap");
  assert.ok(distanceBetweenPoints(base.endLeft, { x: 0, z: 0 }) <= DEFAULT_WALL_WIDTH + 1e-6);
});

test("colinear connected walls close on the same cap edge", () => {
  const footprints = buildWallFootprints([
    wall("left", { x: 0, z: 0 }, { x: 1, z: 0 }),
    wall("right", { x: 1, z: 0 }, { x: 2, z: 0 }),
  ]);

  const left = must(footprints.get("left"));
  const right = must(footprints.get("right"));
  assertPointClose(left.endLeft, right.startLeft);
  assertPointClose(left.endRight, right.startRight);
});

test("near endpoints join only inside the width-based tolerance", () => {
  const inside = buildWallFootprints([
    wall("a", { x: 0, z: 0 }, { x: 1, z: 0 }),
    wall("b", { x: 1.05, z: 0 }, { x: 2.05, z: 0 }),
  ]);
  const outside = buildWallFootprints([
    wall("a", { x: 0, z: 0 }, { x: 1, z: 0 }),
    wall("b", { x: 1.07, z: 0 }, { x: 2.07, z: 0 }),
  ]);

  assertClose(inside.get("a")?.endLeft.x ?? NaN, 1.025);
  assertClose(inside.get("b")?.startLeft.x ?? NaN, 1.025);
  assertClose(outside.get("a")?.endLeft.x ?? NaN, 1);
  assertClose(outside.get("b")?.startLeft.x ?? NaN, 1.07);
});

test("opening interval near but not touching an endpoint keeps the opening cut straight", () => {
  const footprints = buildWallFootprints([
    wall("horizontal", { x: 0, z: 0 }, { x: 1, z: 0 }),
    wall("vertical", { x: 1, z: 0 }, { x: 1, z: 1 }),
  ]);
  const horizontal = must(footprints.get("horizontal"));

  const nearEnd = must(buildWallIntervalFootprint(horizontal, 0.95, 1));
  assertClose(nearEnd.points[0].x, 0.95);
  assertClose(nearEnd.points[3].x, 0.95);
  assertPointClose(nearEnd.points[1], horizontal.endLeft);
  assertPointClose(nearEnd.points[2], horizontal.endRight);
});

test("degree three junction uses local caps and keeps finite coordinates", () => {
  const footprints = buildWallFootprints([
    wall("west", { x: -1, z: 0 }, { x: 0, z: 0 }),
    wall("east", { x: 0, z: 0 }, { x: 1, z: 0 }),
    wall("north", { x: 0, z: 0 }, { x: 0, z: 1 }),
  ]);

  assert.equal(footprints.size, 3);
  for (const footprint of footprints.values()) {
    assert.ok(allFinite(footprint.polygon));
    assert.equal(footprint.startNode.refs.length === 3 || footprint.endNode.refs.length === 3, true);
  }
});

function createWallToolHarness(): { elements: CompositionElement[]; session: EditorToolSession } {
  const elements: CompositionElement[] = [];
  let nextElementId = 1;
  const tool = must(createStructuralTools({} as any).find((candidate) => candidate.id === WALL_TOOL_ID));

  const context: EditorToolContext = {
    i18n: {} as any,
    getElements: () => elements,
    createElement: (typeId, init) => {
      const element: CompositionElement = {
        id: `created-${nextElementId}`,
        type: typeId,
        name: init?.name ?? "",
        position: {
          x: init?.position?.x ?? 0,
          y: init?.position?.y ?? 0,
          z: init?.position?.z ?? 0,
        },
        rotation: {
          x: init?.rotation?.x ?? 0,
          y: init?.rotation?.y ?? 0,
          z: init?.rotation?.z ?? 0,
        },
        props: init?.props ?? {},
      };
      nextElementId += 1;
      elements.push(element);
      return element.id;
    },
    updateElement: () => undefined,
    removeElement: () => undefined,
    openEditor: () => undefined,
    closeEditor: () => undefined,
  };

  return { elements, session: tool.createSession(context) };
}

test("wall tool snaps a new wall start to an existing wall endpoint", () => {
  const { elements, session } = createWallToolHarness();

  session.onPointerEvent?.(pointer("down", { x: 0, z: 0 }, { altKey: true }));
  session.onPointerEvent?.(pointer("down", { x: 1.03, z: 0.02 }, { altKey: true }));
  assert.equal(elements.length, 1);

  const firstEnd = elements[0].props.b as PlanePoint;
  session.onPointerEvent?.(
    pointer("down", { x: 1.055, z: 0.04 }, { world: { x: 1.1, z: 0 }, altKey: false }),
  );
  session.onPointerEvent?.(pointer("down", { x: 1.03, z: 1.02 }, { world: { x: 1, z: 1 }, altKey: false }));

  assert.equal(elements.length, 2);
  assert.deepEqual(elements[1].props.a, firstEnd);
});

test("wall tool Alt bypasses endpoint and grid snap", () => {
  const { elements, session } = createWallToolHarness();

  session.onPointerEvent?.(pointer("down", { x: 0, z: 0 }, { altKey: true }));
  session.onPointerEvent?.(pointer("down", { x: 1.03, z: 0.02 }, { altKey: true }));
  assert.equal(elements.length, 1);

  const rawStart = { x: 1.055, z: 0.04 };
  session.onPointerEvent?.(pointer("down", rawStart, { world: rawStart, altKey: true }));
  session.onPointerEvent?.(pointer("down", { x: 1.03, z: 1.02 }, { world: { x: 1.03, z: 1.02 }, altKey: true }));

  assert.equal(elements.length, 2);
  assert.deepEqual(elements[1].props.a, rawStart);
  assert.notDeepEqual(elements[1].props.a, elements[0].props.b);
});
