import React from "react";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

import type { CompositionElement, ElementType, HostI18n } from "@toposync/plugin-api";

import { readScale as readModelScale, readVector3 as readModelVector3 } from "../../../../models/ui/src/parsing";
import { createGltfModelRuntime } from "../../../../models/ui/src/runtime/gltfModel";

import { createAirflowEffect } from "../airflow";
import {
  DEFAULT_AIRFLOW_INTENSITY,
  DEFAULT_LAMP_COLOR,
  DEFAULT_LAMP_INTENSITY,
  HOME_ASSISTANT_ELEMENT_TYPE_ID,
  AIRFLOW_COMPATIBLE_DOMAINS,
  LAMP_COMPATIBLE_DOMAINS,
} from "../constants";
import {
  boolStateForDomain,
  climateFlowFromLiveState,
  domainFromEntityId,
  isToggleDomain,
  readHomeAssistantSpecialView,
  readHomeAssistantViewMode,
} from "../domain";
import {
  isFontAwesomeSolidIconAvailable,
  normalizeFontAwesomeSvgName,
  resolveFontAwesomeSvg,
  sanitizeFontAwesomeIconName,
} from "../fontAwesome";
import {
  clamp,
  readAirflowIntensity,
  readAirflowWidth,
  readHexColor,
  readLampIntensity,
  readOptionalFiniteNumber,
  readRecord,
  readString,
} from "../parsing";
import { getHomeAssistantLiveState, setHomeAssistantLiveState, watchHomeAssistantLiveStates } from "../liveStates";
import { HomeAssistantAction } from "../ui/HomeAssistantAction";
import { HomeAssistantEditor } from "../ui/HomeAssistantEditor";

import type { HomeAssistantLiveState, HomeAssistantSpecialView, HomeAssistantViewMode } from "../types";

const CEILING_FAN_BLADE_COUNT = 5;
const CEILING_FAN_HUB_RADIUS = 0.09;
const CEILING_FAN_BLADE_LENGTH = 0.55;
const CEILING_FAN_BLADE_ROOT_INSET = 0.07;
const CEILING_FAN_RADIUS_WORLD = CEILING_FAN_HUB_RADIUS + CEILING_FAN_BLADE_LENGTH - CEILING_FAN_BLADE_ROOT_INSET;

export function createHomeAssistantElementType(i18n: HostI18n): ElementType {
  const iconGeometryCache = new Map<string, { geometry: any; scale: number }>();
  const modelPreviewImageCache = new Map<string, HTMLImageElement>();
  const iconTargetSize = 0.14;

  const buttonRadius = 0.18;
  const buttonThetaTopCut = 1.05;

  return {
    type: HOME_ASSISTANT_ELEMENT_TYPE_ID,
    name: { key: "ext.home_assistant.element.name", fallback: "Home Assistant item" },
    description: { key: "ext.home_assistant.element.desc" },
    placeable: false,
    defaultProps: {
      server_id: "",
      items: [],
      icon: "house",
      primary_entity_id: "",
      primary_state: "",
      view_mode: "floor",
      special_view: "none",
      lamp_intensity: DEFAULT_LAMP_INTENSITY,
      lamp_color: DEFAULT_LAMP_COLOR,
      airflow_intensity: DEFAULT_AIRFLOW_INTENSITY,
      model3d: null,
    },
    primaryAction: async ({ element, api, update }) => {
      const props = readRecord(element.props);
      const serverId = readString(props.server_id).trim();
      const entityId = readString(props.primary_entity_id).trim();
      if (!serverId || !entityId) return false;
      const domain = domainFromEntityId(entityId);
      if (!isToggleDomain(domain)) return false;

      const res = await api.emitEvent("home_assistant.primary_action_requested", {
        server_id: serverId,
        entity_id: entityId,
      });
      const state = (res as any)?.result?.state;
      if (typeof state === "string") {
        update({ props: { primary_state: state } });
        setHomeAssistantLiveState(serverId, entityId, { entity_id: entityId, state });
      }
      return true;
    },
    create3D: ({ THREE, view }, element) => {
      function getIconGeometry(iconName: string): { geometry: any; scale: number; key: string } {
        const resolved = resolveFontAwesomeSvg(iconName);
        const cached = iconGeometryCache.get(resolved.key);
        if (cached) return { ...cached, key: resolved.key };

        const data = new SVGLoader().parse(resolved.svgText);

        const shapes: any[] = [];
        for (const path of data.paths) shapes.push(...SVGLoader.createShapes(path));

        const geometry = new THREE.ShapeGeometry(shapes);
        geometry.computeBoundingBox();
        const bbox = geometry.boundingBox;
        if (bbox) {
          const cx = (bbox.min.x + bbox.max.x) / 2;
          const cy = (bbox.min.y + bbox.max.y) / 2;
          geometry.translate(-cx, -cy, 0);
        }

        geometry.scale(1, -1, 1);
        geometry.rotateX(-Math.PI / 2);

        geometry.computeBoundingBox();
        const bbox3 = geometry.boundingBox;
        const sizeX = bbox3 ? bbox3.max.x - bbox3.min.x : 1;
        const sizeZ = bbox3 ? bbox3.max.z - bbox3.min.z : 1;
        const maxXZ = Math.max(sizeX, sizeZ, 1e-9);
        const scale = iconTargetSize / maxXZ;

        const entry = { geometry, scale };
        iconGeometryCache.set(resolved.key, entry);
        return { ...entry, key: resolved.key };
      }

      const neonDefault = 0x38bdf8;
      const neonOn = 0x22c55e;
      const neonOff = 0xef4444;

      const group = new THREE.Group();
      const mountGroup = new THREE.Group();
      group.add(mountGroup);
      const airflow = createAirflowEffect(THREE, { particleCount: 700 });
      group.add(airflow.object);
      const modelMount = new THREE.Group();
      modelMount.visible = false;
      const model3d = createGltfModelRuntime(THREE, { autoplay: false });
      model3d.setAnimated(false);
      modelMount.add(model3d.object);
      group.add(modelMount);

      const detailedGeometry = (view.graphicsQuality ?? "simplified") === "detailed";

      const fanMount = new THREE.Group();
      fanMount.visible = false;
      mountGroup.add(fanMount);

      const fanSegments = detailedGeometry ? 28 : 18;
      const fanBladeCount = CEILING_FAN_BLADE_COUNT;

      const fanMetalMaterial = new THREE.MeshStandardMaterial({
        color: 0x0f172a,
        emissive: new THREE.Color(0x000000),
        emissiveIntensity: 0.0,
        roughness: 0.32,
        metalness: 0.85,
      });
      const fanBodyMaterial = new THREE.MeshStandardMaterial({
        color: 0x111827,
        emissive: new THREE.Color(0x000000),
        emissiveIntensity: 0.0,
        roughness: 0.62,
        metalness: 0.22,
      });
      const fanBladeMaterial = new THREE.MeshStandardMaterial({
        color: 0x8b5e34,
        emissive: new THREE.Color(0x000000),
        emissiveIntensity: 0.0,
        roughness: 0.78,
        metalness: 0.05,
      });
      const fanAccentMaterial = new THREE.MeshStandardMaterial({
        color: 0x0b1220,
        emissive: new THREE.Color(neonDefault),
        emissiveIntensity: 0.0,
        roughness: 0.25,
        metalness: 0.0,
      });
      const fanBlurMaterial = new THREE.MeshBasicMaterial({
        color: neonDefault,
        transparent: true,
        opacity: 0.0,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });

      const fanCeilingGap = 0.002;
      const fanCanopyHeight = 0.042;
      const fanDownrodHeight = 0.18;
      const fanMotorHeight = 0.12;
      const fanHubHeight = 0.026;
      const fanBladeLength = CEILING_FAN_BLADE_LENGTH;
      const fanBladeWidth = 0.115;
      const fanBladeThickness = 0.009;
      const fanBladeRootInset = CEILING_FAN_BLADE_ROOT_INSET;
      const fanHubRadius = CEILING_FAN_HUB_RADIUS;

      const fanCanopyY = -fanCeilingGap - fanCanopyHeight / 2;
      const fanDownrodY = fanCanopyY - fanCanopyHeight / 2 - fanDownrodHeight / 2;
      const fanMotorY = fanDownrodY - fanDownrodHeight / 2 - fanMotorHeight / 2;
      const fanRotorY = fanMotorY - fanMotorHeight / 2 - fanHubHeight / 2 - 0.004;

      const canopyGeometry = new THREE.CylinderGeometry(0.11, 0.085, fanCanopyHeight, fanSegments, 1, false);
      const canopyMesh = new THREE.Mesh(canopyGeometry, fanMetalMaterial);
      canopyMesh.position.y = fanCanopyY;
      fanMount.add(canopyMesh);

      const downrodGeometry = new THREE.CylinderGeometry(0.012, 0.012, fanDownrodHeight, Math.max(10, Math.floor(fanSegments / 2)), 1, false);
      const downrodMesh = new THREE.Mesh(downrodGeometry, fanMetalMaterial);
      downrodMesh.position.y = fanDownrodY;
      fanMount.add(downrodMesh);

      const motorGeometry = new THREE.CylinderGeometry(0.15, 0.17, fanMotorHeight, fanSegments, 1, false);
      const motorMesh = new THREE.Mesh(motorGeometry, fanBodyMaterial);
      motorMesh.position.y = fanMotorY;
      fanMount.add(motorMesh);

      const motorRingGeometry = new THREE.TorusGeometry(0.135, 0.007, Math.max(8, Math.floor(fanSegments / 3)), fanSegments * 2);
      const motorRingMesh = new THREE.Mesh(motorRingGeometry, fanMetalMaterial);
      motorRingMesh.rotation.x = Math.PI / 2;
      motorRingMesh.position.y = fanMotorY + 0.01;
      fanMount.add(motorRingMesh);

      const lightRingGeometry = new THREE.RingGeometry(0.052, 0.108, fanSegments);
      const lightRingMesh = new THREE.Mesh(lightRingGeometry, fanAccentMaterial);
      lightRingMesh.rotation.x = Math.PI / 2;
      lightRingMesh.position.y = fanMotorY - fanMotorHeight / 2 - 0.002;
      lightRingMesh.renderOrder = 3;
      (lightRingMesh.material as any).side = THREE.DoubleSide;
      fanMount.add(lightRingMesh);

      const fanRotor = new THREE.Group();
      fanRotor.position.y = fanRotorY;
      fanMount.add(fanRotor);

      const hubGeometry = new THREE.CylinderGeometry(fanHubRadius, fanHubRadius * 0.92, fanHubHeight, fanSegments, 1, false);
      const hubMesh = new THREE.Mesh(hubGeometry, fanMetalMaterial);
      fanRotor.add(hubMesh);

      const capGeometry = new THREE.SphereGeometry(fanHubRadius * 0.55, fanSegments, Math.max(10, Math.floor(fanSegments / 2)), 0, Math.PI * 2, 0, Math.PI / 2);
      const capMesh = new THREE.Mesh(capGeometry, fanMetalMaterial);
      capMesh.position.y = -fanHubHeight / 2 - 0.002;
      fanRotor.add(capMesh);

      const bladeGeometry = new THREE.BoxGeometry(
        fanBladeLength,
        fanBladeThickness,
        fanBladeWidth,
        detailedGeometry ? 10 : 6,
        1,
        detailedGeometry ? 4 : 2,
      );
      const bladePositions = bladeGeometry.attributes.position as any;
      for (let i = 0; i < bladePositions.count; i += 1) {
        const x = bladePositions.getX(i);
        const t = clamp(x / fanBladeLength + 0.5, 0, 1);
        const taper = 1 - 0.42 * t;
        bladePositions.setZ(i, bladePositions.getZ(i) * taper);
        bladePositions.setY(i, bladePositions.getY(i) + Math.sin(t * Math.PI) * 0.004);
      }
      bladePositions.needsUpdate = true;
      bladeGeometry.computeVertexNormals();
      bladeGeometry.translate(fanBladeLength / 2 - fanBladeRootInset, 0, 0);

      const bladePitch = -0.24;
      for (let i = 0; i < fanBladeCount; i += 1) {
        const arm = new THREE.Group();
        arm.rotation.y = (i / fanBladeCount) * Math.PI * 2;
        const blade = new THREE.Mesh(bladeGeometry, fanBladeMaterial);
        blade.position.x = fanHubRadius;
        blade.rotation.x = bladePitch;
        arm.add(blade);
        fanRotor.add(arm);
      }

      const blurDiscGeometry = new THREE.CircleGeometry(fanHubRadius + fanBladeLength * 0.78, fanSegments);
      const blurDiscMesh = new THREE.Mesh(blurDiscGeometry, fanBlurMaterial);
      blurDiscMesh.rotation.x = Math.PI / 2;
      blurDiscMesh.position.y = fanBladeThickness / 2 + 0.01;
      blurDiscMesh.visible = detailedGeometry;
      blurDiscMesh.renderOrder = 2;
      fanRotor.add(blurDiscMesh);

      const topY = buttonRadius * Math.cos(buttonThetaTopCut);
      const topRadius = buttonRadius * Math.sin(buttonThetaTopCut);
      const airflowWallVentWidth = 0.72;
      const airflowWallVentRadius = 0.03;
      const airflowWallVentTopMargin = 0.22;
      const airflowWallVentZ = 0.08;
      const airflowCassetteWidth = 0.62;
      const airflowCassetteDepth = 0.42;
      const airflowCassetteThickness = 0.05;
      const airflowCassetteCornerRadius = 0.09;
      const airflowStartOffset = 0.045;

      const domeFloorGeometry = new THREE.SphereGeometry(
        buttonRadius,
        56,
        28,
        0,
        Math.PI * 2,
        buttonThetaTopCut,
        Math.PI / 2 - buttonThetaTopCut,
      );
      const domeCeilingGeometry = new THREE.SphereGeometry(
        buttonRadius,
        56,
        34,
        0,
        Math.PI * 2,
        buttonThetaTopCut,
        Math.PI - buttonThetaTopCut,
      );

      const sphereMaterial = new THREE.MeshStandardMaterial({
        color: 0x0b1220,
        emissive: new THREE.Color(neonDefault),
        emissiveIntensity: 0.85,
        roughness: 0.32,
        metalness: 0.0,
      });
      const domeBaseColorHex = 0x0b1220;
      const lampBodyColorHex = 0x111827;

      const cutMaterial = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      const cutBaseColorHex = 0x000000;
      const iconMaterial = new THREE.MeshBasicMaterial({ color: neonDefault, side: THREE.DoubleSide });
      iconMaterial.depthWrite = false;
      iconMaterial.polygonOffset = true;
      iconMaterial.polygonOffsetFactor = -1;
      iconMaterial.polygonOffsetUnits = -1;

      const dome = new THREE.Mesh(domeFloorGeometry, sphereMaterial);
      mountGroup.add(dome);

      const topCapGeometry = new THREE.CircleGeometry(topRadius, 48);
      const topCap = new THREE.Mesh(topCapGeometry, cutMaterial);
      topCap.rotation.x = -Math.PI / 2;
      topCap.position.set(0, topY, 0);
      mountGroup.add(topCap);

      const bottomCapGeometry = new THREE.CircleGeometry(buttonRadius, 48);
      const bottomCap = new THREE.Mesh(bottomCapGeometry, cutMaterial);
      bottomCap.rotation.x = Math.PI / 2;
      bottomCap.position.set(0, 0, 0);
      mountGroup.add(bottomCap);

      const light = new THREE.PointLight(neonDefault, 0.9, 1.15, 2.2);
      light.position.set(0, topY * 0.6, 0);
      light.castShadow = false;
      light.shadow.mapSize.set(256, 256);
      light.shadow.bias = -0.00035;
      light.shadow.normalBias = 0.02;
      light.shadow.camera.near = 0.05;
      light.shadow.camera.far = 16;
      mountGroup.add(light);

      const houseGeometry = getIconGeometry("house");
      const iconMesh = new THREE.Mesh(houseGeometry.geometry, iconMaterial);
      iconMesh.scale.setScalar(houseGeometry.scale);
      iconMesh.position.set(0, topY + 0.002, 0);
      iconMesh.renderOrder = 10;
      mountGroup.add(iconMesh);

      const airflowVentMaterial = new THREE.MeshStandardMaterial({
        color: 0x0b1220,
        emissive: new THREE.Color(0x000000),
        emissiveIntensity: 0.1,
        roughness: 0.5,
        metalness: 0.06,
      });

      function createAirflowWallVentGeometry(ventWidth: number): any {
        return new THREE.CapsuleGeometry(
          airflowWallVentRadius,
          Math.max(0.01, ventWidth - airflowWallVentRadius * 2),
          5,
          14,
        );
      }

      let airflowWallVentGeometry = createAirflowWallVentGeometry(airflowWallVentWidth);
      const airflowWallVent = new THREE.Mesh(airflowWallVentGeometry, airflowVentMaterial);
      airflowWallVent.rotation.z = -Math.PI / 2;
      airflowWallVent.visible = false;
      mountGroup.add(airflowWallVent);

      function roundedRectShape(width: number, height: number, radius: number): any {
        const w = Math.max(0.01, width);
        const h = Math.max(0.01, height);
        const r = Math.max(0.001, Math.min(radius, Math.min(w, h) / 2));

        const x0 = -w / 2;
        const y0 = -h / 2;
        const x1 = w / 2;
        const y1 = h / 2;

        const shape = new THREE.Shape();
        shape.moveTo(x0 + r, y0);
        shape.lineTo(x1 - r, y0);
        shape.quadraticCurveTo(x1, y0, x1, y0 + r);
        shape.lineTo(x1, y1 - r);
        shape.quadraticCurveTo(x1, y1, x1 - r, y1);
        shape.lineTo(x0 + r, y1);
        shape.quadraticCurveTo(x0, y1, x0, y1 - r);
        shape.lineTo(x0, y0 + r);
        shape.quadraticCurveTo(x0, y0, x0 + r, y0);
        return shape;
      }

      const airflowCassetteGeometry = new THREE.ExtrudeGeometry(
        roundedRectShape(airflowCassetteWidth, airflowCassetteDepth, airflowCassetteCornerRadius),
        {
          depth: airflowCassetteThickness,
          bevelEnabled: true,
          bevelThickness: airflowCassetteThickness * 0.55,
          bevelSize: airflowCassetteCornerRadius * 0.35,
          bevelOffset: 0,
          bevelSegments: 2,
          steps: 1,
        },
      );
      airflowCassetteGeometry.rotateX(Math.PI / 2);

      const airflowCassette = new THREE.Mesh(airflowCassetteGeometry, airflowVentMaterial);
      airflowCassette.visible = false;
      mountGroup.add(airflowCassette);

      let wantedIconKey = "house";
      let currentIconKey = houseGeometry.key;
      let currentViewMode: HomeAssistantViewMode = "floor";
      let currentSpecialView: HomeAssistantSpecialView = "none";
      let currentItemCount = 0;
      const lampColor = new THREE.Color(DEFAULT_LAMP_COLOR);
      let lampIntensity = DEFAULT_LAMP_INTENSITY;
      let airflowIntensity = DEFAULT_AIRFLOW_INTENSITY;
      let currentAirflowMount: "wall" | "ceiling" = "wall";
      let currentAirflowWallVentWidth = airflowWallVentWidth;
      let currentAirflowCassetteWidth = airflowCassetteWidth;
      let fanTargetSpeed01 = 0;
      let fanSpeed01 = 0;
      let fanSpinY = 0;
      let fanOn = false;

      let unwatch: (() => void) | null = null;
      let watchedServer = "";
      let watchedEntity = "";
      let watchedDomain = "";
      let watchedIsToggle = false;
      let lastLiveSig = "";

      type FanFlow = {
        active: boolean;
        speed01: number;
        sig: string;
      };

      function fanFlowFromLiveState(live: HomeAssistantLiveState | null, fallbackStateRaw: string): FanFlow {
        const state = readString(live?.state ?? fallbackStateRaw).trim().toLowerCase();
        const attrs = live?.attributes && typeof live.attributes === "object" ? (live.attributes as Record<string, any>) : null;
        const pctRaw = attrs ? Number(attrs.percentage) : NaN;
        const pct = Number.isFinite(pctRaw) ? clamp(pctRaw, 0, 100) : NaN;
        const speed = attrs ? readString(attrs.speed).trim().toLowerCase() : "";
        const preset = attrs ? readString(attrs.preset_mode).trim().toLowerCase() : "";
        const boolState = watchedEntity ? boolStateForDomain(watchedDomain, state) : null;

        const sig = [
          state || "",
          Number.isFinite(pct) ? `pct:${Math.round(pct)}` : "",
          speed ? `speed:${speed}` : "",
          preset ? `preset:${preset}` : "",
        ]
          .filter(Boolean)
          .join("|");

        if (boolState !== true) return { active: false, speed01: 0, sig };

        let speed01 = 0.65;
        if (Number.isFinite(pct)) {
          speed01 = clamp(pct / 100, 0, 1);
        } else {
          const tag = (speed || preset).trim();
          if (tag.includes("low") || tag.includes("slow") || tag === "1") speed01 = 0.33;
          else if (tag.includes("med") || tag.includes("mid") || tag === "2") speed01 = 0.66;
          else if (tag.includes("high") || tag.includes("fast") || tag === "3") speed01 = 1.0;
          else if (tag.includes("auto")) speed01 = 0.75;
        }

        if (!Number.isFinite(speed01) || speed01 <= 0.02) speed01 = 0.35;
        return { active: true, speed01: clamp(speed01, 0, 1), sig };
      }

      function applyNeonFromState(stateRaw: string, live: HomeAssistantLiveState | null) {
        const s = stateRaw.trim().toLowerCase();
        const boolState = watchedEntity ? boolStateForDomain(watchedDomain, s) : null;
        const canLamp =
          currentSpecialView === "lamp" &&
          currentItemCount === 1 &&
          watchedDomain &&
          LAMP_COMPATIBLE_DOMAINS.has(watchedDomain.toLowerCase());
        const canAirflow =
          currentSpecialView === "airflow" &&
          currentItemCount === 1 &&
          watchedDomain &&
          AIRFLOW_COMPATIBLE_DOMAINS.has(watchedDomain.toLowerCase());

        sphereMaterial.color.setHex(domeBaseColorHex);
        cutMaterial.color.setHex(cutBaseColorHex);

        const syncLightShadow = () => {
          const wantsShadow = Boolean(view.ghostWalls) && light.intensity > 0.001 && light.distance > 0.001;
          if (light.castShadow !== wantsShadow) {
            light.castShadow = wantsShadow;
            light.shadow.needsUpdate = true;
          }

          if (!wantsShadow) return;
          const desiredFar = Math.max(2, light.distance);
          if (Math.abs(light.shadow.camera.far - desiredFar) > 1e-3) {
            light.shadow.camera.far = desiredFar;
            light.shadow.camera.updateProjectionMatrix();
            light.shadow.needsUpdate = true;
          }
        };

        if (canAirflow) {
          const flow = climateFlowFromLiveState(live, stateRaw);
          const amp = clamp(airflowIntensity, 0.2, 3.0) * clamp(flow.factor, 0, 1);
          const active = flow.active && amp > 0.05 && flow.mode !== "off";

          const baseColor = flow.mode === "heat" ? 0xff6b6b : flow.mode === "cool" ? 0x4dabf7 : 0x93c5fd;
          const detailed = (view.graphicsQuality ?? "simplified") === "detailed";
          const baseParticleBudget = detailed ? 520 : 320;
          const widthScale =
            currentAirflowMount === "ceiling"
              ? currentAirflowCassetteWidth / airflowCassetteWidth
              : currentAirflowWallVentWidth / airflowWallVentWidth;
          const particleBudget = Math.max(
            30,
            Math.min(baseParticleBudget, Math.floor(baseParticleBudget * clamp(widthScale, 0.05, 1.0))),
          );
          const pitch = currentAirflowMount === "wall" ? (flow.mode === "heat" ? 0.22 : flow.mode === "cool" ? -0.18 : 0.06) : 0.0;
          const dir = active ? { x: 0, y: pitch, z: 1 } : { x: 0, y: 0, z: 1 };
          const origin = { x: 0, y: 0, z: airflowStartOffset };
          const ventWidth =
            currentAirflowMount === "ceiling"
              ? currentAirflowCassetteWidth * 0.78
              : currentAirflowWallVentWidth * 0.92;
          const ventHeight = currentAirflowMount === "ceiling" ? airflowCassetteDepth * 0.55 : 0.08;

          airflow.update({
            active,
            mode: flow.mode,
            intensity: amp,
            origin,
            direction: dir,
            ventWidth,
            ventHeight,
            activeParticleCount: particleBudget,
          });

          airflowVentMaterial.emissive.set(active ? baseColor : 0x000000);
          light.color.set(active ? baseColor : 0x000000);

          airflowVentMaterial.emissiveIntensity = active ? 0.22 + 0.08 * amp : 0.06;
          light.intensity = active ? 0.07 + 0.09 * amp : 0.0;
          light.distance = active ? 0.95 + 0.6 * amp : 0.0;
          syncLightShadow();
          return;
        }

        airflow.update({ active: false });

        if (canLamp) {
          sphereMaterial.color.setHex(lampBodyColorHex);
          cutMaterial.color.setHex(lampBodyColorHex);

          const on = boolState === true;
          const unknown = boolState == null;

          const neon = on ? lampColor : 0x000000;
          sphereMaterial.emissive.set(neon);
          iconMaterial.color.set(lampColor);
          light.color.set(lampColor);

          if (on) {
            const amp = clamp(lampIntensity, 0.2, 3.0);
            sphereMaterial.emissiveIntensity = 0.55 + 0.75 * amp;
            light.intensity = 1.8 * amp;
            light.distance = 4.5 + 3.5 * amp;
          } else if (unknown) {
            sphereMaterial.emissiveIntensity = 0.22;
            light.intensity = 0.0;
            light.distance = 0.0;
          } else {
            sphereMaterial.emissiveIntensity = 0.08;
            light.intensity = 0.0;
            light.distance = 0.0;
          }
          syncLightShadow();
          return;
        }

        const neon = watchedIsToggle
          ? boolState === true
            ? neonOn
            : boolState === false
              ? neonOff
              : neonDefault
          : neonDefault;

        sphereMaterial.emissive.set(neon);
        iconMaterial.color.set(neon);
        light.color.set(neon);

        sphereMaterial.emissiveIntensity = watchedIsToggle
          ? boolState === true
            ? 0.55
            : boolState === false
              ? 0.35
              : 0.42
          : 0.42;
        light.intensity = watchedIsToggle
          ? boolState === true
            ? 0.25
            : boolState === false
              ? 0.12
              : 0.16
          : 0.16;
        light.distance = 1.6;
        syncLightShadow();
      }

      function applyViewMode(mode: HomeAssistantViewMode) {
        if (mode !== currentViewMode) {
          currentViewMode = mode;
          if (mode === "ceiling") {
            dome.geometry = domeCeilingGeometry;
            bottomCap.visible = false;
          } else {
            dome.geometry = domeFloorGeometry;
            bottomCap.visible = true;
          }
        }

        mountGroup.rotation.set(0, 0, 0);
        mountGroup.position.set(0, 0, 0);

        if (mode === "ceiling") {
          mountGroup.position.y = view.wallHeight - topY;
        } else if (mode === "wall") {
          mountGroup.position.y = view.wallHeight / 2;
          mountGroup.rotation.x = Math.PI / 2;
        }
      }

      function apply(el: CompositionElement) {
        const p = readRecord(el.props);
        const icon = sanitizeFontAwesomeIconName(readString(p.icon, "house")) || "house";
        const viewMode = readHomeAssistantViewMode(p.view_mode);
        const specialView = readHomeAssistantSpecialView(p.special_view);
        const itemsRaw = p.items;
        const itemCount = Array.isArray(itemsRaw) ? itemsRaw.length : 0;
        const primaryEntityId = readString(p.primary_entity_id).trim();
        const serverId = readString(p.server_id).trim();

        if (serverId !== watchedServer || primaryEntityId !== watchedEntity) {
          unwatch?.();
          unwatch = null;
          watchedServer = serverId;
          watchedEntity = primaryEntityId;
          watchedDomain = primaryEntityId ? domainFromEntityId(primaryEntityId) : "";
          watchedIsToggle = watchedDomain ? isToggleDomain(watchedDomain) : false;
          lastLiveSig = "";
          if (serverId && primaryEntityId) unwatch = watchHomeAssistantLiveStates(serverId, [primaryEntityId]);
        }

        const live = watchedServer && watchedEntity ? getHomeAssistantLiveState(watchedServer, watchedEntity) : null;
        const primaryState = readString(live?.state ?? p.primary_state);

        currentItemCount = itemCount;
        currentSpecialView = specialView;
        lampColor.set(readHexColor(p.lamp_color, DEFAULT_LAMP_COLOR));
        lampIntensity = readLampIntensity(p.lamp_intensity);
        airflowIntensity = readAirflowIntensity(p.airflow_intensity);
        const airflowWallWidth = readAirflowWidth(p.airflow_width, airflowWallVentWidth);
        const airflowCassetteWidthValue = readAirflowWidth(p.airflow_width, airflowCassetteWidth);
        const airflowMountYOverride = readOptionalFiniteNumber(p.airflow_mount_y);
        if (
          currentSpecialView === "lamp" &&
          !(itemCount === 1 && watchedDomain && LAMP_COMPATIBLE_DOMAINS.has(watchedDomain.toLowerCase()))
        ) {
          currentSpecialView = "none";
        }
        if (
          currentSpecialView === "airflow" &&
          !(itemCount === 1 && watchedDomain && AIRFLOW_COMPATIBLE_DOMAINS.has(watchedDomain.toLowerCase()))
        ) {
          currentSpecialView = "none";
        }
        if (currentSpecialView === "model" && !(itemCount === 1 && watchedEntity)) {
          currentSpecialView = "none";
        }
        if (currentSpecialView === "ceiling_fan" && !(itemCount === 1 && watchedDomain && watchedDomain.toLowerCase() === "fan")) {
          currentSpecialView = "none";
        }

        if (currentSpecialView === "airflow") {
          modelMount.visible = false;
          model3d.setAnimated(false);
          fanMount.visible = false;
          fanTargetSpeed01 = 0;
          fanSpeed01 = 0;
          fanOn = false;

          currentAirflowMount = viewMode === "ceiling" ? "ceiling" : "wall";

          currentAirflowCassetteWidth = airflowCassetteWidthValue;
          airflowCassette.scale.set(currentAirflowCassetteWidth / airflowCassetteWidth, 1, 1);

          if (Math.abs(airflowWallWidth - currentAirflowWallVentWidth) > 1e-6) {
            currentAirflowWallVentWidth = airflowWallWidth;
            airflowWallVentGeometry.dispose();
            airflowWallVentGeometry = createAirflowWallVentGeometry(currentAirflowWallVentWidth);
            airflowWallVent.geometry = airflowWallVentGeometry;
          }

          dome.visible = false;
          topCap.visible = false;
          bottomCap.visible = false;
          iconMesh.visible = false;
          airflowWallVent.visible = currentAirflowMount === "wall";
          airflowCassette.visible = currentAirflowMount === "ceiling";

          mountGroup.rotation.set(0, 0, 0);
          mountGroup.position.set(0, 0, 0);

          const mountY =
            airflowMountYOverride ??
            (currentAirflowMount === "ceiling" ? view.wallHeight - 0.001 : view.wallHeight - airflowWallVentTopMargin);

          if (currentAirflowMount === "ceiling") {
            mountGroup.position.y = mountY;
            mountGroup.position.z = 0;
            light.position.set(0, -0.12, 0);
            airflow.object.position.set(0, mountGroup.position.y, mountGroup.position.z);
            airflow.object.rotation.set(Math.PI / 2, 0, 0);
          } else {
            mountGroup.position.y = mountY;
            mountGroup.position.z = airflowWallVentZ;
            light.position.set(0, 0.0, 0.02);
            airflow.object.position.set(0, mountGroup.position.y, mountGroup.position.z);
            airflow.object.rotation.set(0, 0, 0);
          }
        } else if (currentSpecialView === "model") {
          modelMount.visible = true;
          fanMount.visible = false;
          fanTargetSpeed01 = 0;
          fanSpeed01 = 0;
          fanOn = false;

          const modelProps = readRecord(p.model3d);
          model3d.updateFromProps(modelProps);

          dome.visible = false;
          topCap.visible = false;
          bottomCap.visible = false;
          iconMesh.visible = false;
          airflowWallVent.visible = false;
          airflowCassette.visible = false;

          mountGroup.rotation.set(0, 0, 0);
          mountGroup.position.set(0, 0, 0);

          modelMount.rotation.set(0, 0, 0);
          modelMount.position.set(0, 0, 0);
          model3d.object.position.set(0, 0, 0);

          if (viewMode === "ceiling") {
            modelMount.position.y = view.wallHeight;
            const size = readModelVector3(modelProps.size, { x: 0, y: 0, z: 0 });
            const scale = readModelScale(modelProps.scale, 1);
            const height = Math.max(0, size.y * scale);
            if (Number.isFinite(height) && height > 0) model3d.object.position.y = -height;
          } else if (viewMode === "wall") {
            modelMount.position.y = view.wallHeight / 2;
            modelMount.rotation.x = Math.PI / 2;
          }
        } else if (currentSpecialView === "ceiling_fan") {
          modelMount.visible = false;
          model3d.setAnimated(false);
          fanMount.visible = true;

          dome.visible = false;
          topCap.visible = false;
          bottomCap.visible = false;
          iconMesh.visible = false;
          airflowWallVent.visible = false;
          airflowCassette.visible = false;

          mountGroup.rotation.set(0, 0, 0);
          mountGroup.position.set(0, 0, 0);
          mountGroup.position.y = view.wallHeight - 0.001;
          light.position.set(0, fanRotorY - 0.06, 0);
        } else {
          modelMount.visible = false;
          model3d.setAnimated(false);
          fanMount.visible = false;
          fanTargetSpeed01 = 0;
          fanSpeed01 = 0;
          fanOn = false;

          airflowWallVent.visible = false;
          airflowCassette.visible = false;
          iconMesh.visible = true;
          dome.visible = true;
          topCap.visible = true;

          applyViewMode(viewMode);

          light.position.set(0, topY * 0.6, 0);
        }

        wantedIconKey = normalizeFontAwesomeSvgName(icon) || "house";
        const entry = getIconGeometry(wantedIconKey);
        if (entry.key !== currentIconKey) {
          currentIconKey = entry.key;
          iconMesh.geometry = entry.geometry;
          iconMesh.scale.setScalar(entry.scale);
        }

        if (currentSpecialView === "airflow") {
          const flow = climateFlowFromLiveState(live, primaryState);
          lastLiveSig = flow.sig;
        } else if (currentSpecialView === "ceiling_fan") {
          const flow = fanFlowFromLiveState(live, primaryState);
          lastLiveSig = flow.sig;
          fanTargetSpeed01 = flow.speed01;
          fanOn = flow.active;
        } else {
          lastLiveSig = primaryState.trim().toLowerCase();
        }
        applyNeonFromState(primaryState, live);
        if (currentSpecialView === "model") {
          const boolState = watchedEntity ? boolStateForDomain(watchedDomain, primaryState) : null;
          model3d.setAnimated(boolState === true);
        }
        if (currentSpecialView === "ceiling_fan") {
          fanAccentMaterial.emissiveIntensity = fanOn ? 0.12 + 0.75 * fanTargetSpeed01 : 0.0;
          fanBlurMaterial.opacity = fanOn ? clamp((fanTargetSpeed01 - 0.35) / 0.65, 0, 1) * 0.18 : 0.0;
        } else {
          fanAccentMaterial.emissiveIntensity = 0.0;
          fanBlurMaterial.opacity = 0.0;
        }
      }

      apply(element);

      return {
        object: group,
        update: apply,
        tick: (dt: number) => {
          airflow.tick(dt);
          model3d.tick(dt);
          if (watchedServer && watchedEntity) {
            const live = getHomeAssistantLiveState(watchedServer, watchedEntity);
            const next = readString(live?.state).trim().toLowerCase();
            let nextSig = next;
            let nextFanFlow: FanFlow | null = null;
            if (currentSpecialView === "airflow") {
              nextSig = climateFlowFromLiveState(live, next).sig;
            } else if (currentSpecialView === "ceiling_fan") {
              nextFanFlow = fanFlowFromLiveState(live, next);
              nextSig = nextFanFlow.sig;
            }
            if (next && nextSig !== lastLiveSig) {
              lastLiveSig = nextSig;
              applyNeonFromState(next, live);
              if (currentSpecialView === "model") {
                const boolState = watchedEntity ? boolStateForDomain(watchedDomain, next) : null;
                model3d.setAnimated(boolState === true);
              }
              if (currentSpecialView === "ceiling_fan") {
                const flow = nextFanFlow ?? fanFlowFromLiveState(live, next);
                fanTargetSpeed01 = flow.speed01;
                fanOn = flow.active;
              }
            }
          }

          if (currentSpecialView === "ceiling_fan" && fanMount.visible) {
            const d = Math.max(0.001, Math.min(0.05, dt));
            const target = fanOn ? fanTargetSpeed01 : 0;
            const accel = 1 - Math.exp(-d * 7);
            fanSpeed01 += (target - fanSpeed01) * accel;

            const maxRps = 2.4;
            fanSpinY += fanSpeed01 * maxRps * Math.PI * 2 * d;
            fanRotor.rotation.y = fanSpinY;

            const blur = clamp((fanSpeed01 - 0.35) / 0.65, 0, 1);
            fanBlurMaterial.opacity = fanOn ? blur * 0.18 : 0.0;
            fanAccentMaterial.emissiveIntensity = fanOn ? 0.12 + 0.75 * fanSpeed01 + Math.sin(fanSpinY * 0.15) * 0.03 : 0.0;
          }

          if (wantedIconKey === currentIconKey) return;
          if (!isFontAwesomeSolidIconAvailable(wantedIconKey)) return;
          const entry = getIconGeometry(wantedIconKey);
          if (entry.key === currentIconKey) return;
          currentIconKey = entry.key;
          iconMesh.geometry = entry.geometry;
          iconMesh.scale.setScalar(entry.scale);
        },
        dispose: () => {
          unwatch?.();
          domeFloorGeometry.dispose();
          domeCeilingGeometry.dispose();
          topCapGeometry.dispose();
          bottomCapGeometry.dispose();
          sphereMaterial.dispose();
          cutMaterial.dispose();
          iconMaterial.dispose();
          airflowWallVentGeometry.dispose();
          airflowCassetteGeometry.dispose();
          airflowVentMaterial.dispose();
          canopyGeometry.dispose();
          downrodGeometry.dispose();
          motorGeometry.dispose();
          motorRingGeometry.dispose();
          lightRingGeometry.dispose();
          hubGeometry.dispose();
          capGeometry.dispose();
          bladeGeometry.dispose();
          blurDiscGeometry.dispose();
          fanMetalMaterial.dispose();
          fanBodyMaterial.dispose();
          fanBladeMaterial.dispose();
          fanAccentMaterial.dispose();
          fanBlurMaterial.dispose();
          model3d.dispose();
          airflow.dispose();
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const p = readRecord(element.props);
      const specialView = readHomeAssistantSpecialView(p.special_view);
      const primaryEntityId = readString(p.primary_entity_id).trim();
      const serverId = readString(p.server_id).trim();
      const live = serverId && primaryEntityId ? getHomeAssistantLiveState(serverId, primaryEntityId) : null;
      const primaryState = readString(live?.state ?? p.primary_state).trim().toLowerCase();
      const domain = primaryEntityId ? domainFromEntityId(primaryEntityId) : "";
      const isToggle = primaryEntityId ? isToggleDomain(domain) : false;
      const boolState = primaryEntityId ? boolStateForDomain(domain, primaryState) : null;

      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const radius = 11;

      const fill = isToggle
        ? boolState === true
          ? "rgba(34,197,94,0.22)"
          : boolState === false
            ? "rgba(239,68,68,0.18)"
            : "rgba(56,189,248,0.14)"
        : "rgba(56,189,248,0.14)";
      const stroke = isToggle
        ? boolState === true
          ? "rgba(34,197,94,0.72)"
          : boolState === false
            ? "rgba(239,68,68,0.72)"
            : "rgba(230,232,242,0.24)"
        : "rgba(230,232,242,0.24)";

      if (specialView === "model") {
        const modelProps = readRecord(p.model3d);
        const dir = readString(modelProps.dir).trim();
        const preview = readString(modelProps.preview).trim();
        const previewUrl = dir && preview ? `/files/${encodeURIComponent(dir)}/${encodeURIComponent(preview)}` : "";
        const size = readModelVector3(modelProps.size, { x: 1, y: 1, z: 1 });
        const scale = readModelScale(modelProps.scale, 1);
        const rotationY = element.rotation?.y ?? 0;

        const widthPx = Math.max(20, size.x * scale * viewport.scale);
        const heightPx = Math.max(20, size.z * scale * viewport.scale);

        ctx.save();
        ctx.translate(center.x, center.y);
        ctx.rotate(-rotationY);

        if (previewUrl) {
          let image = modelPreviewImageCache.get(previewUrl) ?? null;
          if (!image) {
            image = new Image();
            image.decoding = "async";
            image.onload = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
            image.onerror = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
            image.src = previewUrl;
            modelPreviewImageCache.set(previewUrl, image);
          }

          if (image.complete && image.naturalWidth > 0) {
            ctx.globalAlpha = 0.94;
            ctx.drawImage(image, -widthPx / 2, -heightPx / 2, widthPx, heightPx);
            ctx.globalAlpha = 1;
          } else {
            ctx.fillStyle = fill;
            ctx.fillRect(-widthPx / 2, -heightPx / 2, widthPx, heightPx);
          }
        } else {
          ctx.fillStyle = fill;
          ctx.fillRect(-widthPx / 2, -heightPx / 2, widthPx, heightPx);
        }

        ctx.strokeStyle = stroke;
        ctx.lineWidth = 2;
        ctx.strokeRect(-widthPx / 2, -heightPx / 2, widthPx, heightPx);

        ctx.fillStyle = "rgba(230,232,242,0.92)";
        ctx.font = "700 11px system-ui, -apple-system, Segoe UI, Roboto, Arial";
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.fillText("HA", 0, -heightPx / 2 - 3);

        ctx.restore();
        return;
      }

      if (specialView === "ceiling_fan") {
        const rotationY = element.rotation?.y ?? 0;
        const rPx = Math.max(18, CEILING_FAN_RADIUS_WORLD * viewport.scale);

        ctx.save();
        ctx.translate(center.x, center.y);
        ctx.rotate(-rotationY);

        ctx.beginPath();
        ctx.arc(0, 0, rPx, 0, Math.PI * 2);
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.strokeStyle = stroke;
        ctx.lineWidth = 2;
        ctx.stroke();

        for (let i = 0; i < CEILING_FAN_BLADE_COUNT; i += 1) {
          ctx.save();
          ctx.rotate((i / CEILING_FAN_BLADE_COUNT) * Math.PI * 2);
          ctx.fillStyle = "rgba(230,232,242,0.12)";
          ctx.strokeStyle = "rgba(230,232,242,0.18)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.rect(rPx * 0.15, -rPx * 0.10, rPx * 0.72, rPx * 0.20);
          ctx.fill();
          ctx.stroke();
          ctx.restore();
        }

        ctx.beginPath();
        ctx.arc(0, 0, Math.max(6, rPx * 0.14), 0, Math.PI * 2);
        ctx.fillStyle = "rgba(230,232,242,0.25)";
        ctx.fill();

        ctx.fillStyle = "rgba(230,232,242,0.92)";
        ctx.font = "700 11px system-ui, -apple-system, Segoe UI, Roboto, Arial";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("HA", 0, 0);

        ctx.restore();
        return;
      }

      ctx.save();
      ctx.translate(center.x, center.y);
      ctx.beginPath();
      ctx.arc(0, 0, radius, 0, Math.PI * 2);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = "rgba(230,232,242,0.92)";
      ctx.font = "700 11px system-ui, -apple-system, Segoe UI, Roboto, Arial";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("HA", 0, 0);
      ctx.restore();
    },
    hitTest2D: ({ element, world }) => {
      const p = readRecord(element.props);
      const specialView = readHomeAssistantSpecialView(p.special_view);
      if (specialView === "model") {
        const modelProps = readRecord(p.model3d);
        const size = readModelVector3(modelProps.size, { x: 1, y: 1, z: 1 });
        const scale = readModelScale(modelProps.scale, 1);
        const angle = element.rotation?.y ?? 0;
        const dx = world.x - element.position.x;
        const dz = world.z - element.position.z;
        const cos = Math.cos(angle);
        const sin = Math.sin(angle);
        const localX = dx * cos - dz * sin;
        const localZ = dx * sin + dz * cos;
        return Math.abs(localX) <= (size.x * scale) / 2 && Math.abs(localZ) <= (size.z * scale) / 2;
      }
      if (specialView === "ceiling_fan") {
        const dx = world.x - element.position.x;
        const dz = world.z - element.position.z;
        return dx * dx + dz * dz <= CEILING_FAN_RADIUS_WORLD * CEILING_FAN_RADIUS_WORLD;
      }

      const dx = world.x - element.position.x;
      const dz = world.z - element.position.z;
      return dx * dx + dz * dz <= 0.25 * 0.25;
    },
    renderActionModal: ({ element, update, close, api }) => (
      <HomeAssistantAction element={element} update={update} close={close} api={api} i18n={i18n} />
    ),
    renderEditorModal: ({ element, update, remove, close }) => (
      <HomeAssistantEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}
