export type UploadFileResponse = {
  dir: string;
  path: string;
  url: string;
  filename: string;
  content_type?: string | null;
  size_bytes: number;
};

