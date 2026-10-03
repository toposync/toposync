import { resolveToposyncUrl, type HostI18n } from "@toposync/plugin-api";

export async function prepareObjectImage(file: File, signal: AbortSignal) {
  const form = new FormData();
  form.append("file", file);
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
): Promise<"generate" | "replace"> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    let settled = false;
    const urls = [URL.createObjectURL(original), URL.createObjectURL(prepared)];
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
    content.append(hint, images, status);
    dialog.append(headingRow, content, actions);
    function cleanup() {
      signal.removeEventListener("abort", abort);
      dialog.close();
      dialog.remove();
      urls.forEach(url => URL.revokeObjectURL(url));
    }
    function finish(choice: "generate" | "replace") {
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
      status.textContent = t("ext.models.prepare.limit");
      generate.disabled = false;
    }).catch(() => {
      if (!settled) status.textContent = t("ext.models.error.invalid_prepared_image");
    });
  });
}
