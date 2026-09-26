import { useCallback, useEffect, useRef, useState } from "react";

import { createCameraSourcePanorama, fetchCameraSourcePanorama, fetchCameraSourcePanoramaJob, finalizeCameraSourcePanoramaReplacement, operateCameraSourcePanorama, saveCameraSourcePanoramaCrop } from "../api/camerasApi";
import { createUniqueId } from "../parsing";
import { fetchPanoramaNativeReferences, operatePanoramaNativeReference, type PanoramaNativeReference } from "../api/camerasApi";
import type { CameraSourcePanorama, CameraSourcePanoramaArtifact, CameraSourcePanoramaCrop, CameraSourcePanoramaJob } from "../types";

export function isPanoramaJobRunning(job: CameraSourcePanoramaJob | null | undefined): boolean {
  return Boolean(job && ["queued", "preparing", "exploring", "capturing", "returning", "processing", "stopping"].includes(job.status));
}

export function useCameraSourcePanorama(cameraId: string, sourceId: string, enabled: boolean) {
  const [data, setData] = useState<CameraSourcePanorama | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [connectionError, setConnectionError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [refreshCount, setRefreshCount] = useState(0);
  const mutationGeneration = useRef(0);
  const operation = useRef<{ generation: number; name: string; jobId?: string } | null>(null);
  const mounted = useRef(true);
  const idempotencyKey = useRef<string | null>(null);
  const previousJobAtStart = useRef<string | null>(null);
  const latestJobId = useRef<string | null>(null);
  const latestJob = useRef<CameraSourcePanoramaJob | null>(null);
  const replacementAttempt = useRef<string | null>(null);
  const [nativeReferences, setNativeReferences] = useState<PanoramaNativeReference[] | null>(null);
  const [nativeConnectionError, setNativeConnectionError] = useState(false);
  const nativeAttempt = useRef<string | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const refresh = useCallback(() => setRefreshCount((value) => value + 1), []);
  useEffect(() => {
    if (!enabled) { setLoading(false); return; }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let failures = 0;
    async function poll() {
      if (cancelled) return;
      controller = new AbortController();
      const currentController = controller;
      const timeout = setTimeout(() => currentController.abort(), 10_000);
      const generation = mutationGeneration.current;
      let delay = 10_000;
      try {
        // Keep observing a known job even when its source is disabled or changed.
        // Its own endpoint does not depend on the source configuration form.
        const pendingJobId = ["reconstruct", "resume", "return", "stop", "cleanup"].includes(operation.current?.name ?? "") ? operation.current?.jobId : undefined;
        const observedJobId = isPanoramaJobRunning(latestJob.current) ? latestJob.current!.id : pendingJobId;
        if (observedJobId) {
          const nextJob = await fetchCameraSourcePanoramaJob(observedJobId, controller.signal);
          if (cancelled) return;
          if (generation === mutationGeneration.current) {
            latestJob.current = nextJob;
            setData((previous) => previous ? { ...previous, job: nextJob } : previous);
          }
          if (isPanoramaJobRunning(nextJob)) {
            setConnectionError(null);
            failures = 0;
            delay = 1_500;
            return;
          }
        }
        let next = await fetchCameraSourcePanorama(cameraId, sourceId, controller.signal);
        const pendingReplacement = next.replacement_pending ? next.candidate?.id : null;
        if (pendingReplacement && replacementAttempt.current !== pendingReplacement) {
          replacementAttempt.current = pendingReplacement;
          try {
            next = await finalizeCameraSourcePanoramaReplacement(cameraId, sourceId, controller.signal);
          } catch {
            // Read-only viewers still receive the current panorama. A writer
            // will finish the one-time transition on a later visit.
          }
        }
        if (cancelled) return;
        const referenceArtifact = next.active;
        let nativeRunning = false;
        if (referenceArtifact) {
          try {
            const catalog = await fetchPanoramaNativeReferences(cameraId, sourceId, referenceArtifact.id, referenceArtifact.revision, controller.signal);
            if (cancelled) return;
            if (generation === mutationGeneration.current) {
              setNativeReferences(catalog.references);
              setNativeConnectionError(false);
              const attempt = catalog.references.find((reference) => reference.id === nativeAttempt.current);
              if (attempt?.status === "ready" || (attempt?.status === "unverified" && attempt.cleanup_confirmed)) nativeAttempt.current = null;
            }
            nativeRunning = catalog.references.some((reference) => ["queued", "preparing", "stopping"].includes(reference.status));
          } catch {
            if (cancelled) return;
            if (generation === mutationGeneration.current) setNativeConnectionError(true);
          }
        } else if (generation === mutationGeneration.current) {
          setNativeReferences(null);
        }
        if (generation === mutationGeneration.current) {
          setData(next);
          latestJobId.current = next.job?.id ?? null;
          latestJob.current = next.job;
          if (next.job && idempotencyKey.current && next.job.id !== previousJobAtStart.current) idempotencyKey.current = null;
        }
        setConnectionError(null);
        failures = 0;
        delay = isPanoramaJobRunning(next.job) || operation.current || nativeRunning ? 1_500 : 10_000;
      } catch (error) {
        if (cancelled) return;
        setConnectionError(error);
        delay = Math.min(15_000, 1_500 * 2 ** Math.min(++failures, 4));
      } finally {
        clearTimeout(timeout);
        if (!cancelled) {
          setLoading(false);
          timer = setTimeout(() => void poll(), delay);
        }
      }
    }
    void poll();
    const onFocus = () => { if (!cancelled) refresh(); };
    window.addEventListener("focus", onFocus);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller?.abort();
      window.removeEventListener("focus", onFocus);
    };
  }, [cameraId, sourceId, enabled, refreshCount, refresh]);

  const perform = useCallback(async <T,>(name: string, action: () => Promise<T>, accept: (result: T) => void, jobId?: string): Promise<T | null> => {
    if (operation.current && (name !== "stop" || operation.current.name === "stop")) return null;
    const generation = ++mutationGeneration.current;
    operation.current = { generation, name, jobId };
    setBusy(name);
    setActionError(null);
    refresh();
    try {
      const result = await action();
      if (generation !== mutationGeneration.current) return null;
      if (mounted.current) accept(result);
      return result;
    } catch (error) {
      if (mounted.current && generation === mutationGeneration.current) setActionError(error);
      return null;
    } finally {
      if (generation === mutationGeneration.current) {
        operation.current = null;
        mutationGeneration.current += 1;
        if (mounted.current) { setBusy(null); refresh(); }
      }
    }
  }, [enabled, refresh]);

  const acceptJob = useCallback((job: CameraSourcePanoramaJob) => {
    const time = (value: string | number) => typeof value === "number" ? value < 1e12 ? value * 1000 : value : Date.parse(value);
    if (latestJob.current?.id === job.id && time(latestJob.current.updated_at) > time(job.updated_at)) return;
    latestJobId.current = job.id;
    latestJob.current = job;
    setData((previous) => ({ camera_id: cameraId, source_id: sourceId, active: previous?.active ?? null, previous: previous?.previous ?? null, candidate: previous?.candidate ?? null, job }));
  }, [cameraId, sourceId]);

  const start = useCallback(() => {
    // An uncertain create must reuse its key, even if the user clicks Retry.
    if (!idempotencyKey.current) {
      idempotencyKey.current = createUniqueId();
      previousJobAtStart.current = latestJobId.current;
    }
    return perform("start", () => createCameraSourcePanorama(cameraId, sourceId, idempotencyKey.current!), (job) => {
      idempotencyKey.current = null;
      acceptJob(job);
    });
  }, [cameraId, sourceId, perform, acceptJob]);

  const operate = useCallback((jobId: string, action: "stop" | "resume" | "return" | "reconstruct" | "cleanup") =>
    perform(action, () => operateCameraSourcePanorama(jobId, action), acceptJob, jobId), [perform, acceptJob]);

  const saveCrop = useCallback((artifact: CameraSourcePanoramaArtifact, crop: CameraSourcePanoramaCrop) =>
    perform("crop", () => saveCameraSourcePanoramaCrop(cameraId, sourceId, artifact.id, artifact.crop_revision, crop), (next) => {
      setData((previous) => previous ? previous.active?.id === next.id ? { ...previous, active: next } : { ...previous, candidate: next } : previous);
    }), [cameraId, sourceId, perform]);

  const operateReference = useCallback((artifact: CameraSourcePanoramaArtifact, action: "prepare" | "stop" | "remove", identifier?: string) => {
    if (action === "prepare" && !nativeAttempt.current) {
      nativeAttempt.current = Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("");
    }
    const id = identifier ?? nativeAttempt.current;
    if (!id) return Promise.resolve(null);
    return perform(action === "stop" ? "stop" : `${action}_reference`,
      () => operatePanoramaNativeReference(cameraId, sourceId, artifact.id, artifact.revision, id, action),
      (result) => {
        // A transport failure preserves the key. Only a terminal server
        // response permits another preparation to create a new device point.
        if (["ready", "interrupted", "retired"].includes(result.status) && nativeAttempt.current === id) nativeAttempt.current = null;
      });
  }, [cameraId, sourceId, perform]);

  return { data, loading, connectionError, actionError, busy, refresh, start, operate, saveCrop,
    nativeReferences, nativeConnectionError, nativeAttemptId: nativeAttempt.current, operateReference, clearActionError: () => setActionError(null) };
}
