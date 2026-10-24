declare const require: any;
import { inspectionRead } from "./inspectionRead";
const test: (name: string, fn: () => Promise<void>) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

test("selected previews serialize reads, bound pending work and skip a revoked waiter", async () => {
  const controller = new AbortController(), revoked = new AbortController();
  let finish!: () => void, active = 0, maximum = 0;
  const gate = new Promise<void>(resolve => { finish = resolve; });
  const first = inspectionRead(async () => { active++; maximum = Math.max(maximum, active); await gate; active--; return 1; }, controller.signal);
  const second = inspectionRead(async () => { active++; maximum = Math.max(maximum, active); active--; return 2; }, controller.signal);
  const third = inspectionRead(async () => { throw new Error("Must not execute"); }, revoked.signal);
  const rejected = assert.rejects(third, { name: "AbortError" });
  const fourth = inspectionRead(async () => { throw new Error("Synthetic read failure"); }, controller.signal);
  const failed = assert.rejects(fourth, /Synthetic read failure/);
  await assert.rejects(inspectionRead(async () => 5, controller.signal), /capacity/);
  revoked.abort(); finish();
  assert.deepEqual(await Promise.all([first, second]), [1, 2]);
  await Promise.all([rejected, failed]);
  assert.equal(maximum, 1);
  assert.equal(await inspectionRead(async () => 6, controller.signal), 6);
});

test("closing a running preview aborts its read and releases the next preview", async () => {
  const controller = new AbortController();
  let started!: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  const result = inspectionRead(signal => new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("closed", "AbortError")), { once: true });
    started();
  }), controller.signal);
  await ready;
  const rejected = assert.rejects(result, { name: "AbortError" });
  controller.abort(); await rejected;
  assert.equal(await inspectionRead(async () => "next", new AbortController().signal), "next");
});
