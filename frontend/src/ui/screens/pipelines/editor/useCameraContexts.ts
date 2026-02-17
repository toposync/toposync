import { useEffect, useMemo, useState } from "react";

import type { CameraContextsResponse } from "../../../../util/api";
import { getCameraContexts } from "../../../../util/api";

import type { SelectOption } from "../types";

type Result = {
  activeCameraContexts: CameraContextsResponse | null;
  activeCameraContextsError: string | null;
  cameraAreaOptions: SelectOption[];
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
    void (async () => {
      try {
        const contexts = await getCameraContexts(cameraId);
        if (cancelled) return;
        setCameraContextsById((prev) => ({ ...prev, [cameraId]: contexts }));
      } catch (err: any) {
        if (cancelled) return;
        setCameraContextsErrorById((prev) => ({ ...prev, [cameraId]: String(err?.message ?? err) }));
      }
    })();

    return () => {
      cancelled = true;
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

  const cameraAreaOptions = useMemo<SelectOption[]>(() => {
    const contexts = activeCameraContexts;
    if (!contexts) return [];
    const options: SelectOption[] = [];
    for (const composition of contexts.compositions ?? []) {
      for (const area of composition.areas ?? []) {
        options.push({
          value: `${composition.id}:${area.id}`,
          label: `${composition.name} / ${area.name}`,
        });
      }
    }
    options.sort((a, b) => a.label.localeCompare(b.label));
    return options;
  }, [activeCameraContexts]);

  return { activeCameraContexts, activeCameraContextsError, cameraAreaOptions };
}

