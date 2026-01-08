import React, { useEffect, useMemo, useState } from "react";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

import cameraSvg from "@fortawesome/fontawesome-free/svgs/solid/camera.svg";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorToolPointerEvent,
  EditorToolSession,
  ElementType,
  HostI18n,
  TopoSyncHost,
  Viewport2DContext,
} from "@toposync/plugin-api";

import { fetchCameraSnapshot, fetchCamerasIndex } from "../api/cameras_api";
import { CAMERA_ELEMENT_TYPE_ID, CONTROL_POINT_COLORS } from "../constants";
import { createDefaultControlPoints, createUniqueId, labelForIndex, readControlPoints, readRecord, readString } from "../parsing";
import type { CamerasIndex, ControlPoint } from "../types";
import { SubModal } from "../ui/sub_modal";

function roundRectPath(
  canvasContext: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const anyContext = canvasContext as unknown as {
    roundRect?: (x: number, y: number, width: number, height: number, radius: number) => void;
  };
  if (typeof anyContext.roundRect === "function") {
    anyContext.roundRect(x, y, width, height, radius);
    return;
  }

  const clampedRadius = Math.max(0, Math.min(radius, Math.min(width, height) / 2));
  canvasContext.moveTo(x + clampedRadius, y);
  canvasContext.lineTo(x + width - clampedRadius, y);
  canvasContext.quadraticCurveTo(x + width, y, x + width, y + clampedRadius);
  canvasContext.lineTo(x + width, y + height - clampedRadius);
  canvasContext.quadraticCurveTo(x + width, y + height, x + width - clampedRadius, y + height);
  canvasContext.lineTo(x + clampedRadius, y + height);
  canvasContext.quadraticCurveTo(x, y + height, x, y + height - clampedRadius);
  canvasContext.lineTo(x, y + clampedRadius);
  canvasContext.quadraticCurveTo(x, y, x + clampedRadius, y);
}

export function createCameraElementType(host: TopoSyncHost): ElementType {
  const i18n = host.i18n;
  const iconGeometryCache = new Map<string, { geometry: any; scale: number }>();
  const iconTargetSize = 0.14;

  const buttonRadius = 0.18;
  const buttonThetaTopCut = 1.05;
  const ceilingTopMargin = 0.0;

  return {
    type: CAMERA_ELEMENT_TYPE_ID,
    name: { key: "ext.cameras.element.name", fallback: "Camera" },
    description: { key: "ext.cameras.element.desc" },
    placeable: false,
    defaultProps: { camera_id: "", camera_name: "", view_mode: "ceiling" },
    render2D: ({ ctx: canvasContext, element, viewport }) => {
      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const rotation = typeof element.rotation?.y === "number" ? element.rotation.y : 0;
      const scale = viewport.scale;
      const width = Math.max(14, Math.min(32, 0.28 * scale));
      const height = Math.max(10, Math.min(26, 0.18 * scale));

      canvasContext.save();
      canvasContext.translate(center.x, center.y);
      canvasContext.rotate(rotation);
      canvasContext.fillStyle = "rgba(56,189,248,0.12)";
      canvasContext.strokeStyle = "rgba(230,232,242,0.24)";
      canvasContext.lineWidth = 2;
      canvasContext.beginPath();
      roundRectPath(canvasContext, -width / 2, -height / 2, width, height, Math.min(10, height / 2));
      canvasContext.fill();
      canvasContext.stroke();

      // Direction marker (forward = +Z in 3D, maps to +Y on canvas after rotation).
      canvasContext.fillStyle = "rgba(251,191,36,0.92)";
      canvasContext.beginPath();
      canvasContext.moveTo(0, height / 2 + 6);
      canvasContext.lineTo(-5, height / 2 - 4);
      canvasContext.lineTo(5, height / 2 - 4);
      canvasContext.closePath();
      canvasContext.fill();

      canvasContext.restore();
    },
    hitTest2D: ({ element, world }) => {
      const deltaX = world.x - element.position.x;
      const deltaZ = world.z - element.position.z;
      return deltaX * deltaX + deltaZ * deltaZ <= 0.32 * 0.32;
    },
    translate2D: ({ element, delta }) => ({
      position: { x: element.position.x + delta.x, z: element.position.z + delta.z },
    }),
    create3D: ({ THREE, view }, element) => {
      function getIconGeometry(): { geometry: any; scale: number } {
        const cached = iconGeometryCache.get("camera");
        if (cached) return cached;

        const data = new SVGLoader().parse(cameraSvg);
        const shapes: any[] = [];
        for (const path of data.paths) shapes.push(...SVGLoader.createShapes(path));

        const geometry = new THREE.ShapeGeometry(shapes);
        geometry.computeBoundingBox();
        const boundingBox = geometry.boundingBox;
        if (boundingBox) {
          const centerX = (boundingBox.min.x + boundingBox.max.x) / 2;
          const centerY = (boundingBox.min.y + boundingBox.max.y) / 2;
          geometry.translate(-centerX, -centerY, 0);
        }

        geometry.scale(1, -1, 1);
        geometry.rotateX(-Math.PI / 2);

        geometry.computeBoundingBox();
        const boundingBox3d = geometry.boundingBox;
        const sizeX = boundingBox3d ? boundingBox3d.max.x - boundingBox3d.min.x : 1;
        const sizeZ = boundingBox3d ? boundingBox3d.max.z - boundingBox3d.min.z : 1;
        const maxXZ = Math.max(sizeX, sizeZ, 1e-9);
        const scale = iconTargetSize / maxXZ;

        const entry = { geometry, scale };
        iconGeometryCache.set("camera", entry);
        return entry;
      }

      const neonColor = 0x38bdf8;

      const group = new THREE.Group();
      const mountGroup = new THREE.Group();
      group.add(mountGroup);

      const topY = buttonRadius * Math.cos(buttonThetaTopCut);
      const topRadius = buttonRadius * Math.sin(buttonThetaTopCut);

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
        emissive: new THREE.Color(neonColor),
        emissiveIntensity: 0.36,
        roughness: 0.32,
        metalness: 0.0,
      });
      const cutMaterial = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      const iconMaterial = new THREE.MeshBasicMaterial({ color: neonColor, side: THREE.DoubleSide });
      iconMaterial.depthWrite = false;
      iconMaterial.polygonOffset = true;
      iconMaterial.polygonOffsetFactor = -1;
      iconMaterial.polygonOffsetUnits = -1;

      const dome = new THREE.Mesh(domeCeilingGeometry, sphereMaterial);
      mountGroup.add(dome);

      const topCapGeometry = new THREE.CircleGeometry(topRadius, 48);
      const topCap = new THREE.Mesh(topCapGeometry, cutMaterial);
      topCap.rotation.x = -Math.PI / 2;
      topCap.position.set(0, topY, 0);
      mountGroup.add(topCap);

      const topIconGeometry = getIconGeometry();
      const topIcon = new THREE.Mesh(topIconGeometry.geometry, iconMaterial);
      topIcon.scale.setScalar(topIconGeometry.scale);
      topIcon.position.set(0, topY + 0.002, 0);
      topIcon.renderOrder = 10;
      mountGroup.add(topIcon);

      // Dome camera lens "window" on the underside, slightly angled.
      const lensCutMaterial = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      lensCutMaterial.depthWrite = false;
      lensCutMaterial.polygonOffset = true;
      lensCutMaterial.polygonOffsetFactor = -1;
      lensCutMaterial.polygonOffsetUnits = -1;

      const lensRadius = 0.055;
      const lensCutGeometry = new THREE.CircleGeometry(lensRadius, 42);
      const lensCut = new THREE.Mesh(lensCutGeometry, lensCutMaterial);
      lensCut.renderOrder = 9;
      mountGroup.add(lensCut);

      const light = new THREE.PointLight(neonColor, 0.18, 0.9, 2.2);
      light.position.set(0, buttonRadius * 0.45, 0);
      mountGroup.add(light);

      function apply() {
        // Ceiling-only for now.
        mountGroup.rotation.set(0, 0, 0);
        mountGroup.position.set(0, 0, 0);

        // Hang from ceiling: top cut flush at wallHeight.
        mountGroup.position.y = view.wallHeight - topY - ceilingTopMargin;

        const lensDirection = new THREE.Vector3(0.12, -0.72, 1).normalize();
        const lensPosition = lensDirection.clone().multiplyScalar(buttonRadius * 0.92);
        lensCut.position.copy(lensPosition);
        lensCut.lookAt(lensPosition.clone().add(lensDirection));
        lensCut.rotateZ(0.55);
        lensCut.position.add(lensDirection.clone().multiplyScalar(0.002));
      }

      apply();

      return {
        object: group,
        update: apply,
        dispose: () => {
          domeCeilingGeometry.dispose();
          topCapGeometry.dispose();
          lensCutGeometry.dispose();
          sphereMaterial.dispose();
          cutMaterial.dispose();
          iconMaterial.dispose();
          lensCutMaterial.dispose();
        },
      };
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <CameraEditor element={element} update={update} remove={remove} close={close} i18n={i18n} host={host} />
    ),
    renderActionModal: ({ element }) => <CameraAction element={element} i18n={i18n} />,
  };
}

function CameraEditor({
  element,
  update,
  remove,
  close,
  i18n,
  host,
}: {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
  host: TopoSyncHost;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const selectedCameraId = readString(props.camera_id).trim();
  const existingControlPoints = useMemo(() => readControlPoints(props.control_points), [props.control_points]);
  const completePairs = existingControlPoints.filter((point) => Boolean(point.image) && Boolean(point.world)).length;
  const totalPoints = existingControlPoints.length;
  const [isControlPointsOpen, setIsControlPointsOpen] = useState(false);

  const [camerasIndex, setCamerasIndex] = useState<CamerasIndex | null>(null);
  const [indexErrorMessage, setIndexErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIndexErrorMessage(null);
    fetchCamerasIndex()
      .then((data) => {
        if (!cancelled) setCamerasIndex(data);
      })
      .catch((error) => {
        if (!cancelled) setIndexErrorMessage(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const cameraOptions = useMemo(() => {
    const cameras = camerasIndex?.cameras ?? [];
    return cameras
      .map((camera) => ({ id: readString((camera as any).id), name: readString((camera as any).name) }))
      .filter((camera) => Boolean(camera.id));
  }, [camerasIndex]);

  return (
    <div>
      {indexErrorMessage ? (
        <div className="card">
          <div className="cardBody">{indexErrorMessage}</div>
        </div>
      ) : null}

      {cameraOptions.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.cameras.editor.no_cameras")}</div>
        </div>
      ) : (
        <div className="field">
          <label className="label">{t("ext.cameras.editor.camera")}</label>
          <select
            className="input"
            value={selectedCameraId}
            onChange={(event) => {
              const nextCameraId = event.target.value;
              const selected = cameraOptions.find((camera) => camera.id === nextCameraId) ?? null;
              update({
                name: selected?.name ?? "",
                props: { camera_id: nextCameraId, camera_name: selected?.name ?? "" },
              });
            }}
          >
            <option value="">{t("ext.cameras.editor.select_placeholder")}</option>
            {cameraOptions.map((camera) => (
              <option key={camera.id} value={camera.id}>
                {camera.name || camera.id}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="field">
        <label className="label">{t("ext.cameras.editor.control_points")}</label>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="cardMeta">
            {totalPoints > 0
              ? t("ext.cameras.editor.control_points_some", { complete: completePairs, total: totalPoints })
              : t("ext.cameras.editor.control_points_none")}
          </div>

          <button
            className="chipButton"
            type="button"
            disabled={!selectedCameraId}
            onClick={() => setIsControlPointsOpen(true)}
          >
            {t("ext.cameras.editor.control_points_open")}
          </button>
        </div>
        {totalPoints > 0 && completePairs < 4 ? (
          <div className="cardMeta" style={{ marginTop: 6 }}>
            {t("ext.cameras.control.min_points")}
          </div>
        ) : null}
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <button
          className="dangerButton"
          type="button"
          onClick={() => {
            remove();
            close();
          }}
        >
          {t("core.actions.delete")}
        </button>

        <button className="primaryButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>

      <ControlPointsModal
        open={isControlPointsOpen}
        onClose={() => setIsControlPointsOpen(false)}
        host={host}
        i18n={i18n}
        cameraId={selectedCameraId}
        initialPoints={existingControlPoints}
        onSave={(points) => update({ props: { control_points: points } })}
      />
    </div>
  );
}

function ControlPointsModal({
  open,
  onClose,
  host,
  i18n,
  cameraId,
  initialPoints,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  host: TopoSyncHost;
  i18n: HostI18n;
  cameraId: string;
  initialPoints: ControlPoint[];
  onSave: (points: ControlPoint[]) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();

  const [points, setPoints] = useState<ControlPoint[]>([]);
  const [selectedPointId, setSelectedPointId] = useState<string | null>(null);

  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);

  useEffect(() => {
    return () => {
      if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    };
  }, [snapshotUrl]);

  useEffect(() => {
    if (!open) return;
    const base = initialPoints.length ? initialPoints : createDefaultControlPoints(4);
    const padded: ControlPoint[] = base.map((point) => ({ ...point, image: point.image ?? null, world: point.world ?? null }));
    while (padded.length < 4) {
      padded.push({ id: createUniqueId(), label: labelForIndex(padded.length), image: null, world: null });
    }
    setPoints(padded);
    setSelectedPointId(padded[0]?.id ?? null);
  }, [open, initialPoints]);

  useEffect(() => {
    if (!open) {
      setSnapshotErrorMessage(null);
      setSnapshotLoading(false);
      setSnapshotUrl(null);
      return;
    }
    if (!cameraId) return;

    let cancelled = false;
    setSnapshotLoading(true);
    setSnapshotErrorMessage(null);
    fetchCameraSnapshot(cameraId)
      .then((blob) => {
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        setSnapshotUrl(url);
      })
      .catch((error) => {
        if (cancelled) return;
        setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
        setSnapshotUrl(null);
      })
      .finally(() => {
        if (!cancelled) setSnapshotLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open, cameraId]);

  const completePairs = useMemo(() => points.filter((point) => Boolean(point.image) && Boolean(point.world)).length, [points]);

  const toolSession = useMemo<EditorToolSession>(() => {
    return {
      onPointerEvent: (event: EditorToolPointerEvent) => {
        if (event.kind !== "down") return;
        if (!selectedPointId) return;
        setPoints((previous) =>
          previous.map((point) =>
            point.id === selectedPointId ? { ...point, world: { x: event.world.x, z: event.world.z } } : point,
          ),
        );
      },
      renderOverlay2D: ({
        ctx: canvasContext,
        viewport,
      }: {
        ctx: CanvasRenderingContext2D;
        viewport: Viewport2DContext;
      }) => {
        canvasContext.save();
        canvasContext.font = "700 12px system-ui, -apple-system, Segoe UI, Roboto, Arial";
        canvasContext.textAlign = "center";
        canvasContext.textBaseline = "middle";

        for (let index = 0; index < points.length; index += 1) {
          const point = points[index];
          if (!point.world) continue;
          const color = CONTROL_POINT_COLORS[index % CONTROL_POINT_COLORS.length];
          const screen = viewport.worldToScreen(point.world);
          const isSelected = selectedPointId === point.id;
          const radius = isSelected ? 10 : 8;

          canvasContext.beginPath();
          canvasContext.arc(screen.x, screen.y, radius, 0, Math.PI * 2);
          canvasContext.fillStyle = color;
          canvasContext.fill();
          canvasContext.lineWidth = 2;
          canvasContext.strokeStyle = isSelected ? "rgba(255,255,255,0.92)" : "rgba(0,0,0,0.65)";
          canvasContext.stroke();

          canvasContext.fillStyle = "rgba(0,0,0,0.82)";
          canvasContext.fillText(point.label || labelForIndex(index), screen.x, screen.y + 0.5);
        }

        canvasContext.restore();
      },
      getCursor: () => "crosshair",
    };
  }, [points, selectedPointId]);

  function addPoint() {
    const id = createUniqueId();
    setPoints((previous) => [...previous, { id, label: labelForIndex(previous.length), image: null, world: null }]);
    setSelectedPointId(id);
  }

  function setImagePointFromEvent(event: React.MouseEvent<HTMLImageElement>) {
    if (!selectedPointId) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const normalizedX = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
    const normalizedY = Math.max(0, Math.min(1, (event.clientY - rect.top) / Math.max(1, rect.height)));
    setPoints((previous) =>
      previous.map((point) => (point.id === selectedPointId ? { ...point, image: { x: normalizedX, y: normalizedY } } : point)),
    );
  }

  return (
    <SubModal
      open={open}
      onClose={onClose}
      title={t("ext.cameras.control.title")}
      panelStyle={{
        width: "min(1440px, calc(100vw - 28px))",
        height: "calc(100vh - 28px)",
        maxHeight: "calc(100vh - 28px)",
      }}
      bodyStyle={{
        padding: 0,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 12, flex: 1, minHeight: 0 }}>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="rowWrap" style={{ gap: 8 }}>
            {points.map((point, index) => {
              const isSelected = selectedPointId === point.id;
              const color = CONTROL_POINT_COLORS[index % CONTROL_POINT_COLORS.length];
              const hasImage = Boolean(point.image);
              const hasWorld = Boolean(point.world);
              return (
                <button
                  key={point.id}
                  type="button"
                  className="chipButton"
                  onClick={() => setSelectedPointId(point.id)}
                  style={{
                    minWidth: 46,
                    justifyContent: "center",
                    borderColor: isSelected ? "rgba(56,189,248,0.55)" : "rgba(255,255,255,0.14)",
                    background: isSelected ? "rgba(56,189,248,0.10)" : undefined,
                  }}
                  aria-label={`Point ${point.label || labelForIndex(index)}`}
                >
                  <span
                    aria-hidden="true"
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: 999,
                      background: color,
                      boxShadow: "0 0 0 2px rgba(0,0,0,0.25)",
                      opacity: hasImage && hasWorld ? 1 : 0.4,
                    }}
                  />
                  <span>{point.label || labelForIndex(index)}</span>
                </button>
              );
            })}

            <button className="iconButton" type="button" onClick={addPoint} aria-label={t("core.actions.add")}>
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>
          </div>

          <div className="cardMeta" style={{ textAlign: "right" }}>
            {t("ext.cameras.control.help")}
            {completePairs > 0 && completePairs < 4 ? ` ${t("ext.cameras.control.min_points")}` : ""}
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, flex: 1, minHeight: 0 }}>
          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="label">{t("ext.cameras.control.image")}</div>
            <div
              style={{
                flex: 1,
                minHeight: 0,
                borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.14)",
                background: "rgba(0,0,0,0.30)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: 10,
                overflow: "hidden",
              }}
            >
              {snapshotErrorMessage ? (
                <div className="card">
                  <div className="cardBody">{snapshotErrorMessage}</div>
                </div>
              ) : snapshotUrl ? (
                <div style={{ position: "relative", display: "inline-block", maxWidth: "100%", maxHeight: "100%" }}>
                  <img
                    src={snapshotUrl}
                    alt={t("ext.cameras.control.image")}
                    style={{
                      display: "block",
                      maxWidth: "100%",
                      maxHeight: "100%",
                      borderRadius: 14,
                      border: "1px solid rgba(255,255,255,0.10)",
                    }}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      setImagePointFromEvent(event);
                    }}
                  />

                  {points.map((point, index) => {
                    if (!point.image) return null;
                    const isSelected = selectedPointId === point.id;
                    const color = CONTROL_POINT_COLORS[index % CONTROL_POINT_COLORS.length];
                    return (
                      <div
                        key={point.id}
                        style={{
                          position: "absolute",
                          left: `${point.image.x * 100}%`,
                          top: `${point.image.y * 100}%`,
                          transform: "translate(-50%,-50%)",
                          width: isSelected ? 22 : 20,
                          height: isSelected ? 22 : 20,
                          borderRadius: 999,
                          background: color,
                          border: isSelected ? "2px solid rgba(255,255,255,0.92)" : "2px solid rgba(0,0,0,0.65)",
                          boxShadow: "0 8px 18px rgba(0,0,0,0.28)",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          fontSize: 12,
                          fontWeight: 800,
                          color: "rgba(0,0,0,0.82)",
                          pointerEvents: "none",
                        }}
                      >
                        {point.label || labelForIndex(index)}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="card">
                  <div className="cardBody">
                    {snapshotLoading ? t("ext.cameras.control.loading") : t("ext.cameras.control.image")}
                  </div>
                </div>
              )}
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="label">{t("ext.cameras.control.canvas")}</div>
            <div
              style={{
                flex: 1,
                minHeight: 0,
                borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.14)",
                background: "rgba(0,0,0,0.30)",
                overflow: "hidden",
              }}
            >
              <host.ui.Viewport2DReplica session={toolSession} style={{ width: "100%", height: "100%" }} />
            </div>
          </div>
        </div>

        <div className="rowWrap" style={{ justifyContent: "space-between" }}>
          <button className="chipButton" type="button" onClick={onClose}>
            {t("core.actions.cancel")}
          </button>
          <button
            className="primaryButton"
            type="button"
            onClick={() => {
              onSave(points);
              onClose();
            }}
          >
            {t("core.actions.save")}
          </button>
        </div>
      </div>
    </SubModal>
  );
}

function CameraAction({ element, i18n }: { element: CompositionElement; i18n: HostI18n }): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = readRecord(element.props);
  const cameraId = readString(props.camera_id).trim();

  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    setErrorMessage(null);
    fetchCameraSnapshot(cameraId)
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        setImageUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return url;
        });
      })
      .catch((error) => {
        setErrorMessage(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        setLoading(false);
      });
  };

  useEffect(() => {
    if (!cameraId) return;
    refresh();
    return () => {
      setImageUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId]);

  if (!cameraId) {
    return <div className="cardBody">{t("ext.cameras.action.no_camera")}</div>;
  }

  return (
    <div>
      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <div className="label">{readString(props.camera_name) || cameraId}</div>
        <button className="chipButton" type="button" onClick={refresh} disabled={loading}>
          {loading ? t("ext.cameras.action.loading") : t("ext.cameras.action.refresh")}
        </button>
      </div>

      <div className="sectionDivider" />

      {errorMessage ? (
        <div className="card">
          <div className="cardBody">{errorMessage}</div>
        </div>
      ) : imageUrl ? (
        <img
          src={imageUrl}
          alt={readString(props.camera_name) || cameraId}
          style={{
            width: "100%",
            borderRadius: 14,
            border: "1px solid rgba(255,255,255,0.14)",
            background: "rgba(0,0,0,0.35)",
          }}
        />
      ) : (
        <div className="card">
          <div className="cardBody">{t("ext.cameras.action.loading")}</div>
        </div>
      )}
    </div>
  );
}

