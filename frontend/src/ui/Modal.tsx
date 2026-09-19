import React, { useEffect, useRef } from "react";
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
  manageFocus?: boolean;
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
  manageFocus = false,
}: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open || !manageFocus) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panelRef.current?.querySelector<HTMLElement>("button")?.focus();
    const trap = (event: KeyboardEvent) => {
      const panel = panelRef.current;
      if (event.key !== "Tab" || !panel || document.querySelectorAll('[role="dialog"]')[document.querySelectorAll('[role="dialog"]').length - 1] !== panel) return;
      const targets = Array.from(panel.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex="0"]')).filter((element) => element.getClientRects().length > 0);
      const first = targets[0], last = targets[targets.length - 1];
      if (!first) { event.preventDefault(); panel.focus(); }
      else if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => { document.removeEventListener("keydown", trap); if (previous?.isConnected) previous.focus(); };
  }, [open, manageFocus]);

  useEffect(() => {
    if (!open) return;

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") closeRef.current();
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
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
        className={["modalPanel", panelClassName].filter(Boolean).join(" ")}
        style={panelStyle}
        ref={panelRef}
        tabIndex={-1}
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
