import type { YoloV12Category } from "./yolo";

export type ProcessingServer = {
  id: string;
  name: string;
  url: string;
  username?: string;
  password?: string;
};

export type DetectionCondition =
  | { kind: "motion" }
  | { kind: "ha_sensor"; entity_id: string }
  | { kind: "ha_state"; entity_id: string; state: string }
  | { kind: "object"; category: YoloV12Category };

export type CameraDetection = {
  id: string;
  trigger: DetectionCondition;
  filters: DetectionCondition[];
};

export type CameraConfig = {
  id: string;
  name: string;
  connection_type: "rtsp";
  rtsp_url: string;
  username?: string;
  password?: string;
  fps: number;
  processing_server_id?: string;
  detections?: CameraDetection[];
};

export type CamerasIndex = {
  processing_servers: Array<{ id: string; name: string; url: string }>;
  cameras: Array<{ id: string; name: string; connection_type: string; processing_server_id?: string }>;
};

export type ControlPoint = {
  id: string;
  label: string;
  image?: { x: number; y: number } | null;
  world?: { x: number; z: number } | null;
};

