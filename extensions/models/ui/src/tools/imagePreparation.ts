import { createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { resolveToposyncUrl, type HostI18n, type HostUi, type NavigableViewportController } from "@toposync/plugin-api";
import type { ObjectContour } from "./objectContour";

export async function prepareObjectImage(file: File, signal: AbortSignal, contour?: ObjectContour) {
  const form = new FormData();
  form.append("file", file);
  if (contour) form.append("foreground_polygon", JSON.stringify(contour));
  const response = await fetch(resolveToposyncUrl("/api/models/prepare-image"), { method: "POST", body: form, signal });
  signal.throwIfAborted();
  if (!response.ok) {
    const failure = await response.json().catch(() => null);
    throw new Error(typeof failure?.detail === "string" ? failure.detail : "invalid_prepared_image");
  }
  const digest = response.headers.get("X-Toposync-Prepared-Sha256") ?? "";
  if (response.headers.get("content-type") !== "image/png" || !/^[0-9a-f]{64}$/.test(digest)) {
    throw new Error("invalid_prepared_image");
  }
  const image = await response.blob();
  signal.throwIfAborted();
  if (!image.size || image.size > 8 * 1024 ** 2) throw new Error("generation_image_too_large");
  return { image, digest };
}

export function reviewObjectImage(
  original: File, prepared: Blob, t: HostI18n["t"], signal: AbortSignal, cancel: () => void,
  ui: HostUi, contour?: ObjectContour,
): Promise<"generate" | "replace" | "adjust"> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    let settled = false;
    const urls = [URL.createObjectURL(original), URL.createObjectURL(prepared)];
    const roots: Root[] = [];
    const navigation = urls.map(() => ({ current: null as NavigableViewportController | null }));
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", t("ext.models.prepare.title"));
    dialog.style.cssText = "width:min(760px,calc(100vw - 28px));max-height:90dvh;overflow:hidden;box-sizing:border-box;padding:24px;border:1px solid var(--color-border-strong,#64748b);border-radius:18px;background:var(--color-surface-solid,#fff);color:var(--color-text-primary,#101418);flex-direction:column";
    const headingRow = document.createElement("div");
    headingRow.style.cssText = "display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-shrink:0";
    const title = document.createElement("h2");
    title.textContent = t("ext.models.prepare.title");
    title.style.cssText = "font-size:18px;line-height:1.4;margin:0";
    title.tabIndex = -1;
    const close = document.createElement("button");
    close.className = "iconButton";
    close.type = "button";
    close.textContent = "×";
    close.setAttribute("aria-label", t("ext.models.editor.cancel"));
    close.style.cssText = "min-width:44px;min-height:44px;flex-shrink:0";
    close.addEventListener("click", cancel);
    headingRow.append(title, close);
    const content = document.createElement("div");
    content.style.cssText = "min-height:0;overflow:auto;margin-top:12px;outline-offset:2px";
    content.tabIndex = 0;
    content.setAttribute("role", "region");
    content.setAttribute("aria-label", t("ext.models.prepare.comparison"));
    const hint = document.createElement("p");
    hint.textContent = t("ext.models.prepare.hint");
    hint.style.cssText = "margin:0 0 16px;line-height:1.5";
    const images = document.createElement("div");
    images.style.cssText = "display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));gap:16px";
    const pictures = urls.map((url, index) => {
      const figure = document.createElement("figure");
      figure.style.cssText = "margin:0;min-width:0";
      const label = t(index ? "ext.models.prepare.cutout" : "ext.models.prepare.original");
      const caption = document.createElement("figcaption");
      caption.textContent = label;
      caption.style.cssText = "font-weight:600;margin-bottom:8px";
      const image = document.createElement("img");
      image.alt = label;
      image.style.cssText = "display:block;width:100%;height:min(20vh,200px);object-fit:contain;border-radius:12px;background:repeating-conic-gradient(#e2e8f0 0% 25%,#f8fafc 0% 50%) 50% / 24px 24px";
      image.src = url;
      figure.append(caption, image);
      images.append(figure);
      return image;
    });
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    status.style.cssText = "margin:16px 0 0;line-height:1.5";
    status.textContent = t("ext.models.source.loading");
    const inspection = document.createElement("div");
    inspection.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;margin-top:12px";
    inspection.setAttribute("role", "group");
    inspection.setAttribute("aria-label", t("ext.models.prepare.comparison"));
    for (const [label, text, action] of [
      ["ext.models.contour.zoom_out", "−", () => navigation.forEach(reference => reference.current?.zoomBy(1 / 1.5))],
      ["ext.models.contour.zoom_in", "+", () => navigation.forEach(reference => reference.current?.zoomBy(1.5))],
      ["ext.models.prepare.full_image", t("ext.models.prepare.full_image"), () => navigation.forEach((reference, index) => reference.current?.fit({ x: 0, y: 0, width: pictures[index].naturalWidth, height: pictures[index].naturalHeight }))],
    ] as const) {
      const button = document.createElement("button");
      button.type = "button"; button.className = text.length === 1 ? "iconButton" : "chipButton";
      button.style.cssText = "min-height:44px;min-width:44px";
      button.setAttribute("aria-label", t(label)); button.textContent = text;
      button.disabled = true; button.addEventListener("click", action); inspection.append(button);
    }
    const adjust = document.createElement("button");
    adjust.type = "button";
    adjust.className = "chipButton";
    adjust.style.cssText = "min-height:44px;margin-top:16px";
    adjust.textContent = t("ext.models.contour.adjust");
    adjust.disabled = true;
    adjust.addEventListener("click", () => finish("adjust"));
    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;flex-wrap:wrap;justify-content:flex-end;gap:12px;flex-shrink:0;margin-top:16px;padding-top:16px;border-top:1px solid var(--color-border-subtle,#64748b)";
    const replace = document.createElement("button"), generate = document.createElement("button");
    replace.type = generate.type = "button";
    replace.className = "chipButton";
    generate.className = "pillButton pillButtonPrimary";
    replace.style.minHeight = generate.style.minHeight = "44px";
    replace.textContent = t("ext.models.prepare.replace");
    generate.textContent = t("ext.models.prepare.generate");
    generate.disabled = true;
    replace.addEventListener("click", () => finish("replace"));
    generate.addEventListener("click", () => finish("generate"));
    actions.append(replace, generate);
    content.append(hint, images, inspection, adjust, status);
    dialog.append(headingRow, content, actions);
    function cleanup() {
      signal.removeEventListener("abort", abort);
      roots.forEach(root => root.unmount());
      dialog.close();
      dialog.remove();
      urls.forEach(url => URL.revokeObjectURL(url));
    }
    function finish(choice: "generate" | "replace" | "adjust") {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(choice);
    }
    function abort() {
      if (settled) return;
      settled = true;
      cleanup();
      reject(signal.reason ?? new DOMException("Cancelled", "AbortError"));
    }
    signal.addEventListener("abort", abort, { once: true });
    dialog.addEventListener("cancel", event => { event.preventDefault(); cancel(); });
    document.body.append(dialog);
    dialog.showModal();
    dialog.style.display = "flex";
    title.focus();
    void Promise.all(pictures.map(image => image.decode())).then(() => {
      if (settled) return;
      pictures.forEach((image, index) => {
        const container = document.createElement("div"), width = image.naturalWidth, height = image.naturalHeight;
        const root = createRoot(container); roots.push(root); image.replaceWith(container);
        const bounds = contour ? {
          x: Math.min(...contour.map(point => point[0])) * width,
          y: Math.min(...contour.map(point => point[1])) * height,
          width: (Math.max(...contour.map(point => point[0])) - Math.min(...contour.map(point => point[0]))) * width,
          height: (Math.max(...contour.map(point => point[1])) - Math.min(...contour.map(point => point[1]))) * height,
        } : undefined;
        if (bounds) { const margin = Math.max(bounds.width, bounds.height) * .1; bounds.x -= margin; bounds.y -= margin; bounds.width += margin * 2; bounds.height += margin * 2; }
        root.render(createElement(ui.NavigableViewport, {
          contentSize: { width, height }, initialBounds: bounds, controllerRef: navigation[index], label: image.alt,
          style: { height: "min(20vh,200px)", borderRadius: 12, background: "repeating-conic-gradient(#e2e8f0 0% 25%,#f8fafc 0% 50%) 50% / 24px 24px" },
          children: createElement("img", { src: urls[index], alt: image.alt, draggable: false, style: { width, height, display: "block", pointerEvents: "none" } }),
        }));
      });
      inspection.querySelectorAll("button").forEach(button => { button.disabled = false; });
      status.textContent = t("ext.models.prepare.limit");
      generate.disabled = false;
      adjust.disabled = false;
    }).catch(() => {
      if (!settled) status.textContent = t("ext.models.error.invalid_prepared_image");
    });
  });
}
