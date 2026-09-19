import React, { useLayoutEffect, useRef, useState } from 'react';
/** Session-local panel. Its coordinates never enter panorama or motor geometry. */
export function FloatingVideoPanel({ title, children, hidden = false, initialLeft = 16, initialTop = 70, initialWidth = 320, minimizable = false, onMinimizedChange }: {
    title: string;
    children: React.ReactNode;
    hidden?: boolean;
    initialLeft?: number;
    initialTop?: number;
    initialWidth?: number;
    minimizable?: boolean;
    onMinimizedChange?: (minimized: boolean) => void;
}) {
    const panel = useRef<HTMLDivElement | null>(null);
    const [box, setBox] = useState({ left: initialLeft, top: initialTop, width: initialWidth });
    const [minimized, setMinimized] = useState(false);
    useLayoutEffect(() => { if (panel.current)
        panel.current.inert = hidden; }, [hidden]);
    const bounds = useRef({ width: 800, height: 600 });
    const gesture = useRef<{
        id: number;
        x: number;
        y: number;
        box: typeof box;
        resize: boolean;
    } | null>(null);
    function constrain(value: typeof box) { const width = Math.min(Math.max(220, value.width), Math.max(220, bounds.current.width - 24)); return { width, left: Math.max(8, Math.min(value.left, bounds.current.width - width - 8)), top: Math.max(54, Math.min(value.top, bounds.current.height - (minimized ? 36 : width * 9 / 16 + 36) - 52)) }; }
    useLayoutEffect(() => { const parent = panel.current?.parentElement; if (!parent)
        return; const observer = new ResizeObserver(() => { bounds.current = { width: parent.clientWidth, height: parent.clientHeight }; setBox(previous => constrain(previous)); }); observer.observe(parent); return () => observer.disconnect(); }, [minimized]);
    function start(event: React.PointerEvent, resize = false) { if (event.button !== 0 || (event.target as Element).closest('button'))
        return; event.preventDefault(); event.stopPropagation(); gesture.current = { id: event.pointerId, x: event.clientX, y: event.clientY, box, resize }; event.currentTarget.setPointerCapture(event.pointerId); }
    function move(event: React.PointerEvent) { const current = gesture.current; if (!current || current.id !== event.pointerId)
        return; const dx = event.clientX - current.x, dy = event.clientY - current.y; const resizeDelta = Math.abs(dx) >= Math.abs(dy * 16 / 9) ? dx : dy * 16 / 9; setBox(constrain(current.resize ? { ...current.box, width: current.box.width + resizeDelta } : { ...current.box, left: current.box.left + dx, top: current.box.top + dy })); event.stopPropagation(); }
    function end() { gesture.current = null; }
    function toggleMinimized() { setMinimized(value => { const next = !value; onMinimizedChange?.(next); return next; }); }
    return <div ref={panel} className="livePanoramaFloatingPanel" data-viewport-control="" data-minimized={minimized} aria-label={title} aria-hidden={hidden} style={{ left: box.left, top: box.top, width: box.width, height: minimized ? 36 : box.width * 9 / 16 + 36, opacity: hidden ? 0 : 1, pointerEvents: hidden ? 'none' : 'auto' }}>
    <div className="livePanoramaFloatingHeader" tabIndex={0} aria-label={`Mover ${title}`} onPointerDown={event => start(event)} onPointerMove={move} onPointerUp={end} onPointerCancel={end} onKeyDown={event => { const d = event.shiftKey ? 30 : 10; const delta = ({ ArrowLeft: [-d, 0], ArrowRight: [d, 0], ArrowUp: [0, -d], ArrowDown: [0, d] } as Record<string, number[]>)[event.key]; if (delta) {
        event.preventDefault();
        setBox(constrain({ ...box, left: box.left + delta[0], top: box.top + delta[1] }));
    } }}>
      <span className="livePanoramaFloatingTitle">{title}</span>{minimizable && <button className="iconButton livePanoramaPanelButton" type="button" aria-label={minimized ? 'Restaurar teleobjetiva' : 'Minimizar teleobjetiva'} title={minimized ? 'Restaurar teleobjetiva' : 'Minimizar teleobjetiva'} onClick={toggleMinimized}><i className={`fa-solid fa-${minimized ? 'window-maximize' : 'minus'}`} aria-hidden="true" /></button>}
    </div>
    <div className="livePanoramaFloatingBody" style={{ height: box.width * 9 / 16, visibility: minimized ? 'hidden' : 'visible' }}>{children}</div>
    {!minimized && <div className="livePanoramaResizeHandle" role="slider" tabIndex={0} aria-label={`Redimensionar ${title}`} aria-valuenow={Math.round(box.width)} aria-valuemin={220} aria-valuemax={Math.max(220, bounds.current.width - 24)} onKeyDown={event => { if (['ArrowLeft', 'ArrowRight'].includes(event.key)) {
        event.preventDefault();
        setBox(constrain({ ...box, width: box.width + (event.key === 'ArrowRight' ? 20 : -20) }));
    } }} onPointerDown={event => start(event, true)} onPointerMove={move} onPointerUp={end} onPointerCancel={end}/>}
  </div>;
}
