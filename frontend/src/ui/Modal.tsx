import React, { useEffect } from "react";
import { createPortal } from "react-dom";

import { i18n } from "../util/i18n";
import { Icon } from "./Icon";

type Props = {
  open: boolean;
  title: string;
  children: React.ReactNode;
  onClose: () => void;
  panelClassName?: string;
  panelStyle?: React.CSSProperties;
  bodyClassName?: string;
  bodyStyle?: React.CSSProperties;
};

export function Modal({
  open,
  title,
  children,
  onClose,
  panelClassName,
  panelStyle,
  bodyClassName,
  bodyStyle,
}: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();

  useEffect(() => {
    if (!open) return;

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;
  if (typeof document === "undefined") return null;

  const stopPortalEventPropagation = (event: React.SyntheticEvent) => {
    event.stopPropagation();
  };

  return createPortal(
    <div
      className="modalBackdrop"
      onPointerDown={stopPortalEventPropagation}
      onPointerMove={stopPortalEventPropagation}
      onPointerUp={stopPortalEventPropagation}
      onPointerCancel={stopPortalEventPropagation}
      onClick={stopPortalEventPropagation}
      onDoubleClick={stopPortalEventPropagation}
      onWheel={stopPortalEventPropagation}
      onMouseDown={(e) => {
        e.stopPropagation();
        if (e.target === e.currentTarget) onClose();
      }}
      role="presentation"
    >
      <div
        className={["modalPanel", panelClassName].filter(Boolean).join(" ")}
        style={panelStyle}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modalHeader">
          <div className="modalTitle">{title}</div>
          <button className="iconButton" type="button" onClick={onClose} aria-label={t("core.modal.aria.close")}>
            <Icon name="xmark" />
          </button>
        </div>
        <div className={["modalBody", bodyClassName].filter(Boolean).join(" ")} style={bodyStyle}>
          {children}
        </div>
      </div>
    </div>,
    document.body,
  );
}
