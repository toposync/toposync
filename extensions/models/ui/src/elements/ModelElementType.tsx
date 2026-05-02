import React, { useMemo } from "react";

import { resolveToposyncUrl, type BoundsXZ, type CompositionElement, type CompositionElementPatch, type ElementType, type HostI18n } from "@toposync/plugin-api";

import { MAXIMUM_MODEL_SCALE, MINIMUM_MODEL_SCALE, MODEL_ELEMENT_TYPE_ID } from "../constants";
import { clamp, readNumber, readScale, readString, readVector3 } from "../parsing";
import { createGltfModelRuntime } from "../runtime/gltfModel";
import type { Vector3 } from "../types";

function readBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function modelBounds(element: CompositionElement): BoundsXZ {
  const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
  const scale = readScale((element.props as any).scale, 1);
  const angle = readNumber((element.rotation as any)?.y, 0);
  const halfX = (size.x * scale) / 2;
  const halfZ = (size.z * scale) / 2;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  const xs: number[] = [];
  const zs: number[] = [];
  for (const corner of [
    { x: -halfX, z: -halfZ },
    { x: halfX, z: -halfZ },
    { x: halfX, z: halfZ },
    { x: -halfX, z: halfZ },
  ]) {
    xs.push(element.position.x + corner.x * cos - corner.z * sin);
    zs.push(element.position.z + corner.x * sin + corner.z * cos);
  }
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minZ: Math.min(...zs), maxZ: Math.max(...zs) };
}

export function createModelElementType(i18n: HostI18n): ElementType {
  const imageCache = new Map<string, HTMLImageElement>();

  function getPreviewUrl(element: CompositionElement): string | null {
    const dir = readString((element.props as any).dir, "");
    const preview = readString((element.props as any).preview, "");
    if (!dir || !preview) return null;
    return resolveToposyncUrl(`/files/${encodeURIComponent(dir)}/${encodeURIComponent(preview)}`);
  }

  return {
    type: MODEL_ELEMENT_TYPE_ID,
    layerGroup: "objects",
    placeable: false,
    name: { key: "ext.models.element.name", fallback: "3D Model" },
    description: { key: "ext.models.element.desc", fallback: "GLB/GLTF model placed in the scene." },
    defaultProps: {
      dir: "",
      model: "",
      preview: "",
      size: { x: 1, y: 1, z: 1 },
      center: { x: 0, y: 0, z: 0 },
      min_y: 0,
      scale: 1,
      animation_enabled: false,
    },
    getMain2DBounds: modelBounds,
    renderMain2DVector: ({ element }) => {
      const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
      const scale = readScale((element.props as any).scale, 1);
      const previewUrl = getPreviewUrl(element);
      const rotationDeg = (readNumber((element.rotation as any)?.y, 0) * -180) / Math.PI;
      const width = Math.max(0.2, size.x * scale);
      const height = Math.max(0.2, size.z * scale);
      const animated = readBoolean((element.props as any).animation_enabled, false);
      const accent = animated ? "rgba(56,189,248,0.42)" : "rgba(226,232,240,0.22)";
      return (
        <g className="mainVector2dModel" transform={`translate(${element.position.x} ${element.position.z}) rotate(${rotationDeg})`} opacity={0.96} filter="url(#mainVector2dSoftShadow)">
          <rect x={-width / 2} y={-height / 2} width={width} height={height} rx={0.05} fill="rgba(2,6,23,0.12)" />
          {previewUrl ? (
            <image href={previewUrl} x={-width / 2} y={-height / 2} width={width} height={height} preserveAspectRatio="none" />
          ) : (
            <rect x={-width / 2} y={-height / 2} width={width} height={height} rx={0.05} fill="rgba(56,189,248,0.08)" />
          )}
          <rect
            x={-width / 2}
            y={-height / 2}
            width={width}
            height={height}
            rx={0.05}
            fill="none"
            stroke={accent}
            strokeWidth={0.018}
            vectorEffect="non-scaling-stroke"
          />
          {animated ? <circle cx={width / 2 - 0.07} cy={-height / 2 + 0.07} r={0.025} fill="rgba(56,189,248,0.72)" /> : null}
        </g>
      );
    },
    create3D: ({ THREE, requestRender }, element) => {
      const runtime = createGltfModelRuntime(THREE, { autoplay: false, onInvalidate: requestRender });
      runtime.updateFromProps(element.props);
      runtime.setAnimated(readBoolean((element.props as any).animation_enabled, false));
      return {
        object: runtime.object,
        update: (el) => {
          runtime.updateFromProps(el.props);
          runtime.setAnimated(readBoolean((el.props as any).animation_enabled, false));
        },
        tick: runtime.tick,
        dispose: runtime.dispose,
      };
    },
    render2D: ({ ctx: canvasContext, element, viewport }) => {
      const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
      const scale = readScale((element.props as any).scale, 1);
      const previewUrl = getPreviewUrl(element);
      const rotationY = readNumber((element.rotation as any)?.y, 0);

      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const widthPx = Math.max(20, size.x * scale * viewport.scale);
      const heightPx = Math.max(20, size.z * scale * viewport.scale);

      canvasContext.save();
      canvasContext.translate(center.x, center.y);
      canvasContext.rotate(-rotationY);

      if (previewUrl) {
        let image = imageCache.get(previewUrl) ?? null;
        if (!image) {
          image = new Image();
          image.decoding = "async";
          image.onload = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
          image.onerror = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
          image.src = previewUrl;
          imageCache.set(previewUrl, image);
        }

        if (image.complete && image.naturalWidth > 0) {
          canvasContext.globalAlpha = 0.94;
          canvasContext.drawImage(image, -widthPx / 2, -heightPx / 2, widthPx, heightPx);
          canvasContext.globalAlpha = 1;
        } else {
          canvasContext.fillStyle = "rgba(56,189,248,0.10)";
          canvasContext.fillRect(-widthPx / 2, -heightPx / 2, widthPx, heightPx);
        }
      } else {
        canvasContext.fillStyle = "rgba(56,189,248,0.10)";
        canvasContext.fillRect(-widthPx / 2, -heightPx / 2, widthPx, heightPx);
      }

      canvasContext.strokeStyle = "rgba(230,232,242,0.22)";
      canvasContext.lineWidth = 2;
      canvasContext.strokeRect(-widthPx / 2, -heightPx / 2, widthPx, heightPx);
      canvasContext.restore();
    },
    hitTest2D: ({ element, world }) => {
      const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
      const scale = readScale((element.props as any).scale, 1);
      const angle = readNumber((element.rotation as any)?.y, 0);
      const dx = world.x - element.position.x;
      const dz = world.z - element.position.z;
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      const localX = dx * cos - dz * sin;
      const localZ = dx * sin + dz * cos;
      return Math.abs(localX) <= (size.x * scale) / 2 && Math.abs(localZ) <= (size.z * scale) / 2;
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <ModelEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

type ModelEditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

function ModelEditor({ element, update, remove, close, i18n }: ModelEditorProps): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const numberFormatter = useMemo(
    () => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    [locale],
  );

  const directory = readString((element.props as any).dir, "");
  const modelFilename = readString((element.props as any).model, "");
  const previewFilename = readString((element.props as any).preview, "");
  const size = readVector3((element.props as any).size, { x: 1, y: 1, z: 1 });
  const scale = readScale((element.props as any).scale, 1);
  const animationEnabled = readBoolean((element.props as any).animation_enabled, false);
  const heightMeters = readNumber((element.position as any).y, 0);

  const previewUrl =
    directory && previewFilename
      ? resolveToposyncUrl(`/files/${encodeURIComponent(directory)}/${encodeURIComponent(previewFilename)}`)
      : "";
  const finalSize = useMemo(
    () => ({ x: size.x * scale, y: size.y * scale, z: size.z * scale }),
    [scale, size.x, size.y, size.z],
  );

  return (
    <div>
      <div className="field">
        <div className="label">{t("core.element_editor.name")}</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 180 }}>
          <div className="label">{t("ext.models.editor.file")}</div>
          <input className="input" value={modelFilename || "-"} readOnly />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 180 }}>
          <div className="label">{t("ext.models.editor.size")}</div>
          <input
            className="input"
            value={`${numberFormatter.format(finalSize.x)} × ${numberFormatter.format(finalSize.y)} × ${numberFormatter.format(finalSize.z)} m`}
            readOnly
          />
        </div>
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 180 }}>
          <div className="label">{t("ext.models.editor.scale")}</div>
          <input
            className="input"
            type="number"
            inputMode="decimal"
            min={MINIMUM_MODEL_SCALE}
            max={MAXIMUM_MODEL_SCALE}
            step={0.01}
            value={scale}
            onChange={(e) => {
              const next = Number.parseFloat(e.target.value);
              if (!Number.isFinite(next)) return;
              update({ props: { scale: clamp(next, MINIMUM_MODEL_SCALE, MAXIMUM_MODEL_SCALE) } });
            }}
          />
        </div>
      </div>

      <label className="chipButton" style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
        <input
          type="checkbox"
          checked={animationEnabled}
          onChange={(event) => update({ props: { animation_enabled: event.target.checked } })}
        />
        <span>{t("ext.models.editor.play_animations")}</span>
      </label>

      <div className="field">
        <div className="label">
          {t("ext.models.editor.height")}: {numberFormatter.format(heightMeters)} m
        </div>
        <div className="rowWrap">
          {(
            [
              { key: "floor", y: 0 },
              { key: "mid", y: 1.35 },
              { key: "ceiling", y: 2.7 },
            ] as const
          ).map((preset) => {
            const isActive = Math.abs(heightMeters - preset.y) < 0.01;
            return (
              <button
                key={preset.key}
                className={["chipButton", isActive ? "isActive" : ""].join(" ")}
                type="button"
                onClick={() => update({ position: { y: preset.y } })}
              >
                {t(`ext.models.editor.height.${preset.key}`)}
              </button>
            );
          })}
        </div>
        <input
          className="input"
          type="range"
          min={0}
          max={3}
          step={0.01}
          value={heightMeters}
          onChange={(e) => update({ position: { y: Number(e.target.value) } })}
        />
      </div>

      {previewUrl ? (
        <>
          <div className="sectionDivider" />
          <div className="card">
            <div className="cardHeaderRow">
              <div className="cardTitle">{t("ext.models.editor.preview")}</div>
              <div className="cardMeta">{directory}</div>
            </div>
            <div className="cardBody">
              <img
                src={previewUrl}
                alt={t("ext.models.editor.preview")}
                style={{ width: "100%", borderRadius: 12, border: "1px solid rgba(255,255,255,0.10)" }}
              />
            </div>
          </div>
        </>
      ) : null}

      <div className="sectionDivider" />
      <div className="rowWrap">
        <button className="dangerButton" type="button" onClick={remove}>
          {t("core.actions.delete")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}
