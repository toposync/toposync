import React, { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function SubModal({
  title,
  closeLabel,
  open,
  busy = false,
  onClose,
  children,
}: {
  title: string;
  closeLabel: string;
  open: boolean;
  busy?: boolean;
  onClose: () => void;
  children: React.ReactNode;
}): React.ReactElement | null {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const busyRef = useRef(busy);

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>(FOCUSABLE);
    window.setTimeout(() => (first ?? panel)?.focus(), 0);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => !element.hidden && element.getAttribute("aria-hidden") !== "true",
      );
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const firstFocusable = focusable[0];
      const lastFocusable = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === firstFocusable) {
        event.preventDefault();
        lastFocusable.focus();
      } else if (!event.shiftKey && document.activeElement === lastFocusable) {
        event.preventDefault();
        firstFocusable.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      previousFocus?.focus();
    };
  }, [onClose, open]);

  if (!open || typeof document === "undefined") return null;

  const stopPortalEventPropagation = (event: React.SyntheticEvent) => {
    event.stopPropagation();
  };

  return createPortal(
    <div
      className="modalBackdrop"
      style={{ zIndex: "calc(var(--z-modal) + 1)" }}
      onPointerDown={stopPortalEventPropagation}
      onPointerMove={stopPortalEventPropagation}
      onPointerUp={stopPortalEventPropagation}
      onPointerCancel={stopPortalEventPropagation}
      onClick={stopPortalEventPropagation}
      onDoubleClick={stopPortalEventPropagation}
      onWheel={stopPortalEventPropagation}
      onMouseDown={(event) => {
        event.stopPropagation();
        if (event.target === event.currentTarget && !busy) onClose();
      }}
      role="presentation"
    >
      <div
        ref={panelRef}
        className="modalPanel ptzAttentionWizard"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="modalHeader">
          <h2 className="modalTitle" id={titleId}>{title}</h2>
          <button className="iconButton" type="button" disabled={busy} onClick={onClose} aria-label={closeLabel}>
            <i className="fa-solid fa-xmark" aria-hidden="true" />
          </button>
        </div>
        <div className="modalBody">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
