import React, { useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { i18n } from "../util/i18n";
import { Icon } from "./Icon";
import { activateModalFocus } from "./modalFocus";

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
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useLayoutEffect(() => { onCloseRef.current = onClose; });

  useLayoutEffect(() => {
    if (!open || !panelRef.current) return;
    return activateModalFocus(panelRef.current, () => onCloseRef.current());
  }, [open]);

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
        ref={panelRef}
        tabIndex={-1}
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
