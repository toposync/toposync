import React from "react";
import { createPortal } from "react-dom";

export function SubModal({
  title,
  open,
  onClose,
  closeAriaLabel,
  children,
  panelStyle,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  closeAriaLabel?: string;
  children: React.ReactNode;
  panelStyle?: React.CSSProperties;
}): React.ReactElement | null {
  if (!open) return null;

  const resolvedCloseAriaLabel = String(closeAriaLabel || "").trim() || "Close";

  return createPortal(
    <div
      className="modalBackdrop"
      style={{ zIndex: "calc(var(--z-modal) + 1)" }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <div
        className="modalPanel"
        style={{ width: "min(920px, calc(100vw - 28px))", ...(panelStyle ?? {}) }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modalHeader">
          <div className="modalTitle">{title}</div>
          <button className="iconButton" type="button" onClick={onClose} aria-label={resolvedCloseAriaLabel}>
            <i className="fa-solid fa-xmark" aria-hidden="true" />
          </button>
        </div>
        <div className="modalBody">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
