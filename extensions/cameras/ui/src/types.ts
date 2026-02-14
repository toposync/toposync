export type CameraConfig = {
  id: string;
  name: string;
  connection_type: "rtsp";
  rtsp_url: string;
  username?: string;
  password?: string;
  fps: number;
};

export type CamerasIndex = {
  cameras: Array<{ id: string; name: string; connection_type: string }>;
};

export type ControlPoint = {
  id: string;
  label: string;
  image?: { x: number; y: number } | null;
  world?: { x: number; z: number } | null;
};
