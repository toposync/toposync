import React from "react";
import { createPortal } from "react-dom";

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
  if (!open) return null;

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
