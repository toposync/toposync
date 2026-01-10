import React from "react";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

import type { CompositionElement, ElementType, HostI18n } from "@toposync/plugin-api";

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

export function createHomeAssistantElementType(i18n: HostI18n): ElementType {
  const iconGeometryCache = new Map<string, { geometry: any; scale: number }>();
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
      const cutMaterial = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
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

      let unwatch: (() => void) | null = null;
      let watchedServer = "";
      let watchedEntity = "";
      let watchedDomain = "";
      let watchedIsToggle = false;
      let lastLiveSig = "";

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
          return;
        }

        airflow.update({ active: false });

        if (canLamp) {
          const on = boolState === true;
          const unknown = boolState == null;

          const neon = on ? lampColor : 0x000000;
          sphereMaterial.emissive.set(neon);
          iconMaterial.color.set(on ? lampColor : unknown ? 0x334155 : 0x111827);
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

        if (currentSpecialView === "airflow") {
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
        } else {
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
        } else {
          lastLiveSig = primaryState.trim().toLowerCase();
        }
        applyNeonFromState(primaryState, live);
      }

      apply(element);

      return {
        object: group,
        update: apply,
        tick: (dt: number) => {
          airflow.tick(dt);
          if (watchedServer && watchedEntity) {
            const live = getHomeAssistantLiveState(watchedServer, watchedEntity);
            const next = readString(live?.state).trim().toLowerCase();
            const nextSig = currentSpecialView === "airflow" ? climateFlowFromLiveState(live, next).sig : next;
            if (next && nextSig !== lastLiveSig) {
              lastLiveSig = nextSig;
              applyNeonFromState(next, live);
            }
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
          airflow.dispose();
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const p = readRecord(element.props);
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
