import { useCallback, useLayoutEffect, useRef } from "react";
import type { PipelineReferenceCapture, PipelineReferenceFrame } from "@toposync/plugin-api";
import { fetchPipelineReferenceFrame, type PipelinePreviewFrameRequest } from "../../../../../util/api";

/** One pending capture per panel revision, including when the transport ignores abort. */
export function createPipelineReferenceCapture(
  request: PipelinePreviewFrameRequest,
  fetchFrame: (request: PipelinePreviewFrameRequest, signal: AbortSignal) => Promise<PipelineReferenceFrame> = fetchPipelineReferenceFrame,
) {
  let disposed = false;
  let pending: AbortController | null = null;
  return {
    async capture(signal?: AbortSignal): Promise<PipelineReferenceFrame> {
      if (disposed) throw new DOMException("Capture context changed", "AbortError");
      signal?.throwIfAborted();
      pending?.abort();
      const controller = new AbortController();
      pending = controller;
      const abort = () => controller.abort();
      signal?.addEventListener("abort", abort, { once: true });
      try {
        const result = await fetchFrame(request, controller.signal);
        controller.signal.throwIfAborted();
        return result;
      } finally {
        signal?.removeEventListener("abort", abort);
        if (pending === controller) pending = null;
      }
    },
    dispose() {
      disposed = true;
      pending?.abort();
      pending = null;
    },
  };
}

export function usePipelineReferenceCapture(
  request: PipelinePreviewFrameRequest | null,
  revision: string,
  reason: Extract<PipelineReferenceCapture, { available: false }>["reason"],
): PipelineReferenceCapture {
  const active = useRef<{ revision: string; session: ReturnType<typeof createPipelineReferenceCapture> } | null>(null);
  useLayoutEffect(() => {
    if (!request) return;
    const session = createPipelineReferenceCapture(request);
    active.current = { revision, session };
    return () => {
      session.dispose();
      if (active.current?.session === session) active.current = null;
    };
  }, [request, revision]);
  const capture = useCallback((signal?: AbortSignal) => {
    if (active.current?.revision !== revision) {
      return Promise.reject(new DOMException("Capture context changed", "AbortError"));
    }
    return active.current.session.capture(signal);
  }, [revision]);
  return request ? { available: true, capture } : { available: false, reason };
}
