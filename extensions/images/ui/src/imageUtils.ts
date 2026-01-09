export function filenameStem(filename: string): string {
  const base = filename.replace(/^.*[\\/]/, "");
  const idx = base.lastIndexOf(".");
  if (idx <= 0) return base;
  return base.slice(0, idx);
}

export function isImageFile(file: File): boolean {
  if (file.type && file.type.startsWith("image/")) return true;
  const name = file.name.toLowerCase();
  return (
    name.endsWith(".png") ||
    name.endsWith(".jpg") ||
    name.endsWith(".jpeg") ||
    name.endsWith(".webp") ||
    name.endsWith(".gif") ||
    name.endsWith(".bmp") ||
    name.endsWith(".svg")
  );
}

export async function readImageDimensions(file: File): Promise<{ width: number; height: number } | null> {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    return { width: img.naturalWidth, height: img.naturalHeight };
  } catch {
    return null;
  } finally {
    URL.revokeObjectURL(url);
  }
}

