import { requestJson, resolveToposyncUrl, type HostI18n, type Notification } from "@toposync/plugin-api";

/** Apenas histórico existente; selecionar uma origem não solicita captura da câmera. */
export function openModelSourcePicker(t: HostI18n["t"], actions: { local: () => void; stored: (file: File) => void; cancel: () => void }): () => void {
  const dialog = document.createElement("dialog");
  dialog.setAttribute("aria-label", t("ext.models.source.title"));
  dialog.style.cssText = "width:min(520px,calc(100vw - 32px));max-height:85vh;overflow:auto;box-sizing:border-box;padding:24px;border:1px solid var(--color-border-strong,#64748b);border-radius:18px;background:var(--color-surface-solid,#fff);color:var(--color-text-primary,#101418)";
  const button = (label: string, action: () => void) => {
    const element = document.createElement("button"); element.type = "button"; element.className = "chipButton";
    element.textContent = t(label); element.style.minHeight = "44px"; element.addEventListener("click", action); return element;
  };
  const heading = document.createElement("h3"); heading.textContent = t("ext.models.source.title");
  const details = document.createElement("details"); details.style.margin = "16px 0";
  const summary = document.createElement("summary"); summary.textContent = t("ext.models.source.history");
  const status = document.createElement("p"); status.setAttribute("role", "status");
  const search = document.createElement("input"); search.className = "input"; search.type = "search"; search.setAttribute("aria-label", t("ext.models.source.search")); search.placeholder = t("ext.models.source.search");
  const form = document.createElement("form"); form.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;margin:12px 0";
  search.style.cssText = "flex:1;min-width:0";
  const find = button("ext.models.source.find", () => {}); find.type = "submit"; form.append(search, find);
  const select = document.createElement("select"); select.className = "input"; select.setAttribute("aria-label", t("ext.models.source.image")); select.style.cssText = "width:100%;min-width:0;height:44px";
  const preview = document.createElement("img"); preview.alt = t("ext.models.editor.preview"); preview.hidden = true; preview.style.cssText = "max-width:100%;max-height:240px;margin:12px auto;object-fit:contain";
  let disposed = false, listOperation: AbortController | null = null, imageOperation: AbortController | null = null;
  let items: Notification[] = [], cursor: number | null = null, objectUrl = "", selectedFile: File | null = null, selected: Notification | null = null;
  const use = button("ext.models.source.use", () => { void useSelected(); }); use.disabled = true;
  const more = button("ext.models.source.more", () => { void load(true); }); more.hidden = true;
  details.append(summary, form, select, status, preview, more, use);
  dialog.append(heading, button("ext.models.source.local", actions.local), details, button("ext.models.editor.cancel", actions.cancel));

  function localImage(value: unknown): string {
    if (typeof value !== "string" || !value.startsWith("/files/") || value.includes("\\")) throw new Error("image unavailable");
    const resolved = new URL(resolveToposyncUrl(value), window.location.href);
    const prefix = new URL(resolveToposyncUrl("/files/"), window.location.href);
    if (resolved.origin !== prefix.origin || !resolved.pathname.startsWith(prefix.pathname)) throw new Error("image unavailable");
    return resolved.href;
  }
  function clearImage() {
    imageOperation?.abort(); imageOperation = null; selectedFile = null; selected = null; use.disabled = true;
    preview.onload = null; preview.onerror = null; preview.removeAttribute("src"); preview.hidden = true;
    if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = "";
  }
  function clearOptions() { items = []; select.replaceChildren(new Option(t("ext.models.source.choose"), "")); }
  async function load(append = false) {
    listOperation?.abort(); const controller = new AbortController(); listOperation = controller;
    clearImage(); select.value = ""; if (!append) { clearOptions(); cursor = null; more.hidden = true; }
    more.disabled = true; status.textContent = t("ext.models.source.loading");
    const params = new URLSearchParams({ limit: "30" }); if (search.value.trim()) params.set("query", search.value.trim());
    if (append && cursor != null) params.set("before", String(cursor));
    try {
      const page = await requestJson<{ notifications: Notification[]; next_cursor: number | null }>(`/api/notifications?${params}`, { signal: controller.signal });
      controller.signal.throwIfAborted();
      for (const item of page.notifications) {
        try { localImage(item.imageUrl); } catch { continue; }
        if (items.some(existing => existing.id === item.id)) continue;
        items.push(item); const time = item.createdAt ? new Date(item.createdAt).toLocaleString() : "";
        select.append(new Option(`${item.title}${time ? ` · ${time}` : ""}`, item.id));
      }
      cursor = page.next_cursor; more.hidden = cursor == null; more.disabled = false;
      status.textContent = items.length ? t("ext.models.source.choose") : t("ext.models.source.empty");
    } catch { if (!controller.signal.aborted) status.textContent = t("ext.models.source.failed"); }
  }
  async function choose() {
    clearImage(); const item = items.find(entry => entry.id === select.value); if (!item) return;
    const controller = new AbortController(); imageOperation = controller; status.textContent = t("ext.models.source.loading");
    try {
      const current = await requestJson<Notification>(`/api/notifications/${encodeURIComponent(item.id)}`, { signal: controller.signal });
      const url = localImage(current.imageUrl);
      const response = await fetch(url, { signal: controller.signal, redirect: "error" });
      const type = response.headers.get("content-type")?.split(";")[0];
      if (!response.ok || !["image/png", "image/jpeg", "image/webp"].includes(type ?? "") || !response.body) throw new Error("image unavailable");
      const reader = response.body.getReader(); const chunks: ArrayBuffer[] = []; let size = 0;
      try { while (true) { const { done, value } = await reader.read(); if (done) break; size += value.byteLength; if (size > 8 * 1024 ** 2) throw new Error("image too large"); chunks.push(new Uint8Array(value).buffer); } }
      finally { await reader.cancel(); reader.releaseLock(); }
      controller.signal.throwIfAborted();
      const extension = type === "image/jpeg" ? "jpg" : type === "image/webp" ? "webp" : "png";
      selectedFile = new File(chunks, `${current.title.replace(/[^\p{L}\p{N} _-]/gu, "").slice(0, 80) || "object"}.${extension}`, { type });
      selected = current; objectUrl = URL.createObjectURL(selectedFile);
      preview.onload = () => {
        if (controller.signal.aborted) return;
        if (!preview.naturalWidth || preview.naturalWidth * preview.naturalHeight > 16_000_000) {
          clearImage(); status.textContent = t("ext.models.source.failed"); return;
        }
        preview.hidden = false; status.textContent = t("ext.models.source.review"); use.disabled = false;
      };
      preview.onerror = () => { if (!controller.signal.aborted) { clearImage(); status.textContent = t("ext.models.source.failed"); } };
      preview.src = objectUrl;
    } catch { if (!controller.signal.aborted) { clearImage(); status.textContent = t("ext.models.source.failed"); } }
  }
  async function useSelected() {
    if (!selected || !selectedFile || !imageOperation) return;
    const controller = imageOperation, file = selectedFile, item = selected; use.disabled = true;
    try {
      const current = await requestJson<Notification>(`/api/notifications/${encodeURIComponent(item.id)}`, { signal: controller.signal });
      controller.signal.throwIfAborted();
      if (current.imageUrl !== item.imageUrl || current.updatedAt !== item.updatedAt) throw new Error("image changed");
      actions.stored(file);
    } catch { if (!controller.signal.aborted) { clearImage(); status.textContent = t("ext.models.source.failed"); } }
  }
  select.addEventListener("change", () => { void choose(); });
  form.addEventListener("submit", event => { event.preventDefault(); void load(); });
  details.addEventListener("toggle", () => { if (details.open && !listOperation && !disposed) void load(); });
  dialog.addEventListener("cancel", event => { event.preventDefault(); actions.cancel(); });
  document.body.appendChild(dialog); clearOptions(); dialog.showModal();
  return () => { disposed = true; listOperation?.abort(); clearImage(); dialog.close(); dialog.remove(); };
}
