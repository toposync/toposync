import { useEffect, useMemo, useState } from "react";

import type { CameraContextsResponse } from "../../../../util/api";
import { getCameraContexts, isAbortError } from "../../../../util/api";

import type { CameraAreaOption } from "../types";

type Result = {
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: CameraAreaOption[];
};

export function useCameraContexts(interactiveCameraId: string): Result {
  const [cameraContextsById, setCameraContextsById] = useState<Record<string, CameraContextsResponse>>({});
  const [cameraContextsErrorById, setCameraContextsErrorById] = useState<Record<string, string>>({});

  useEffect(() => {
    const cameraId = String(interactiveCameraId || "").trim();
    if (!cameraId) return;
    if (cameraContextsById[cameraId]) return;
    if (cameraContextsErrorById[cameraId]) return;

    let cancelled = false;
    const controller = new AbortController();
    void (async () => {
      try {
        const contexts = await getCameraContexts(cameraId, { signal: controller.signal });
        if (cancelled || controller.signal.aborted) return;
        setCameraContextsById((prev) => ({ ...prev, [cameraId]: contexts }));
      } catch (err: any) {
        if (cancelled || isAbortError(err)) return;
        setCameraContextsErrorById((prev) => ({ ...prev, [cameraId]: String(err?.message ?? err) }));
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [interactiveCameraId, cameraContextsById, cameraContextsErrorById]);

  const activeCameraContexts = useMemo(() => {
    const cameraId = String(interactiveCameraId || "").trim();
    if (!cameraId) return null;
    return cameraContextsById[cameraId] ?? null;
  }, [interactiveCameraId, cameraContextsById]);

  const activeCameraContextsError = useMemo(() => {
    const cameraId = String(interactiveCameraId || "").trim();
    if (!cameraId) return null;
    return cameraContextsErrorById[cameraId] ?? null;
  }, [interactiveCameraId, cameraContextsErrorById]);

  const cameraAreaOptions = useMemo<CameraAreaOption[]>(() => {
    const contexts = activeCameraContexts;
    if (!contexts) return [];
    const options: CameraAreaOption[] = [];
    for (const composition of contexts.compositions ?? []) {
      for (const area of composition.areas ?? []) {
        const points = Array.isArray(area.vertices)
          ? area.vertices
              .map((point) => {
                const x = Number((point as any)?.x);
                const z = Number((point as any)?.z);
                return Number.isFinite(x) && Number.isFinite(z) ? { x, z } : null;
              })
              .filter((point): point is { x: number; z: number } => point !== null)
          : [];
        options.push({
          value: `${composition.id}:${area.id}`,
          label: `${composition.name} / ${area.name}`,
          compositionId: composition.id,
          areaId: area.id,
          areaName: area.name,
          points,
        });
      }
    }
    options.sort((a, b) => a.label.localeCompare(b.label));
    return options;
  }, [activeCameraContexts]);

  return { activeCameraContexts, activeCameraContextsError, cameraAreaOptions };
}
