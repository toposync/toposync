import React, { useEffect, useRef, useState } from "react";
import { resolveToposyncUrl, type InspectionSnapshot } from "@toposync/plugin-api";
import { i18n } from "../../util/i18n";
import { inspectionRead } from "./inspectionRead";
import { decodePairedFrame, expirePrivateVisuals, placeInspectionLabel, projectInspectionBoxes, projectInspectionMasks, projectInspectionVisuals, readInspectionResponse, type PairedInspectionMetadata } from "./pairedInspection";

export function PairedInspectionVideo({ session, nodeId, frozen, boxes, layers, selectedNodes, onSnapshot, onClosed }: {
  session: string; nodeId: string; frozen: boolean; boxes: boolean; layers: ReadonlySet<string>; selectedNodes?: ReadonlySet<string>;
  onSnapshot: (snapshot: InspectionSnapshot) => void; onClosed: () => void;
}) {
  const { t } = i18n.useI18n();
  const canvas = useRef<HTMLCanvasElement>(null);
  const options = useRef({ frozen, boxes, layers, selectedNodes, onSnapshot, onClosed });
  options.current = { frozen, boxes, layers, selectedNodes, onSnapshot, onClosed };
  const repaint = useRef<() => void>(() => {});
  const [state, setState] = useState("waiting");
  const [stamp, setStamp] = useState<number | null>(null);
  const [privateExpired, setPrivateExpired] = useState(false);
  useEffect(() => {
    let disposed = false, timer = 0, expiry = 0, emptyReads = 0;
    let presented: PairedInspectionMetadata | null = null;
    let retained: ImageBitmap | null = null, presentedAt = 0;
    let retainedMasks: ImageBitmap[] = [];
    const controller = new AbortController();
    const clear = () => {
      presented = null; setStamp(null);
      setPrivateExpired(false);
      retained?.close(); retained = null;
      retainedMasks.forEach(mask => mask.close()); retainedMasks = [];
      if (canvas.current) {
        canvas.current.width = 1; canvas.current.height = 1;
        for (const key of ["capture", "boxes", "visuals", "masks", "paintMilliseconds", "snapshotSequence"]) delete canvas.current.dataset[key];
      }
    };
    function paint() {
      const element = canvas.current;
      if (!presented || !retained || !element) return;
      if (!options.current.frozen && Date.now() / 1000 >= presented.expires_at) { clear(); setState("waiting"); return; }
      const context = element.getContext("2d");
      if (!context) throw new Error("inspection_canvas_unavailable");
      const started = performance.now();
      context.clearRect(0, 0, element.width, element.height);
      context.drawImage(retained, 0, 0);
      const maskDrawings = options.current.layers.has("masks") ? projectInspectionMasks(presented, presentedAt, options.current.selectedNodes) : [];
      for (const drawing of maskDrawings) {
        const mask = retainedMasks[drawing.attachment];
        if (!mask) continue;
        context.save();
        for (const polygon of drawing.clips) {
          context.beginPath();
          polygon.forEach(([x,y], index) => index ? context.lineTo(x,y) : context.moveTo(x,y));
          context.closePath(); context.clip();
        }
        context.setTransform(...drawing.matrix);
        context.imageSmoothingEnabled = false;
        context.drawImage(mask, 0, 0); context.restore();
      }
      // A frozen view reuses its exact authorized pixels and geometry, never the next snapshot.
      const drawings = options.current.boxes ? projectInspectionBoxes(presented, presentedAt, options.current.selectedNodes) : [];
      const visuals = projectInspectionVisuals(presented, presentedAt, options.current.layers, options.current.selectedNodes);
      context.lineWidth = 2; context.strokeStyle = "#00ff9d"; context.fillStyle = "#00ff9d";
      const fontSize = Math.max(12, Math.ceil(12 * element.width / (element.clientWidth || element.width)));
      context.font = `${fontSize}px sans-serif`;
      const occupied: number[][] = visuals.filter(visual => ["mapping", "zones"].includes(visual.layer))
        .flatMap(visual => visual.points.flatMap(point => point ? [[point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5]] : []));
      const label = (text: string, x: number, y: number) => {
        const position = placeInspectionLabel(x, y, context.measureText(text).width + 4, element.width, element.height, occupied, fontSize + 2);
        if (!position) return;
        context.save(); context.strokeStyle = "#000"; context.lineWidth = 3;
        context.strokeText(text, position[0], position[1]);
        context.fillText(text, position[0], position[1]); context.restore();
      };
      for (const box of drawings) {
        context.beginPath();
        box.points.forEach(([x,y], index) => index ? context.lineTo(x,y) : context.moveTo(x,y));
        context.closePath(); context.stroke();
        label(`${box.label} · ${box.score.toFixed(3)}`, box.points[0][0], box.points[0][1] - 4);
      }
      context.strokeStyle = "#ffd166"; context.fillStyle = "#ffd166";
      for (const visual of visuals) {
        if (visual.kind === "points") {
          for (const [a,b] of visual.connections) {
            const start = visual.points[a]!, end = visual.points[b]!;
            context.beginPath(); context.moveTo(start[0], start[1]); context.lineTo(end[0], end[1]); context.stroke();
          }
          for (const point of visual.points) if (point) {
            context.beginPath(); context.arc(point[0], point[1], 3, 0, Math.PI * 2); context.fill();
            if (visual.layer === "gestures" && visual.label)
              label(`${t(`inspection.${visual.label}`, {}, visual.label)} · ${t(`inspection.${visual.state}`)}`, point[0] + 5, point[1] - 5);
            if (visual.layer === "mapping" || visual.layer === "zones")
              label(visual.layer === "zones" ? `${visual.properties.zone_name || ""} · ${t(`inspection.${visual.state}`)}`
                : t("inspection.mapping_anchor"), point[0] + 5, point[1] - 5);
          }
        } else if (visual.points.length) {
          context.beginPath();
          visual.points.forEach((point, index) => index ? context.lineTo(point![0], point![1]) : context.moveTo(point![0], point![1]));
          if (visual.kind === "polygon") context.closePath();
          context.stroke();
          if (visual.layer === "tracks" && visual.subject_id) {
            const point = visual.points[0]!;
            label(visual.subject_id, point[0], point[1] - 4);
          }
          if (visual.layer === "faces" && visual.label && options.current.layers.has("identity")) {
            const point = visual.points[0]!;
            label(visual.label, point[0], point[1] - 4);
          }
          if (visual.layer === "groups") {
            const point = visual.points[0]!;
            label(`${t("inspection.group_member")} · ${visual.properties.member_count}`, point[0], point[1] - 4);
          }
        }
      }
      const reference = presented.presentation.frame;
      element.dataset.capture = `${reference.device_id}/${reference.source_id}/${reference.capture_instance}/${reference.generation}/${reference.sequence}`;
      element.dataset.boxes = String(drawings.length); element.dataset.visuals = String(visuals.length);
      element.dataset.masks = String(maskDrawings.length);
      element.dataset.paintMilliseconds = String(performance.now() - started);
      element.dataset.snapshotSequence = String(presented.snapshot.sequence);
    }
    repaint.current = paint;
    function changedCapture(next: PairedInspectionMetadata): boolean {
      if (!presented) return false;
      const a = presented.presentation.frame, b = next.presentation.frame;
      return a.capture_instance !== b.capture_instance || a.generation !== b.generation || a.source_id !== b.source_id || a.device_id !== b.device_id
        || presented.snapshot.policy_revision !== next.snapshot.policy_revision;
    }
    async function poll() {
      let delay = options.current.layers.has("masks") ? 500 : 200;
      try {
        const response = await inspectionRead(async signal => {
          const response = await fetch(resolveToposyncUrl(`/api/streams/inspection/${encodeURIComponent(session)}/${encodeURIComponent(nodeId)}/frame${options.current.layers.has("masks") ? "?masks=true" : ""}`),
            { credentials: "same-origin", cache: "no-store", signal });
          if (response.status === 409 || response.status === 429) return { status: response.status, detail: (await response.json()).detail };
          if (!response.ok) throw new Error("inspection_unavailable");
          return { status: response.status, frame: decodePairedFrame(await readInspectionResponse(response)) };
        }, controller.signal);
        if (disposed) return;
        if (response.status === 409 || response.status === 429) {
          if (response.status === 409 && response.detail !== "inspection_frame_not_paired")
            throw new Error("inspection_access_or_capture_changed");
          emptyReads = Math.min(25, emptyReads + 1);
          if (!options.current.frozen) clear();
          setState(options.current.frozen && presented ? "paired" : "waiting"); delay = response.status === 429 || emptyReads >= 25 ? 1000 : 200;
          return;
        }
        if (!response.frame) throw new Error("inspection_unavailable");
        const { metadata, jpeg, masks } = response.frame;
        if (disposed) return;
        if (metadata.snapshot.session !== session || Date.now() / 1000 >= metadata.expires_at)
          throw new Error("inspection_expired");
        emptyReads = 0;
        if (changedCapture(metadata)) clear();
        if (options.current.frozen && presented) return;
        let bitmap: ImageBitmap | null = await createImageBitmap(new Blob([new Uint8Array(jpeg).buffer], { type: "image/jpeg" }));
        let decodedMasks: ImageBitmap[] = [];
        try {
          for (let index = 0; index < masks.length; index++) {
            if (disposed || controller.signal.aborted) return;
            const mask = await createImageBitmap(new Blob([new Uint8Array(masks[index]).buffer], { type: "image/png" }));
            decodedMasks.push(mask);
            if (mask.width !== metadata.masks[index].width || mask.height !== metadata.masks[index].height)
              throw new Error("inspection_mask_geometry_mismatch");
          }
          if (disposed || controller.signal.aborted || !canvas.current) return;
          if (Date.now() / 1000 >= metadata.expires_at) { clear(); setState("waiting"); return; }
          if (bitmap.width !== metadata.presentation.image_size[0] || bitmap.height !== metadata.presentation.image_size[1])
            throw new Error("inspection_geometry_mismatch");
          if (options.current.frozen && presented) return;
          const element = canvas.current;
          element.width = bitmap.width; element.height = bitmap.height;
          retained?.close(); retained = bitmap; bitmap = null;
          retainedMasks.forEach(mask => mask.close()); retainedMasks = decodedMasks; decodedMasks = [];
          presented = metadata;
          options.current.onSnapshot(metadata.snapshot);
          setPrivateExpired(false);
          presentedAt = Date.now() / 1000;
          paint(); setStamp(presentedAt); setState("paired");
        } finally { bitmap?.close(); decodedMasks.forEach(mask => mask.close()); }
      } catch {
        if (!disposed) { clear(); setState("failed"); options.current.onClosed(); }
        disposed = true; controller.abort();
      } finally {
        if (!disposed) timer = window.setTimeout(poll, delay);
      }
    }
    expiry = window.setInterval(() => {
      if (presented && !options.current.frozen && Date.now() / 1000 >= presented.expires_at) { clear(); setState("waiting"); }
      if (presented && options.current.frozen) {
        const filtered = expirePrivateVisuals(presented, Date.now() / 1000);
        if (filtered !== presented) {
          presented = filtered; options.current.onSnapshot(filtered.snapshot); setPrivateExpired(true); paint();
        }
      }
    }, 100);
    void poll();
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); window.clearInterval(expiry); repaint.current = () => {}; clear(); };
  }, [session, nodeId]);
  useEffect(() => { repaint.current(); }, [boxes, layers, selectedNodes, frozen]);
  return <div className="pairedInspectionVideo">
    <canvas ref={canvas} aria-label={t("inspection.paired_video")} />
    <div className="pairedInspectionStatus" role="status">
      {t(state === "paired" ? frozen ? "inspection.frozen" : "inspection.paired_video" : state === "failed" ? "inspection.failed" : "inspection.waiting")}
      {stamp !== null && frozen ? ` · ${new Date(stamp * 1000).toLocaleTimeString()}` : ""}
      {privateExpired ? ` · ${t("inspection.private_expired")}` : ""}
    </div>
  </div>;
}
