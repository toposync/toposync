import React from "react";
import { createPortal } from "react-dom";
import { activateModalFocus } from "@toposync/plugin-api";

export function SubModal({
  title,
  open,
  onClose,
  children,
  panelStyle,
  bodyStyle,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  panelStyle?: React.CSSProperties;
  bodyStyle?: React.CSSProperties;
}): React.ReactElement | null {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const onCloseRef = React.useRef(onClose);
  onCloseRef.current = onClose;
  React.useLayoutEffect(() => {
    // Older hosts do not expose the shared coordinator. Preserve their existing
    // modal behavior without introducing an independent competing focus trap.
    if (!open || !panelRef.current || typeof activateModalFocus !== "function") return;
    return activateModalFocus(panelRef.current, () => onCloseRef.current());
  }, [open]);
  if (!open) return null;

  // A submodal is rendered through a separate body portal. Keep its complete
  // pointer sequence inside that portal so the editor modal behind it cannot
  // interpret the interaction as a backdrop click.
  const stopPortalEventPropagation = (event: React.SyntheticEvent) => {
    event.stopPropagation();
  };

  return createPortal(
    <div
      className="modalBackdrop"
      // Do not use a custom-property calculation here: hosts that do not load
      // the token stylesheet discard it, placing this portal behind its owner.
      style={{ zIndex: 101 }}
      onPointerDown={stopPortalEventPropagation}
      onPointerMove={stopPortalEventPropagation}
      onPointerUp={stopPortalEventPropagation}
      onPointerCancel={stopPortalEventPropagation}
      onClick={stopPortalEventPropagation}
      onDoubleClick={stopPortalEventPropagation}
      onWheel={stopPortalEventPropagation}
      onMouseDown={(event) => {
        event.stopPropagation();
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        className="modalPanel"
        style={{ width: "min(980px, calc(100vw - 28px))", ...(panelStyle ?? {}) }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modalHeader">
          <div className="modalTitle">{title}</div>
          <button className="iconButton" type="button" onClick={onClose} aria-label="Close">
            <i className="fa-solid fa-xmark" aria-hidden="true" />
          </button>
        </div>
        <div className="modalBody" style={bodyStyle}>
          {children}
        </div>
      </div>
    </div>,
    document.body,
  );
}
