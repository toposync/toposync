export type Vector3 = { x: number; y: number; z: number };

export type UploadFileResponse = {
  dir: string;
  path: string;
  url: string;
  filename: string;
  content_type?: string | null;
  size_bytes: number;
};

export type ModelPreviewResult = {
  dataUrl: string;
  widthPx: number;
  heightPx: number;
  size: Vector3;
  center: Vector3;
  minY: number;
};

