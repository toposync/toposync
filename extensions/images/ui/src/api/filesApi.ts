import { resolveToposyncUrl } from "@toposync/plugin-api";
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

  debugLog("[images] POST /api/files/upload", {
    dir: options.dir ?? null,
    filename: options.filename,
    size: file.size,
  });
  const response = await fetch("/api/files/upload", { method: "POST", body: form });
  debugLog("[images] upload response", { status: response.status, ok: response.ok });
  if (!response.ok) throw new Error(`Upload failed: ${response.status}`);
  const data = (await response.json()) as UploadFileResponse;
  return { ...data, url: resolveToposyncUrl(data.url) };
}
