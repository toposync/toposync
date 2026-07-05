import { requestForm, resolveToposyncUrl } from "@toposync/plugin-api";
import { debugLog } from "../debug";
import type { UploadFileResponse } from "../types";

export async function uploadToFilesDir(
  file: Blob,
  options: { dir?: string; filename: string },
): Promise<UploadFileResponse> {
  const form = new FormData();
  form.append("file", file, options.filename);
  if (options.dir) form.append("dir", options.dir);
  form.append("filename", options.filename);

  debugLog("[models:tool] POST /api/files/upload", {
    dir: options.dir ?? null,
    filename: options.filename,
    size: file.size,
  });
  const data = await requestForm<UploadFileResponse>("/api/files/upload", form);
  debugLog("[models:tool] upload response", { ok: true });
  return { ...data, url: resolveToposyncUrl(data.url) };
}
