import React, { useEffect, useRef, useState } from "react";
import { resolveToposyncUrl, type InspectionSnapshot } from "@toposync/plugin-api";
import { i18n } from "../../util/i18n";
import { decodePairedFrame, projectInspectionBoxes, readPairedResponse, type PairedInspectionMetadata } from "./pairedInspection";

export function PairedInspectionVideo({ session, nodeId, frozen, boxes, onSnapshot, onClosed }: {
  session: string; nodeId: string; frozen: boolean; boxes: boolean;
  onSnapshot: (snapshot: InspectionSnapshot) => void; onClosed: () => void;
}) {
  const { t } = i18n.useI18n();
  const canvas = useRef<HTMLCanvasElement>(null);
  const options = useRef({ frozen, boxes, onSnapshot, onClosed });
  options.current = { frozen, boxes, onSnapshot, onClosed };
  const [state, setState] = useState("waiting");
  const [stamp, setStamp] = useState<number | null>(null);
  useEffect(() => {
    let disposed = false, timer = 0, expiry = 0, emptyReads = 0;
    let presented: PairedInspectionMetadata | null = null;
    const controller = new AbortController();
    const clear = () => {
      presented = null; setStamp(null);
      if (canvas.current) {
        canvas.current.width = 1; canvas.current.height = 1;
        for (const key of ["capture", "boxes", "paintMilliseconds", "snapshotSequence"]) delete canvas.current.dataset[key];
      }
    };
    function changedCapture(next: PairedInspectionMetadata): boolean {
      if (!presented) return false;
      const a = presented.presentation.frame, b = next.presentation.frame;
      return a.capture_instance !== b.capture_instance || a.generation !== b.generation || a.source_id !== b.source_id || a.device_id !== b.device_id;
    }
    async function poll() {
      const timeout = window.setTimeout(() => controller.abort(), 2500);
      let delay = 200;
      try {
        const response = await fetch(resolveToposyncUrl(`/api/streams/inspection/${encodeURIComponent(session)}/${encodeURIComponent(nodeId)}/frame`),
          { credentials: "same-origin", cache: "no-store", signal: controller.signal });
        if (disposed) return;
        if (response.status === 409 || response.status === 429) {
          if (++emptyReads >= 25) throw new Error("inspection_no_frames");
          if (!options.current.frozen) clear();
          setState(options.current.frozen && presented ? "paired" : "waiting"); delay = response.status === 429 ? 1000 : 200;
          return;
        }
        if (!response.ok) throw new Error("inspection_unavailable");
        const { metadata, jpeg } = decodePairedFrame(await readPairedResponse(response));
        if (disposed) return;
        if (metadata.snapshot.session !== session || Date.now() / 1000 >= metadata.expires_at)
          throw new Error("inspection_expired");
        emptyReads = 0;
        options.current.onSnapshot(metadata.snapshot);
        if (changedCapture(metadata)) clear();
        if (options.current.frozen && presented) return;
        const bitmap = await createImageBitmap(new Blob([new Uint8Array(jpeg).buffer], { type: "image/jpeg" }));
        try {
          if (disposed || controller.signal.aborted || !canvas.current) return;
          if (Date.now() / 1000 >= metadata.expires_at) { clear(); setState("waiting"); return; }
          if (bitmap.width !== metadata.presentation.image_size[0] || bitmap.height !== metadata.presentation.image_size[1])
            throw new Error("inspection_geometry_mismatch");
          if (options.current.frozen && presented) return;
          const element = canvas.current;
          element.width = bitmap.width; element.height = bitmap.height;
          const context = element.getContext("2d");
          if (!context) throw new Error("inspection_canvas_unavailable");
          const started = performance.now();
          context.drawImage(bitmap, 0, 0);
          const drawings = options.current.boxes ? projectInspectionBoxes(metadata, Date.now() / 1000) : [];
          context.lineWidth = 2; context.strokeStyle = "#00ff9d"; context.fillStyle = "#00ff9d";
          context.font = "12px sans-serif";
          for (const box of drawings) {
            context.beginPath();
            box.points.forEach(([x,y], index) => index ? context.lineTo(x,y) : context.moveTo(x,y));
            context.closePath(); context.stroke();
            context.fillText(`${box.label} · ${box.score.toFixed(3)}`, box.points[0][0], Math.max(12, box.points[0][1] - 4));
          }
          presented = metadata;
          const reference = metadata.presentation.frame;
          // A bounded sample exposes the actual paint result for diagnostics, not a synthetic player clock.
          element.dataset.capture = `${reference.device_id}/${reference.source_id}/${reference.capture_instance}/${reference.generation}/${reference.sequence}`;
          element.dataset.boxes = String(drawings.length);
          element.dataset.paintMilliseconds = String(performance.now() - started);
          element.dataset.snapshotSequence = String(metadata.snapshot.sequence);
          setStamp(Date.now() / 1000); setState("paired");
        } finally { bitmap.close(); }
      } catch {
        if (!disposed) { clear(); setState("failed"); options.current.onClosed(); }
        disposed = true; controller.abort();
      } finally {
        window.clearTimeout(timeout);
        if (!disposed) timer = window.setTimeout(poll, delay);
      }
    }
    expiry = window.setInterval(() => {
      if (presented && !options.current.frozen && Date.now() / 1000 >= presented.expires_at) { clear(); setState("waiting"); }
    }, 100);
    void poll();
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); window.clearInterval(expiry); clear(); };
  }, [session, nodeId]);
  return <div className="pairedInspectionVideo">
    <canvas ref={canvas} aria-label={t("inspection.paired_video")} />
    <div className="pairedInspectionStatus" role="status">
      {t(state === "paired" ? frozen ? "inspection.frozen" : "inspection.paired_video" : state === "failed" ? "inspection.failed" : "inspection.waiting")}
      {stamp !== null && frozen ? ` · ${new Date(stamp * 1000).toLocaleTimeString()}` : ""}
    </div>
  </div>;
}
