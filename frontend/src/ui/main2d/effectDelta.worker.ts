/// <reference lib="webworker" />

import type { Main2DEffectBlendMode } from "@toposync/plugin-api";

import { computeMain2DEffectDeltaCrop } from "./effectDelta";

type EffectDeltaWorkerRequest = {
  id: number;
  width: number;
  height: number;
  blendMode: Main2DEffectBlendMode;
  base: ArrayBuffer;
  active: ArrayBuffer;
};

type EffectDeltaWorkerResponse =
  | {
      id: number;
      ok: true;
      delta: {
        width: number;
        height: number;
        data: ArrayBuffer;
        crop: { x: number; y: number; width: number; height: number };
      } | null;
    }
  | { id: number; ok: false; error: string };

const workerSelf = self as unknown as {
  addEventListener(type: "message", listener: (event: MessageEvent<EffectDeltaWorkerRequest>) => void): void;
  postMessage(message: EffectDeltaWorkerResponse, transfer?: Transferable[]): void;
};

workerSelf.addEventListener("message", (event) => {
  const request = event.data;
  try {
    const delta = computeMain2DEffectDeltaCrop(
      {
        width: request.width,
        height: request.height,
        data: new Uint8ClampedArray(request.base),
      },
      {
        width: request.width,
        height: request.height,
        data: new Uint8ClampedArray(request.active),
      },
      { blendMode: request.blendMode },
    );

    if (!delta) {
      workerSelf.postMessage({ id: request.id, ok: true, delta: null });
      return;
    }

    const dataBuffer = delta.data.buffer as ArrayBuffer;
    workerSelf.postMessage(
      {
        id: request.id,
        ok: true,
        delta: {
          width: request.width,
          height: request.height,
          data: dataBuffer,
          crop: delta.crop,
        },
      },
      [dataBuffer],
    );
  } catch (err) {
    workerSelf.postMessage({
      id: request.id,
      ok: false,
      error: err instanceof Error ? err.message : String(err),
    });
  }
});
