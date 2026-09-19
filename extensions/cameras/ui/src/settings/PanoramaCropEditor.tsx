import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { HostUi, NavigableViewportController } from "@toposync/plugin-api";
import { resolveToposyncUrl } from "@toposync/plugin-api";
import type { CameraSourcePanoramaArtifact, CameraSourcePanoramaCrop } from "../types";
import { adjustPanoramaCrop, FULL_PANORAMA_CROP, panoramaCropFromCorners, panoramaCropSeamOffset, panoramaCropSegments, samePanoramaCrop, validPanoramaCrop, wrapPanoramaCoordinate } from "./panoramaCrop";
import type { PanoramaCropHandle, PanoramaPoint } from "./panoramaCrop";

export type PanoramaTranslate = (key: string, parameters?: Record<string, unknown>) => string;

export function PanoramaImage({ ui, text, artifact, crop = FULL_PANORAMA_CROP, label, errorLabel }: {
  ui: HostUi;
  text: PanoramaTranslate;
  artifact: Pick<CameraSourcePanoramaArtifact, "width" | "height" | "image_url">;
  crop?: CameraSourcePanoramaCrop;
  label: string;
  errorLabel: string;
}): React.ReactElement {
  const [failed, setFailed] = useState(false);
  const controller = useRef<NavigableViewportController | null>(null);
  const [zoom, setZoom] = useState(1);
  if (failed) return <p className="sourcePanoramaError" role="alert">{errorLabel}</p>;
  const ratio = artifact.width * crop.u_width / (artifact.height * crop.v_height);
  return <div>
    <div className="sourcePanoramaActions" role="toolbar" aria-label={text("image_tools")}>
      <button className="iconButton" type="button" aria-label={text("image_zoom_out")} disabled={zoom <= 0.5} onClick={() => controller.current?.zoomBy(1 / 1.25)}>−</button>
      <button className="iconButton" type="button" aria-label={text("image_zoom_in")} disabled={zoom >= 16} onClick={() => controller.current?.zoomBy(1.25)}>+</button>
      <button className="chipButton" type="button" onClick={() => controller.current?.fit()}>{text("fit_view")}</button>
    </div>
    <ui.NavigableViewport className="sourcePanoramaPreviewViewport" label={label} contentKey={`${artifact.image_url}:${crop.u_start}:${crop.u_width}:${crop.v_start}:${crop.v_height}`} contentSize={{ width: artifact.width * crop.u_width, height: artifact.height * crop.v_height }} controllerRef={controller} onViewChange={(state) => setZoom(state.zoom)}>
    <div className="sourcePanoramaImage" role="img" aria-label={label} style={{ aspectRatio: ratio, width: "100%", height: "100%" }}>
    {[0, 1].map((copy) => <img key={copy} src={resolveToposyncUrl(artifact.image_url)} alt="" aria-hidden="true" draggable={false} onError={() => setFailed(true)} style={{ width: `${100 / crop.u_width}%`, height: `${100 / crop.v_height}%`, left: `${(copy - crop.u_start) * 100 / crop.u_width}%`, top: `${-crop.v_start * 100 / crop.v_height}%` }} />)}
    </div>
    </ui.NavigableViewport>
    <p className="cardMeta">{text("navigation_help")}</p>
  </div>;
}

export async function exportPanoramaCrop(artifact: CameraSourcePanoramaArtifact, crop: CameraSourcePanoramaCrop): Promise<Blob> {
  const response = await fetch(resolveToposyncUrl(artifact.image_url));
  if (!response.ok) throw new Error("panorama_image_unavailable");
  const objectUrl = URL.createObjectURL(await response.blob());
  try {
    const image = new Image();
    image.src = objectUrl;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.naturalWidth * crop.u_width));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * crop.v_height));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("panorama_canvas_unavailable");
    let destination = 0;
    for (const segment of panoramaCropSegments(crop, 0)) {
      const destinationWidth = segment.width / crop.u_width * canvas.width;
      context.drawImage(image, segment.left * image.naturalWidth, crop.v_start * image.naturalHeight, segment.width * image.naturalWidth, crop.v_height * image.naturalHeight, destination, 0, destinationWidth, canvas.height);
      destination += destinationWidth;
    }
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("panorama_export_failed")), "image/png"));
  } finally { URL.revokeObjectURL(objectUrl); }
}

type Gesture = { pointerId: number; handle: PanoramaCropHandle | "draw"; start: PanoramaPoint; original: CameraSourcePanoramaCrop; moved: boolean };
const HANDLES: Array<{ key: Exclude<PanoramaCropHandle, "move">; horizontal: number; vertical: number }> = [
  { key: "nw", horizontal: 0, vertical: 0 }, { key: "n", horizontal: 0.5, vertical: 0 }, { key: "ne", horizontal: 1, vertical: 0 },
  { key: "w", horizontal: 0, vertical: 0.5 }, { key: "e", horizontal: 1, vertical: 0.5 },
  { key: "sw", horizontal: 0, vertical: 1 }, { key: "s", horizontal: 0.5, vertical: 1 }, { key: "se", horizontal: 1, vertical: 1 },
];

export function PanoramaCropEditor({ ui, artifact, busy, saveDisabled = false, contextLabel, feedback, text, locale, onSave, onClose }: {
  ui: HostUi;
  artifact: CameraSourcePanoramaArtifact;
  busy: boolean;
  saveDisabled?: boolean;
  contextLabel: string;
  feedback?: React.ReactNode;
  text: PanoramaTranslate;
  locale: string;
  onSave: (crop: CameraSourcePanoramaCrop) => Promise<boolean>;
  onClose: () => void;
}): React.ReactElement {
  const initialCrop = validPanoramaCrop(artifact.crop) ? artifact.crop : FULL_PANORAMA_CROP;
  const [crop, setCrop] = useState<CameraSourcePanoramaCrop>(initialCrop);
  const cropRef = useRef(crop);
  const [seamOffset, setSeamOffset] = useState(() => panoramaCropSeamOffset(initialCrop));
  const [history, setHistory] = useState<CameraSourcePanoramaCrop[]>([]);
  const [drawMode, setDrawMode] = useState(true);
  const [firstCorner, setFirstCorner] = useState<PanoramaPoint | null>(null);
  const [zoom, setZoom] = useState(1);
  const [navigating, setNavigating] = useState(false);
  const navigation = useRef<NavigableViewportController | null>(null);
  const [scale, setScale] = useState(1);
  const [imageError, setImageError] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const continueButton = useRef<HTMLButtonElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const dirty = !samePanoramaCrop(crop, initialCrop);

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    element.showModal();
    element.querySelector<HTMLButtonElement>("[data-panorama-initial-focus]")?.focus();
    return () => { if (element.open) element.close(); };
  }, []);

  useEffect(() => { if (confirmClose) continueButton.current?.focus(); }, [confirmClose]);

  function requestClose() {
    if (busy) return;
    if (dirty) setConfirmClose(true);
    else onClose();
  }

  function update(next: CameraSourcePanoramaCrop, remember = true) {
    if (remember && !samePanoramaCrop(cropRef.current, next)) setHistory((previous) => [...previous.slice(-29), cropRef.current]);
    cropRef.current = next;
    setCrop(next);
    setConfirmClose(false);
  }

  function point(event: React.PointerEvent): PanoramaPoint {
    const rectangle = stage.current!.getBoundingClientRect();
    return { x: Math.max(0, Math.min(1, (event.clientX - rectangle.left) / rectangle.width)), y: Math.max(0, Math.min(1, (event.clientY - rectangle.top) / rectangle.height)) };
  }

  function begin(event: React.PointerEvent, handle: Gesture["handle"]) {
    if (busy || imageError || navigating || event.button !== 0 || !stage.current) return;
    event.preventDefault();
    event.stopPropagation();
    gesture.current = { pointerId: event.pointerId, handle, start: point(event), original: cropRef.current, moved: false };
    stage.current.setPointerCapture(event.pointerId);
  }

  function move(event: React.PointerEvent) {
    const current = gesture.current;
    if (!current || current.pointerId !== event.pointerId || !stage.current) return;
    const target = point(event);
    const deltaX = target.x - current.start.x;
    const deltaY = target.y - current.start.y;
    const rectangle = stage.current.getBoundingClientRect();
    if (Math.hypot(deltaX * rectangle.width, deltaY * rectangle.height) > 4) current.moved = true;
    if (!current.moved) return;
    update(current.handle === "draw"
      ? panoramaCropFromCorners(current.start, target, seamOffset)
      : adjustPanoramaCrop(current.original, current.handle, deltaX, deltaY), false);
  }

  function finish(event: React.PointerEvent, cancelled = false) {
    const current = gesture.current;
    if (!current || current.pointerId !== event.pointerId) return;
    gesture.current = null;
    if (stage.current?.hasPointerCapture(event.pointerId)) stage.current.releasePointerCapture(event.pointerId);
    if (cancelled) { update(current.original, false); return; }
    if (current.handle === "draw" && !current.moved) {
      if (!firstCorner) setFirstCorner(current.start);
      else {
        update(panoramaCropFromCorners(firstCorner, point(event), seamOffset));
        setFirstCorner(null);
        setDrawMode(false);
      }
    } else if (current.moved) {
      setHistory((previous) => [...previous.slice(-29), current.original]);
      setFirstCorner(null);
      setDrawMode(false);
    }
  }

  function cancelGesture() {
    const current = gesture.current;
    gesture.current = null;
    if (current) {
      update(current.original, false);
      if (stage.current?.hasPointerCapture(current.pointerId)) stage.current.releasePointerCapture(current.pointerId);
    }
    setFirstCorner(null);
  }

  function adjust(handle: PanoramaCropHandle, horizontal: number, vertical: number) {
    update(adjustPanoramaCrop(cropRef.current, handle, horizontal, vertical));
    setDrawMode(false);
    setFirstCorner(null);
  }

  function key(event: React.KeyboardEvent, handle: PanoramaCropHandle) {
    if (busy || imageError) return;
    if (event.key === "Escape") { event.preventDefault(); setFirstCorner(null); setDrawMode(false); return; }
    const delta = event.shiftKey ? 0.025 : 0.005;
    const horizontal = event.key === "ArrowLeft" ? -delta : event.key === "ArrowRight" ? delta : 0;
    const vertical = event.key === "ArrowUp" ? -delta : event.key === "ArrowDown" ? delta : 0;
    if (!horizontal && !vertical) return;
    event.preventDefault();
    event.stopPropagation();
    adjust(handle, horizontal, vertical);
  }

  const segments = panoramaCropSegments(crop, seamOffset);
  const displayedStart = crop.u_width === 1 ? 0 : wrapPanoramaCoordinate(crop.u_start - seamOffset);
  const percentage = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }).format;
  const selectionLabel = text("selection_description", { width: percentage(crop.u_width), height: percentage(crop.v_height) });
  return createPortal(<dialog ref={dialog} className="sourcePanorama sourcePanoramaDialog" data-testid="panorama-crop-dialog" aria-label={`${contextLabel} · ${text("crop_title")}`} onCancel={(event) => { event.preventDefault(); requestClose(); }} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); requestClose(); } }}>
    <section className="sourcePanoramaCrop" data-testid="panorama-crop-editor" aria-label={text("crop_title")}>
    <div className="sourcePanoramaDialogHeader"><strong>{contextLabel}</strong><button type="button" className="iconButton" aria-label={text("close_editor")} disabled={busy} onClick={requestClose}>×</button></div>
    <div className="sourcePanoramaEditorContent">
    {feedback}
    <div className="sourcePanoramaHeading"><div><h3>{text("crop_title")}</h3><p>{text(firstCorner ? "second_corner" : "crop_help")}</p></div></div>
    <div className="sourcePanoramaActions" role="toolbar" aria-label={text("image_tools")}>
      <button className="chipButton" type="button" data-panorama-initial-focus disabled={busy || imageError} aria-pressed={drawMode && !navigating} onClick={() => { setNavigating(false); setDrawMode(true); setFirstCorner(null); }}>{text("draw_area")}</button>
      <button className="chipButton" type="button" disabled={busy || imageError} aria-pressed={navigating} onClick={() => { cancelGesture(); setNavigating((value) => !value); }}>{text("move_view")}</button>
      <button className="chipButton" type="button" disabled={busy || !history.length} onClick={() => { const previous = history.at(-1); if (previous) { update(previous, false); setHistory((values) => values.slice(0, -1)); setFirstCorner(null); } }}>{text("undo")}</button>
      <button className="chipButton" type="button" disabled={busy || imageError} onClick={() => { update(FULL_PANORAMA_CROP); setSeamOffset(0); setFirstCorner(null); }}>{text("whole_image")}</button>
      <button className="iconButton" type="button" aria-label={text("image_zoom_out")} disabled={zoom <= 0.5} onClick={() => { cancelGesture(); navigation.current?.zoomBy(1 / 1.25); }}>−</button>
      <button className="iconButton" type="button" aria-label={text("image_zoom_in")} disabled={zoom >= 16} onClick={() => { cancelGesture(); navigation.current?.zoomBy(1.25); }}>+</button>
      <button className="chipButton" type="button" onClick={() => { cancelGesture(); navigation.current?.fit(); }}>{text("fit_view")}</button>
    </div>
    {imageError ? <p className="sourcePanoramaError" role="alert">{text("image_failed")}</p> : null}
    <ui.NavigableViewport className="sourcePanoramaCropViewport" label={text("image_tools")} contentSize={{ width: artifact.width, height: artifact.height }}
      controllerRef={navigation} interactionMode={navigating ? "navigate" : "interact"} onNavigationStart={cancelGesture}
      onViewChange={(state) => { setZoom(state.zoom); setScale(state.scale); }}>
      <div className={`sourcePanoramaCropStage${drawMode ? " isDrawing" : ""}`} ref={stage} style={{ width: "100%", height: "100%", aspectRatio: artifact.width / artifact.height }} role="group" aria-label={selectionLabel} tabIndex={0} onKeyDown={(event) => key(event, "move")} onPointerDown={(event) => begin(event, "draw")} onPointerMove={move} onPointerUp={(event) => finish(event)} onPointerCancel={(event) => finish(event, true)} onLostPointerCapture={(event) => finish(event, true)}>
        <div className="sourcePanoramaStrip" style={{ transform: `translateX(${-seamOffset * 50}%)` }}>{[0, 1].map((copy) => <img key={copy} src={resolveToposyncUrl(artifact.image_url)} alt="" aria-hidden="true" draggable={false} onError={() => setImageError(true)} />)}</div>
        <svg className="sourcePanoramaCropMask" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
          <path fillRule="evenodd" d={`M0 0H1000V1000H0Z ${segments.map((segment) => `M${segment.left * 1000} ${crop.v_start * 1000}h${segment.width * 1000}v${crop.v_height * 1000}h${-segment.width * 1000}Z`).join(" ")}`} />
        </svg>
        {segments.map((segment, index) => <div key={index} className="sourcePanoramaSelection" style={{ left: `${segment.left * 100}%`, top: `${crop.v_start * 100}%`, width: `${segment.width * 100}%`, height: `${crop.v_height * 100}%`, cursor: drawMode ? "crosshair" : "move" }} onPointerDown={(event) => begin(event, drawMode ? "draw" : "move")} />)}
        {!drawMode && !navigating && HANDLES.map((handle) => {
          const unwrapped = displayedStart + crop.u_width * handle.horizontal;
          const horizontal = unwrapped > 1 ? wrapPanoramaCoordinate(unwrapped) : unwrapped;
          return <button key={handle.key} type="button" className="sourcePanoramaHandle" style={{ left: `clamp(${22 / scale}px, ${horizontal * 100}%, calc(100% - ${22 / scale}px))`, top: `clamp(${22 / scale}px, ${(crop.v_start + crop.v_height * handle.vertical) * 100}%, calc(100% - ${22 / scale}px))`, cursor: `${handle.key}-resize` }} aria-label={text(`handle_${handle.key}`)} disabled={busy} onPointerDown={(event) => begin(event, handle.key)} onKeyDown={(event) => key(event, handle.key)} />;
        })}
        {firstCorner ? <span className="sourcePanoramaFirstCorner" style={{ left: `${firstCorner.x * 100}%`, top: `${firstCorner.y * 100}%` }} aria-hidden="true" /> : null}
      </div>
    </ui.NavigableViewport>
    <div className="sourcePanoramaActions"><span className="cardMeta">{text("seam_help")}</span><button type="button" className="iconButton" aria-label={text("seam_left")} onClick={() => { setSeamOffset((value) => wrapPanoramaCoordinate(value + 0.125)); setFirstCorner(null); }}>←</button><button type="button" className="iconButton" aria-label={text("seam_right")} onClick={() => { setSeamOffset((value) => wrapPanoramaCoordinate(value - 0.125)); setFirstCorner(null); }}>→</button></div>
    <details><summary>{text("adjust_selection")}</summary><p className="cardMeta">{text("keyboard_help")}</p><div className="sourcePanoramaActions" role="group" aria-label={text("move_selection")}>
      {([[-0.01, 0, "left", "←"], [0.01, 0, "right", "→"], [0, -0.01, "up", "↑"], [0, 0.01, "down", "↓"]] as const).map(([horizontal, vertical, direction, symbol]) => <button type="button" className="iconButton" key={direction} disabled={busy || imageError} aria-label={text(`move_${direction}`)} onClick={() => adjust("move", horizontal, vertical)}>{symbol}</button>)}
      <button type="button" className="chipButton" disabled={busy || imageError} onClick={() => adjust("e", -0.01, 0)}>{text("narrower")}</button><button type="button" className="chipButton" disabled={busy || imageError} onClick={() => adjust("e", 0.01, 0)}>{text("wider")}</button>
      <button type="button" className="chipButton" disabled={busy || imageError} onClick={() => adjust("s", 0, -0.01)}>{text("shorter")}</button><button type="button" className="chipButton" disabled={busy || imageError} onClick={() => adjust("s", 0, 0.01)}>{text("taller")}</button>
    </div></details>
    </div>
    <div className="sourcePanoramaFooter sourcePanoramaEditorFooter"><p className="cardMeta">{text("crop_non_destructive")}</p><div className="sourcePanoramaActions"><button type="button" className="chipButton" disabled={busy} onClick={requestClose}>{text("close_editor")}</button><button type="button" className="primaryButton" disabled={busy || saveDisabled || imageError || Boolean(firstCorner)} onClick={() => void onSave(cropRef.current)}>{text(busy ? "saving_crop" : "save_crop")}</button></div></div>
    {confirmClose ? <div className="sourcePanoramaNotice sourcePanoramaCloseNotice" role="status"><p>{text("unsaved_crop")}</p><button ref={continueButton} type="button" className="chipButton" onClick={() => setConfirmClose(false)}>{text("continue_editing")}</button><button type="button" className="chipButton" onClick={onClose}>{text("discard_crop")}</button></div> : null}
    </section>
  </dialog>, document.body);
}
