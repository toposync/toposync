import React from "react";
import type * as ThreeTypes from "three";

import type { CompositionElement, ElementType, HostI18n } from "@toposync/plugin-api";

import {
  DEFAULT_IMAGE_OPACITY_OVERLAY,
  DEFAULT_IMAGE_OPACITY_TRACING,
  DEFAULT_IMAGE_WIDTH_METERS,
  IMAGE_ELEMENT_TYPE_ID,
  IMAGE_LAYER_Y,
} from "../constants";
import { clamp, readBlendMode, readImageMode, readNumber, readString } from "../parsing";
import { ImageEditorModal } from "./ImageEditorModal";

type ImageElementMode = "overlay" | "tracing";
type BlendMode = "normal" | "multiply";

type ImageProps = {
  dir: string;
  file: string;
  width_m: number;
  depth_m: number;
  opacity: number;
  mode: ImageElementMode;
  blend: BlendMode;
  pixel_width?: number;
  pixel_height?: number;
};

function imageUrl(props: { dir: string; file: string }): string {
  return `/files/${encodeURIComponent(props.dir)}/${encodeURIComponent(props.file)}`;
}

function parseImageProps(props: Record<string, unknown>): ImageProps {
  const dir = readString(props["dir"], "");
  const file = readString(props["file"], "");
  const mode = readImageMode(props["mode"], "overlay");
  const opacityFallback = mode === "tracing" ? DEFAULT_IMAGE_OPACITY_TRACING : DEFAULT_IMAGE_OPACITY_OVERLAY;

  const width = readNumber(props["width_m"], DEFAULT_IMAGE_WIDTH_METERS);
  const depth = readNumber(props["depth_m"], DEFAULT_IMAGE_WIDTH_METERS);
  const opacity = clamp(readNumber(props["opacity"], opacityFallback), 0, 1);
  const blendFallback: BlendMode = mode === "tracing" ? "multiply" : "normal";
  const blend = readBlendMode(props["blend"], blendFallback);
  const pixelWidth = readNumber(props["pixel_width"], NaN);
  const pixelHeight = readNumber(props["pixel_height"], NaN);

  return {
    dir,
    file,
    width_m: clamp(width, 0.05, 200),
    depth_m: clamp(depth, 0.05, 200),
    opacity,
    mode,
    blend,
    pixel_width: Number.isFinite(pixelWidth) ? pixelWidth : undefined,
    pixel_height: Number.isFinite(pixelHeight) ? pixelHeight : undefined,
  };
}

export function createImageElementType(i18n: HostI18n): ElementType {
  const imageCache = new Map<string, HTMLImageElement>();

  return {
    type: IMAGE_ELEMENT_TYPE_ID,
    layerGroup: "background",
    placeable: false,
    name: { key: "ext.images.element.name", fallback: "Image" },
    description: { key: "ext.images.element.desc" },
    defaultProps: {
      dir: "",
      file: "",
      width_m: DEFAULT_IMAGE_WIDTH_METERS,
      depth_m: DEFAULT_IMAGE_WIDTH_METERS,
      opacity: DEFAULT_IMAGE_OPACITY_OVERLAY,
      mode: "overlay",
      blend: "normal",
      pixel_width: null,
      pixel_height: null,
    },
    create3D: ({ THREE }, element) => {
      const group = new THREE.Group();
      const geometry = new THREE.PlaneGeometry(1, 1, 1, 1);
      geometry.rotateX(-Math.PI / 2);

      const material = new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: 1,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.y = IMAGE_LAYER_Y;
      mesh.raycast = () => undefined;
      group.add(mesh);

      const loader = new THREE.TextureLoader();
      let currentTexture: ThreeTypes.Texture | null = null;
      let lastUrl = "";
      let disposed = false;
      let token = 0;

      async function loadTexture(url: string) {
        const myToken = ++token;
        try {
          const tex = await loader.loadAsync(url);
          if (disposed || myToken !== token) {
            tex.dispose();
            return;
          }

          tex.colorSpace = THREE.SRGBColorSpace;

          if (currentTexture) currentTexture.dispose();
          currentTexture = tex;
          material.map = tex;
          material.needsUpdate = true;
        } catch (err) {
          console.warn("[images:create3D] texture load failed", err);
        }
      }

      function apply(el: CompositionElement) {
        const p = parseImageProps(el.props);
        mesh.scale.set(p.width_m, 1, p.depth_m);
        const hasImage = Boolean(p.dir && p.file);
        const shouldRenderIn3D = hasImage && p.mode === "overlay";
        mesh.visible = shouldRenderIn3D;

        if (!shouldRenderIn3D) {
          lastUrl = "";
          token++;
          if (currentTexture) {
            currentTexture.dispose();
            currentTexture = null;
          }
          if (material.map) {
            material.map = null;
            material.needsUpdate = true;
          }
          return;
        }

        const blending =
          p.mode === "tracing" && p.blend === "multiply" ? THREE.MultiplyBlending : THREE.NormalBlending;

        const opacityTransparent = p.opacity < 0.999;
        const nextTransparent = hasImage || opacityTransparent || blending !== THREE.NormalBlending;
        const needsUpdate = material.blending !== blending || material.transparent !== nextTransparent;
        material.blending = blending;
        material.transparent = nextTransparent;
        material.opacity = p.opacity;
        if (needsUpdate) material.needsUpdate = true;

        const url = imageUrl(p);
        if (url !== lastUrl) {
          lastUrl = url;
          void loadTexture(url);
        }
      }

      apply(element);

      return {
        object: group,
        update: apply,
        dispose: () => {
          disposed = true;
          if (currentTexture) currentTexture.dispose();
          geometry.dispose();
          material.dispose();
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const p = parseImageProps(element.props);

      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const widthPx = Math.max(20, p.width_m * viewport.scale);
      const depthPx = Math.max(20, p.depth_m * viewport.scale);
      const rotationY = readNumber(element.rotation.y, 0);

      const url = p.dir && p.file ? imageUrl(p) : "";
      const image =
        url && (() => {
          const existing = imageCache.get(url) ?? null;
          if (existing) return existing;
          const created = new Image();
          created.decoding = "async";
          created.onload = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
          created.onerror = () => viewport.canvas.dispatchEvent(new Event("toposync:invalidate"));
          created.src = url;
          imageCache.set(url, created);
          return created;
        })();

      ctx.save();
      ctx.translate(center.x, center.y);
      ctx.rotate(-rotationY);

      ctx.globalAlpha = p.opacity;
      if (image && image.complete && image.naturalWidth > 0) {
        ctx.drawImage(image, -widthPx / 2, -depthPx / 2, widthPx, depthPx);
      } else {
        ctx.fillStyle = "rgba(56,189,248,0.10)";
        ctx.fillRect(-widthPx / 2, -depthPx / 2, widthPx, depthPx);
      }
      ctx.globalAlpha = 1;

      ctx.strokeStyle = "rgba(230,232,242,0.22)";
      ctx.lineWidth = 2;
      ctx.strokeRect(-widthPx / 2, -depthPx / 2, widthPx, depthPx);
      ctx.restore();
    },
    hitTest2D: ({ element, world }) => {
      const p = parseImageProps(element.props);
      const angle = readNumber(element.rotation.y, 0);
      const dx = world.x - element.position.x;
      const dz = world.z - element.position.z;
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      const localX = dx * cos - dz * sin;
      const localZ = dx * sin + dz * cos;
      return Math.abs(localX) <= p.width_m / 2 && Math.abs(localZ) <= p.depth_m / 2;
    },
    translate2D: ({ element, delta }) => ({
      id: element.id,
      position: { x: element.position.x + delta.x, z: element.position.z + delta.z },
    }),
    renderEditorModal: ({ element, update, remove, close }) => (
      <ImageEditorModal element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}
