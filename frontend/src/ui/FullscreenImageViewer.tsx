import React, { useCallback, useEffect } from "react";
import { createPortal } from "react-dom";

import { i18n } from "../util/i18n";
import { Icon } from "./Icon";

export type FullscreenImageViewerItem = {
  id: string;
  url: string;
  label?: string;
  meta?: string;
};

type Props = {
  open: boolean;
  items: FullscreenImageViewerItem[];
  index: number;
  onIndexChange: (index: number) => void;
  onClose: () => void;
};

function getFullscreenElement(): Element | null {
  if (typeof document === "undefined") return null;
  const anyDoc = document as any;
  return anyDoc.fullscreenElement || anyDoc.webkitFullscreenElement || anyDoc.mozFullScreenElement || anyDoc.msFullscreenElement || null;
}

function exitFullscreenIfActive(): void {
  if (typeof document === "undefined" || !getFullscreenElement()) return;
  const anyDoc = document as any;
  const exit =
    typeof document.exitFullscreen === "function"
      ? document.exitFullscreen.bind(document)
      : typeof anyDoc.webkitExitFullscreen === "function"
        ? anyDoc.webkitExitFullscreen.bind(document)
        : typeof anyDoc.mozCancelFullScreen === "function"
          ? anyDoc.mozCancelFullScreen.bind(document)
          : typeof anyDoc.msExitFullscreen === "function"
            ? anyDoc.msExitFullscreen.bind(document)
            : null;
  try {
    const result = exit?.();
    if (result && typeof result.catch === "function") result.catch(() => undefined);
  } catch {
    // Fullscreen exit is best-effort.
  }
}

export function requestFullscreenImageViewer(target?: Element | null): void {
  if (typeof document === "undefined") return;
  const root = target ?? document.documentElement;
  const anyRoot = root as any;
  const request =
    typeof anyRoot.requestFullscreen === "function"
      ? anyRoot.requestFullscreen.bind(root)
      : typeof anyRoot.webkitRequestFullscreen === "function"
        ? anyRoot.webkitRequestFullscreen.bind(root)
        : typeof anyRoot.mozRequestFullScreen === "function"
          ? anyRoot.mozRequestFullScreen.bind(root)
          : typeof anyRoot.msRequestFullscreen === "function"
            ? anyRoot.msRequestFullscreen.bind(root)
            : null;
  if (!request) return;
  try {
    const result = request();
    if (result && typeof result.catch === "function") result.catch(() => undefined);
  } catch {
    // Opening the viewer still works when the browser denies fullscreen.
  }
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!target || !(target instanceof Element)) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || Boolean((target as HTMLElement).isContentEditable);
}

export function FullscreenImageViewer({ open, items, index, onIndexChange, onClose }: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const total = items.length;
  const currentIndex = total > 0 ? Math.max(0, Math.min(index, total - 1)) : 0;
  const activeItem = items[currentIndex] ?? null;
  const canNavigate = total > 1;

  const showPrev = useCallback(() => {
    if (!canNavigate) return;
    onIndexChange((currentIndex - 1 + total) % total);
  }, [canNavigate, currentIndex, onIndexChange, total]);

  const showNext = useCallback(() => {
    if (!canNavigate) return;
    onIndexChange((currentIndex + 1) % total);
  }, [canNavigate, currentIndex, onIndexChange, total]);

  const close = useCallback(() => {
    onClose();
    exitFullscreenIfActive();
  }, [onClose]);

  useEffect(() => {
    if (!open || index === currentIndex) return;
    onIndexChange(currentIndex);
  }, [currentIndex, index, onIndexChange, open]);

  useEffect(() => {
    if (!open) return;

    function onKeyDown(event: KeyboardEvent): void {
      if (isEditableTarget(event.target)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        event.stopPropagation();
        showPrev();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        event.stopPropagation();
        showNext();
      }
    }

    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [close, open, showNext, showPrev]);

  useEffect(() => {
    if (!open) return;

    function onFullscreenChange(): void {
      if (!getFullscreenElement()) onClose();
    }

    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange as EventListener);
    return () => {
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      document.removeEventListener("webkitfullscreenchange", onFullscreenChange as EventListener);
    };
  }, [onClose, open]);

  if (!open || !activeItem || typeof document === "undefined") return null;

  return createPortal(
    <div className="fullscreenImageViewerBackdrop" role="dialog" aria-modal="true" aria-label={t("core.ui.image_viewer.title", {}, "Image viewer")}>
      <div className="fullscreenImageViewerHeader">
        <div className="fullscreenImageViewerTitle">{activeItem.label || t("core.ui.image_viewer.title", {}, "Image viewer")}</div>
        <button
          className="iconButton fullscreenImageViewerButton"
          type="button"
          onClick={close}
          aria-label={t("core.ui.image_viewer.close", {}, "Close image viewer")}
          title={t("core.ui.image_viewer.close", {}, "Close image viewer")}
        >
          <Icon name="xmark" />
        </button>
      </div>

      <div className="fullscreenImageViewerStage">
        <button
          className="iconButton fullscreenImageViewerButton fullscreenImageViewerNav isPrev"
          type="button"
          onClick={showPrev}
          disabled={!canNavigate}
          aria-label={t("core.ui.image_viewer.previous", {}, "Previous image")}
          title={t("core.ui.image_viewer.previous", {}, "Previous image")}
        >
          <Icon name="chevron-left" />
        </button>

        <img className="fullscreenImageViewerImage" src={activeItem.url} alt={activeItem.label || ""} />

        <button
          className="iconButton fullscreenImageViewerButton fullscreenImageViewerNav isNext"
          type="button"
          onClick={showNext}
          disabled={!canNavigate}
          aria-label={t("core.ui.image_viewer.next", {}, "Next image")}
          title={t("core.ui.image_viewer.next", {}, "Next image")}
        >
          <Icon name="chevron-right" />
        </button>
      </div>

      <div className="fullscreenImageViewerFooter">
        <div className="fullscreenImageViewerCaption">
          {activeItem.meta ? <span>{activeItem.meta}</span> : null}
          <span className="fullscreenImageViewerCounter">
            {t("core.ui.image_viewer.counter", { current: currentIndex + 1, total }, `${currentIndex + 1} / ${total}`)}
          </span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
