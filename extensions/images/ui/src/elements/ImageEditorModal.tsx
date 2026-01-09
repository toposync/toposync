import React, { useCallback, useMemo, useRef, useState } from "react";

import type { CompositionElement, CompositionElementPatch, HostI18n } from "@toposync/plugin-api";

import { uploadToFilesDir } from "../api/filesApi";
import { DEFAULT_IMAGE_WIDTH_METERS, IMAGE_LAYER_Y } from "../constants";
import { clamp, readBlendMode, readImageMode, readNumber } from "../parsing";

type Props = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

async function readImageDimensions(file: File): Promise<{ width: number; height: number } | null> {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    return { width: img.naturalWidth, height: img.naturalHeight };
  } catch {
    return null;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function filenameStem(filename: string): string {
  const base = filename.replace(/^.*[\\/]/, "");
  const idx = base.lastIndexOf(".");
  if (idx <= 0) return base;
  return base.slice(0, idx);
}

export function ImageEditorModal({ element, update, remove, close, i18n }: Props): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const numberFormatter = useMemo(() => new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }), [locale]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [uploadState, setUploadState] = useState<{ status: "idle" | "uploading" | "error"; message?: string }>({
    status: "idle",
  });

  const props = element.props;
  const mode = readImageMode(props["mode"], "overlay");
  const blendFallback = mode === "tracing" ? "multiply" : "normal";

  const widthM = readNumber(props["width_m"], DEFAULT_IMAGE_WIDTH_METERS);
  const depthM = readNumber(props["depth_m"], DEFAULT_IMAGE_WIDTH_METERS);
  const opacity = clamp(readNumber(props["opacity"], mode === "tracing" ? 0.55 : 1), 0, 1);
  const blend = readBlendMode(props["blend"], blendFallback);

  const rotationDeg = useMemo(() => (element.rotation.y * 180) / Math.PI, [element.rotation.y]);

  const onReplace = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  return (
    <div>
      <div className="card">
        <div className="cardHeaderRow">
          <div className="cardTitle">{t("ext.images.editor.title")}</div>
          <div className="cardMeta">y={numberFormatter.format(IMAGE_LAYER_Y)}m</div>
        </div>
        <div className="cardBody">{t("ext.images.element.desc")}</div>
      </div>

      <div className="sectionDivider" />

      <div className="field">
        <div className="label">{t("core.element_editor.name")}</div>
        <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.images.editor.mode")}</div>
          <select
            className="input"
            value={mode}
            onChange={(e) => {
              const nextMode = readImageMode(e.target.value, mode);
              const nextOpacity = nextMode === "tracing" ? 0.55 : 1;
              const nextBlend = nextMode === "tracing" ? "multiply" : "normal";
              update({ props: { mode: nextMode, opacity: nextOpacity, blend: nextBlend } });
            }}
          >
            <option value="overlay">{t("ext.images.editor.mode.overlay")}</option>
            <option value="tracing">{t("ext.images.editor.mode.tracing")}</option>
          </select>
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.images.editor.opacity")}</div>
          <input
            className="input"
            type="range"
            min={0}
            max={100}
            step={1}
            value={Math.round(opacity * 100)}
            onChange={(e) => update({ props: { opacity: clamp(Number(e.target.value) / 100, 0, 1) } })}
          />
          <div className="hint">{Math.round(opacity * 100)}%</div>
        </div>
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.images.editor.width")}</div>
          <input
            className="input"
            type="number"
            inputMode="decimal"
            min={0.05}
            max={200}
            step={0.05}
            value={Number.isFinite(widthM) ? widthM : DEFAULT_IMAGE_WIDTH_METERS}
            onChange={(e) => {
              const next = Number.parseFloat(e.target.value);
              if (!Number.isFinite(next)) return;
              update({ props: { width_m: clamp(next, 0.05, 200) } });
            }}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.images.editor.depth")}</div>
          <input
            className="input"
            type="number"
            inputMode="decimal"
            min={0.05}
            max={200}
            step={0.05}
            value={Number.isFinite(depthM) ? depthM : DEFAULT_IMAGE_WIDTH_METERS}
            onChange={(e) => {
              const next = Number.parseFloat(e.target.value);
              if (!Number.isFinite(next)) return;
              update({ props: { depth_m: clamp(next, 0.05, 200) } });
            }}
          />
        </div>
      </div>

      <div className="rowWrap">
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.images.editor.rotation")}</div>
          <input
            className="input"
            type="number"
            inputMode="decimal"
            step={1}
            value={Number.isFinite(rotationDeg) ? rotationDeg : 0}
            onChange={(e) => {
              const deg = Number.parseFloat(e.target.value);
              if (!Number.isFinite(deg)) return;
              update({ rotation: { y: (deg * Math.PI) / 180 } });
            }}
          />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 160 }}>
          <div className="label">{t("ext.images.editor.blend")}</div>
          <select
            className="input"
            value={blend}
            onChange={(e) => update({ props: { blend: readBlendMode(e.target.value, blend) } })}
          >
            <option value="normal">{t("ext.images.editor.blend.normal")}</option>
            <option value="multiply">{t("ext.images.editor.blend.multiply")}</option>
          </select>
        </div>
      </div>

      <div className="rowWrap">
        <button className="chipButton" type="button" onClick={onReplace} disabled={uploadState.status === "uploading"}>
          {uploadState.status === "uploading" ? t("ext.images.editor.uploading") : t("ext.images.editor.replace")}
        </button>
      </div>

      {uploadState.status === "error" ? (
        <div className="card" style={{ borderColor: "rgba(248,113,113,0.35)" }}>
          <div className="cardBody">
            {t("ext.images.editor.failed")}
            {uploadState.message ? `: ${uploadState.message}` : ""}
          </div>
        </div>
      ) : null}

      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        style={{ position: "fixed", left: "-9999px", width: 1, height: 1 }}
        onChange={(e) => {
          const file = e.target.files?.[0] ?? null;
          if (!file) return;
          e.target.value = "";

          void (async () => {
            setUploadState({ status: "uploading" });
            try {
              const dims = await readImageDimensions(file);
              const upload = await uploadToFilesDir(file, { filename: file.name });
              const name = element.name || filenameStem(file.name) || element.name;

              const nextProps: Record<string, unknown> = {
                dir: upload.dir,
                file: upload.filename,
              };
              if (dims) {
                nextProps.pixel_width = dims.width;
                nextProps.pixel_height = dims.height;
                const aspect = dims.width > 0 ? dims.width / Math.max(1, dims.height) : null;
                if (aspect && Number.isFinite(aspect)) nextProps.depth_m = clamp(widthM / aspect, 0.05, 200);
              }

              update({ name, props: nextProps });
              setUploadState({ status: "idle" });
            } catch (err) {
              setUploadState({ status: "error", message: err instanceof Error ? err.message : String(err) });
            }
          })();
        }}
      />

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
