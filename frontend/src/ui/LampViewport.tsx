import React, { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";

import type { Overlay3DContribution } from "@toposync/plugin-api";

type Props = {
  overlays: Overlay3DContribution[];
  lampOn: boolean;
  onLampClicked: () => void | Promise<void>;
};

export function LampViewport({ overlays, lampOn, onLampClicked }: Props): React.ReactElement {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const lampRef = useRef<THREE.Mesh<THREE.BoxGeometry, THREE.MeshStandardMaterial> | null>(null);
  const overlayCleanupRef = useRef<Map<string, () => void>>(new Map());

  const raycaster = useMemo(() => new THREE.Raycaster(), []);
  const mouse = useMemo(() => new THREE.Vector2(), []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setClearColor(0x0b1020, 1);
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(2, 4, 3);
    scene.add(dirLight);

    const camera = new THREE.PerspectiveCamera(65, 1, 0.1, 100);
    camera.position.set(0, 1.2, 3.2);

    const floorGeo = new THREE.PlaneGeometry(10, 10);
    const floorMat = new THREE.MeshStandardMaterial({ color: 0x141a2d, roughness: 1 });
    const floor = new THREE.Mesh(floorGeo, floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -0.65;
    scene.add(floor);

    const lampGeo = new THREE.BoxGeometry(0.6, 0.6, 0.6);
    const lampMat = new THREE.MeshStandardMaterial({ color: 0x334155, emissive: 0x000000 });
    const lamp = new THREE.Mesh(lampGeo, lampMat);
    lamp.position.set(0, 0, 0);
    lamp.name = "lamp1";
    scene.add(lamp);

    rendererRef.current = renderer;
    cameraRef.current = camera;
    sceneRef.current = scene;
    lampRef.current = lamp;

    function resize() {
      const w = container.clientWidth;
      const h = container.clientHeight;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(container);

    let raf = 0;
    const clock = new THREE.Clock();

    function animate() {
      raf = requestAnimationFrame(animate);
      const t = clock.getElapsedTime();
      lamp.rotation.y = t * 0.4;
      renderer.render(scene, camera);
    }

    animate();

    async function handleClick(e: MouseEvent) {
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -(((e.clientY - rect.top) / rect.height) * 2 - 1);

      raycaster.setFromCamera(mouse, camera);
      const hits = raycaster.intersectObjects([lamp], false);
      if (hits.length > 0) await onLampClicked();
    }

    renderer.domElement.addEventListener("click", handleClick);

    return () => {
      for (const cleanup of overlayCleanupRef.current.values()) cleanup();
      overlayCleanupRef.current.clear();

      renderer.domElement.removeEventListener("click", handleClick);
      ro.disconnect();
      cancelAnimationFrame(raf);
      renderer.dispose();
      floorGeo.dispose();
      floorMat.dispose();
      lampGeo.dispose();
      lampMat.dispose();
      container.removeChild(renderer.domElement);
      rendererRef.current = null;
      cameraRef.current = null;
      sceneRef.current = null;
      lampRef.current = null;
    };
  }, [mouse, onLampClicked, raycaster]);

  useEffect(() => {
    const renderer = rendererRef.current;
    const camera = cameraRef.current;
    const scene = sceneRef.current;
    if (!renderer || !camera || !scene) return;

    const cleanupById = overlayCleanupRef.current;
    const wanted = new Set(overlays.map((o) => o.id));

    for (const overlay of overlays) {
      if (cleanupById.has(overlay.id)) continue;
      const maybeCleanup = overlay.mount({ THREE, scene, camera, renderer });
      cleanupById.set(overlay.id, typeof maybeCleanup === "function" ? maybeCleanup : () => {});
    }

    for (const [id, cleanup] of cleanupById.entries()) {
      if (wanted.has(id)) continue;
      cleanup();
      cleanupById.delete(id);
    }
  }, [overlays]);

  useEffect(() => {
    const lamp = lampRef.current;
    if (!lamp) return;
    lamp.material.color.set(lampOn ? 0xfbbf24 : 0x334155);
    lamp.material.emissive.set(lampOn ? 0xffd166 : 0x000000);
  }, [lampOn]);

  return <div className="viewport" ref={containerRef} />;
}
