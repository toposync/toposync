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

export type CameraContextArea = {
  id: string;
  name: string;
  vertices_count: number;
};

export type CameraContextCameraElement = {
  id: string;
  name: string;
  control_points_pairs: number;
  has_mapping: boolean;
};

export type CameraContextComposition = {
  id: string;
  name: string;
  camera_elements: CameraContextCameraElement[];
  areas: CameraContextArea[];
};

export type CameraContextsResponse = {
  camera_id: string;
  compositions: CameraContextComposition[];
};

export type CameraPipelineWizardPreset = "people" | "vehicles_stopped" | "pets";

export type CameraPipelineWizardRequest = {
  preset: CameraPipelineWizardPreset;
  pipeline_name?: string;
  enabled?: boolean;
  processing_server_id?: string;
  composition_id?: string;
  area_id?: string;
  notification_title?: string;
  notification_description?: string;
};

export type CameraPipelineWizardResponse = {
  pipeline_name: string;
};
