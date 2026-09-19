import React, { useLayoutEffect, useRef, useState } from "react";
import type { NavigableViewportController, NavigableViewportProps, NavigableViewportState } from "@toposync/plugin-api";
import { fitNavigation, navigationToContent, navigationToScreen, panNavigation, wheelNavigationScale, zoomNavigation } from "./viewportNavigation";
import type { NavigationBounds, NavigationPoint, NavigationSize, NavigationView } from "./viewportNavigation";

type Pan = { kind: "pan"; pointer: number; start: NavigationPoint; view: NavigationView; moved: boolean; clickable: boolean };
type Pinch = { kind: "pinch"; distance: number; midpoint: NavigationPoint; view: NavigationView };
const midpoint = (a: NavigationPoint, b: NavigationPoint): NavigationPoint => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
const distance = (a: NavigationPoint, b: NavigationPoint) => Math.hypot(a.x - b.x, a.y - b.y);

/** Shared DOM navigation surface. Editors retain ownership of their content/tools. */
export function NavigableViewport(props: NavigableViewportProps): React.ReactElement {
  const latest = useRef(props);
  latest.current = props;
  const element = useRef<HTMLDivElement | null>(null);
  const size = useRef<NavigationSize>({ width: 1, height: 1 });
  const view = useRef<NavigationView>({ center: { x: 0, y: 0 }, scale: 1 });
  const [state, setState] = useState<NavigableViewportState>({ ...view.current, zoom: 1 });
  const [measured, setMeasured] = useState(false);
  const fitted = useRef(false);
  const fittedBounds = useRef<NavigationBounds | undefined>(undefined);
  const gesture = useRef<Pan | Pinch | null>(null);
  const touches = useRef(new Map<number, NavigationPoint>());
  const pressed = useRef(new Set<number>());
  const suppressClick = useRef(false);
  const space = useRef(false);

  function fullBounds() {
    const content = latest.current.contentSize;
    return { x: 0, y: 0, width: Math.max(1, content.width), height: Math.max(1, content.height) };
  }
  function baseScale() { return fitNavigation(fullBounds(), size.current).scale; }
  function clampScale(scale: number) {
    return Math.max(baseScale() * (latest.current.minZoom ?? 0.5), Math.min(baseScale() * (latest.current.maxZoom ?? 16), scale));
  }
  function publish(next: NavigationView) {
    if (!Number.isFinite(next.scale) || next.scale <= 0) return;
    view.current = next;
    const value = { ...next, zoom: next.scale / baseScale() };
    setState(value);
    latest.current.onViewChange?.(value);
  }
  const controller = useRef<NavigableViewportController | null>(null);
  // Methods read current props/state through refs; callers can safely retain the controller.
  if (!controller.current) controller.current = {
    fit: (bounds) => { fitted.current = true; fittedBounds.current = bounds; const next = fitNavigation(bounds ?? latest.current.initialBounds ?? fullBounds(), size.current); publish({ ...next, scale: clampScale(next.scale) }); },
    zoomBy: (factor) => { fitted.current = false; publish(zoomNavigation(view.current, clampScale(view.current.scale * factor), { x: size.current.width / 2, y: size.current.height / 2 }, size.current)); },
    centerOn: (center) => { fitted.current = false; publish({ ...view.current, center }); },
    contentToScreen: (point) => navigationToScreen(point, view.current, size.current),
    screenToContent: (point) => navigationToContent(point, view.current, size.current),
  };

  useLayoutEffect(() => {
    const current = element.current;
    if (!current) return;
    let initialized = false;
    function resize() {
      if (!current || !current.clientWidth || !current.clientHeight) return;
      size.current = { width: current.clientWidth, height: current.clientHeight };
      if (!initialized) controller.current!.fit();
      else if (fitted.current) controller.current!.fit(fittedBounds.current);
      else publish(view.current); // Keep the inspected content point and absolute magnification.
      initialized = true;
      setMeasured(true);
    }
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(current);
    return () => observer.disconnect();
  }, [props.contentKey, props.contentSize.width, props.contentSize.height]);

  useLayoutEffect(() => {
    if (props.controllerRef) props.controllerRef.current = controller.current;
    return () => { if (props.controllerRef) props.controllerRef.current = null; };
  }, [props.controllerRef]);

  useLayoutEffect(() => {
    const current = element.current;
    if (!current) return;
    const wheel = (event: WheelEvent) => {
      if (event.target instanceof Element && event.target.closest("input,select,textarea,[data-viewport-control]")) return;
      event.preventDefault();
      event.stopPropagation();
      if (pressed.current.size) return; // A wheel must not change coordinates underneath an editing gesture.
      latest.current.onNavigationStart?.();
      const rectangle = current.getBoundingClientRect();
      const anchor = { x: event.clientX - rectangle.left, y: event.clientY - rectangle.top };
      const scale = wheelNavigationScale(view.current.scale, event.deltaY, event.deltaMode, size.current.height, baseScale() * (latest.current.minZoom ?? 0.5), baseScale() * (latest.current.maxZoom ?? 16));
      fitted.current = false;
      publish(zoomNavigation(view.current, scale, anchor, size.current));
    };
    const releaseSpace = (event: KeyboardEvent) => { if (event.code === "Space") space.current = false; };
    const releasePointer = (event: PointerEvent) => { pressed.current.delete(event.pointerId); };
    const cancel = () => { gesture.current = null; touches.current.clear(); pressed.current.clear(); space.current = false; latest.current.onNavigationStart?.(); };
    current.addEventListener("wheel", wheel, { passive: false });
    window.addEventListener("keyup", releaseSpace);
    window.addEventListener("pointerup", releasePointer, true);
    window.addEventListener("pointercancel", releasePointer, true);
    window.addEventListener("blur", cancel);
    return () => { current.removeEventListener("wheel", wheel); window.removeEventListener("keyup", releaseSpace); window.removeEventListener("pointerup", releasePointer, true); window.removeEventListener("pointercancel", releasePointer, true); window.removeEventListener("blur", cancel); };
  }, []);

  function point(event: React.PointerEvent): NavigationPoint {
    const rectangle = element.current!.getBoundingClientRect();
    return { x: event.clientX - rectangle.left, y: event.clientY - rectangle.top };
  }
  function take(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault(); event.stopPropagation();
    element.current!.setPointerCapture(event.pointerId);
  }
  function down(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button > 2) return;
    suppressClick.current = false;
    const position = point(event);
    const control = event.button === 0 && (event.target as Element).closest("button,input,select,textarea,a,[data-viewport-control]");
    pressed.current.add(event.pointerId);
    if (control && event.pointerType !== "touch") return;
    if (event.pointerType === "touch") touches.current.set(event.pointerId, position);
    if (touches.current.size >= 2) {
      latest.current.onNavigationStart?.();
      const [first, second] = [...touches.current.values()];
      gesture.current = { kind: "pinch", midpoint: midpoint(first, second), distance: Math.max(1, distance(first, second)), view: view.current };
      for (const pointer of touches.current.keys()) element.current!.setPointerCapture(pointer);
      take(event); return;
    }
    if (control) return;
    if (gesture.current) return;
    if (latest.current.interactionMode === "interact" && event.button === 0 && !space.current) return;
    latest.current.onNavigationStart?.();
    gesture.current = { kind: "pan", pointer: event.pointerId, start: position, view: view.current, moved: false, clickable: event.button === 0 && !space.current };
    element.current!.focus({ preventScroll: true });
    take(event);
  }
  function move(event: React.PointerEvent<HTMLDivElement>) {
    const position = point(event);
    if (touches.current.has(event.pointerId)) touches.current.set(event.pointerId, position);
    const current = gesture.current;
    if (!current) { latest.current.onContentPointerMove?.(navigationToContent(position, view.current, size.current), event); return; }
    if (current.kind === "pinch") {
      const [first, second] = [...touches.current.values()];
      if (!first || !second) return;
      const center = midpoint(first, second);
      const next = zoomNavigation(current.view, clampScale(current.view.scale * distance(first, second) / current.distance), current.midpoint, size.current);
      fitted.current = false;
      publish(panNavigation(next, { x: center.x - current.midpoint.x, y: center.y - current.midpoint.y }));
      take(event); return;
    }
    if (current.pointer !== event.pointerId) return;
    if (!current.moved && distance(position, current.start) >= 3) current.moved = true;
    if (current.moved) {
      fitted.current = false;
      publish(panNavigation(current.view, { x: position.x - current.start.x, y: position.y - current.start.y }));
    }
    take(event);
  }
  function end(event: React.PointerEvent<HTMLDivElement>, cancelled = false) {
    pressed.current.delete(event.pointerId);
    touches.current.delete(event.pointerId);
    const current = gesture.current;
    if (!current || current.kind === "pan" && current.pointer !== event.pointerId) return;
    gesture.current = null;
    suppressClick.current = cancelled || current.kind === "pinch" || current.moved;
    if (!cancelled && current.kind === "pan" && current.clickable && !current.moved && distance(point(event), current.start) < 3) {
      latest.current.onContentClick?.(navigationToContent(point(event), view.current, size.current), event);
    }
    if (current.kind === "pinch" && touches.current.size) {
      const [pointer, start] = [...touches.current.entries()][0];
      gesture.current = { kind: "pan", pointer, start, view: view.current, moved: true, clickable: false };
    }
    event.preventDefault(); event.stopPropagation();
    if (element.current!.hasPointerCapture(event.pointerId)) element.current!.releasePointerCapture(event.pointerId);
  }
  function key(event: React.KeyboardEvent<HTMLDivElement>) {
    props.onKeyDown?.(event);
    if (event.defaultPrevented || event.target !== event.currentTarget) return;
    if (event.code === "Space") { event.preventDefault(); space.current = true; return; }
    const delta = event.shiftKey ? 100 : 40;
    const direction = ({ ArrowLeft: { x: delta, y: 0 }, ArrowRight: { x: -delta, y: 0 }, ArrowUp: { x: 0, y: delta }, ArrowDown: { x: 0, y: -delta } } as Record<string, NavigationPoint>)[event.key];
    if (!["+", "=", "-", "0", "Home"].includes(event.key) && !direction) return;
    event.preventDefault();
    props.onNavigationStart?.();
    if (direction) { fitted.current = false; publish(panNavigation(view.current, direction)); }
    else if (event.key === "0" || event.key === "Home") controller.current!.fit();
    else controller.current!.zoomBy(event.key === "-" ? 1 / 1.25 : 1.25);
  }
  const translation = navigationToScreen({ x: 0, y: 0 }, state, size.current);
  return <div ref={(node) => { element.current = node; const external = props.viewportRef; if (typeof external === "function") external(node); else if (external) (external as React.MutableRefObject<HTMLDivElement | null>).current = node; }}
    className={props.className} role="group" aria-label={props.label} tabIndex={0} data-navigable-viewport="" data-viewport-zoom={state.zoom}
    style={{ cursor: props.interactionMode === "interact" ? undefined : gesture.current ? "grabbing" : "grab", ...props.style, position: "relative", overflow: "clip", touchAction: "none", userSelect: "none" }}
    onPointerDownCapture={down} onPointerMoveCapture={move} onPointerUpCapture={(event) => end(event)} onPointerCancelCapture={(event) => end(event, true)}
    onLostPointerCapture={(event) => { if (event.target === element.current && gesture.current?.kind === "pan" && gesture.current.pointer === event.pointerId) end(event, true); }}
    onClickCapture={(event) => { if (suppressClick.current) { event.preventDefault(); event.stopPropagation(); suppressClick.current = false; } }}
    onContextMenu={(event) => event.preventDefault()} onKeyDown={key} onPointerLeave={props.onPointerLeave} onBlur={() => { space.current = false; props.onBlur?.(); }}>
    <div data-viewport-content="" style={{ position: "absolute", width: props.contentSize.width, height: props.contentSize.height, transformOrigin: "0 0", transform: `translate(${translation.x}px, ${translation.y}px) scale(${state.scale})`, visibility: measured ? "visible" : "hidden", "--viewport-inverse-scale": 1 / state.scale } as React.CSSProperties}>
      {typeof props.children === "function" ? props.children(state) : props.children}
    </div>
  </div>;
}
