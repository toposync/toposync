export type HomeAssistantViewMode = "floor" | "ceiling" | "wall";
export type HomeAssistantSpecialView = "none" | "lamp" | "airflow" | "model" | "ceiling_fan";

export type HomeAssistantServer = {
  id: string;
  name: string;
  host: string;
  apiKey: string;
};

export type HomeAssistantServerPublic = {
  id: string;
  name: string;
  host: string;
  managed: boolean;
  source: "manual" | "supervisor";
};

export type HomeAssistantRegistryEntity = {
  entity_id: string;
  name: string;
  icon?: string;
  domain?: string;
  device_id?: string;
};

export type HomeAssistantRegistryDevice = {
  id: string;
  name: string;
};

export type HomeAssistantRegistryResponse = {
  entities: HomeAssistantRegistryEntity[];
  devices: HomeAssistantRegistryDevice[];
  device_entities: Record<string, string[]>;
};

export type HomeAssistantItemRef = {
  kind: "entity" | "device";
  id: string;
  name?: string;
  domain?: string;
  icon?: string;
  device_id?: string;
};

export type HomeAssistantItemOption = {
  value: string;
  label: string;
  kind: "entity" | "device";
  id: string;
  meta?: {
    subLabel?: string;
    icon?: string;
    domain?: string;
    deviceId?: string;
    deviceName?: string;
    searchText?: string;
  };
};

export type FontAwesomeIconSvg = {
  viewBox: number[];
  path: string;
};

export type FontAwesomeIconFamilies = Record<
  string,
  {
    label?: string;
    search?: { terms?: string[] };
    svgs?: { classic?: { solid?: FontAwesomeIconSvg } };
  }
>;

export type HomeAssistantLiveState = { entity_id?: string; state?: string; attributes?: Record<string, any> };
