export type AirflowMode = "off" | "neutral" | "cool" | "heat";

type Vec3 = { x: number; y: number; z: number };

export type AirflowConfig = {
  active: boolean;
  mode: AirflowMode;
  intensity: number;
  origin: Vec3;
  direction: Vec3;
  ventWidth: number;
  ventHeight: number;
  activeParticleCount: number;
};

type AirflowEffect = {
  object: any;
  update: (patch: Partial<AirflowConfig>) => void;
  tick: (dt: number) => void;
  dispose: () => void;
};

let cachedSoftCircleTexture: any | null = null;

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

function getSoftCircleTexture(THREE: any): any {
  if (cachedSoftCircleTexture) return cachedSoftCircleTexture;

  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    g.addColorStop(0, "rgba(255,255,255,1)");
    g.addColorStop(0.35, "rgba(255,255,255,0.95)");
    g.addColorStop(0.7, "rgba(255,255,255,0.22)");
    g.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
  }

  const tex = new THREE.CanvasTexture(canvas);
  tex.needsUpdate = true;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.generateMipmaps = false;
  cachedSoftCircleTexture = tex;
  return tex;
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}

export function createAirflowEffect(THREE: any, opts?: { particleCount?: number }): AirflowEffect {
  const particleCapacity = Math.max(220, Math.min(9000, opts?.particleCount ?? 900));
  const heads = new Float32Array(particleCapacity * 3);
  const vels = new Float32Array(particleCapacity * 3);
  const ages = new Float32Array(particleCapacity);
  const lifes = new Float32Array(particleCapacity);
  const seeds = new Float32Array(particleCapacity);
  const linePositions = new Float32Array(particleCapacity * 2 * 3);
  const lineColors = new Float32Array(particleCapacity * 2 * 3);
  const pointColors = new Float32Array(particleCapacity * 3);

  const origin = new THREE.Vector3(0, 0, 0);
  const direction = new THREE.Vector3(0, 0, 1);

  let ventWidth = 0.26;
  let ventHeight = 0.12;
  let active = false;
  let mode: AirflowMode = "neutral";
  let intensity = 1.0;
  let activeParticleCount = particleCapacity;

  const colorCool = new THREE.Color(0x4dabf7);
  const colorHeat = new THREE.Color(0xff6b6b);
  const colorNeutral = new THREE.Color(0x93c5fd);
  const tmpColor = new THREE.Color();

  function baseColorForMode(m: AirflowMode): any {
    if (m === "heat") return colorHeat;
    if (m === "cool") return colorCool;
    if (m === "neutral") return colorNeutral;
    return colorNeutral;
  }

  function respawn(i: number): void {
    const dx = (Math.random() - 0.5) * ventWidth;
    const dy = (Math.random() - 0.5) * ventHeight;
    const idx = i * 3;

    heads[idx + 0] = origin.x + dx;
    heads[idx + 1] = origin.y + dy;
    heads[idx + 2] = origin.z + (Math.random() * 0.02 - 0.01);

    const amp = clamp(intensity, 0.2, 3.0);
    const baseSpeed = 1.15 + 0.85 * amp;
    const spreadX = 0.55 + 0.55 * amp;
    const spreadY = 0.16 + 0.18 * amp;
    const spreadZ = 0.10 + 0.10 * amp;
    const upKick = mode === "heat" ? 0.18 : mode === "cool" ? -0.14 : 0;

    vels[idx + 0] = direction.x * baseSpeed + (Math.random() - 0.5) * spreadX;
    vels[idx + 1] = direction.y * baseSpeed + (Math.random() - 0.5) * spreadY + upKick;
    vels[idx + 2] = direction.z * baseSpeed + (Math.random() - 0.5) * spreadZ;

    ages[i] = 0;
    lifes[i] = 0.8 + Math.random() * 0.9;
    seeds[i] = Math.random() * 1000;
  }

  for (let i = 0; i < particleCapacity; i += 1) respawn(i);

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(linePositions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(lineColors, 3));
  geometry.setDrawRange(0, 0);

  const material = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.6,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });

  const lines = new THREE.LineSegments(geometry, material);
  lines.frustumCulled = false;
  lines.renderOrder = 2;

  const sprite = new THREE.PointsMaterial({
    color: 0xffffff,
    map: getSoftCircleTexture(THREE),
    size: 0.07,
    sizeAttenuation: true,
    transparent: true,
    opacity: 0.25,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    vertexColors: true,
  });
  const pointsGeo = new THREE.BufferGeometry();
  pointsGeo.setAttribute("position", new THREE.BufferAttribute(heads, 3));
  pointsGeo.setAttribute("color", new THREE.BufferAttribute(pointColors, 3));
  pointsGeo.setDrawRange(0, 0);
  const points = new THREE.Points(pointsGeo, sprite);
  points.frustumCulled = false;
  points.renderOrder = 1;

  const root = new THREE.Group();
  root.add(points);
  root.add(lines);
  root.visible = false;

  let t = 0;

  function update(patch: Partial<AirflowConfig>): void {
    let needsRespawn = false;
    if (typeof patch.active === "boolean") {
      active = patch.active;
      root.visible = active;
      needsRespawn = needsRespawn || active;
      geometry.setDrawRange(0, active ? activeParticleCount * 2 : 0);
      pointsGeo.setDrawRange(0, active ? activeParticleCount : 0);
    }
    if (patch.mode && patch.mode !== mode) {
      mode = patch.mode;
      needsRespawn = true;
    }
    if (typeof patch.intensity === "number" && Number.isFinite(patch.intensity)) {
      const next = clamp(patch.intensity, 0.2, 3.0);
      if (Math.abs(next - intensity) > 0.03) needsRespawn = true;
      intensity = next;
    }

    if (patch.origin) {
      origin.set(patch.origin.x, patch.origin.y, patch.origin.z);
      needsRespawn = true;
    }
    if (patch.direction) {
      direction.set(patch.direction.x, patch.direction.y, patch.direction.z);
      direction.normalize();
      needsRespawn = true;
    }
    if (typeof patch.ventWidth === "number" && Number.isFinite(patch.ventWidth)) {
      const next = clamp(patch.ventWidth, 0.06, 0.9);
      if (Math.abs(next - ventWidth) > 1e-6) needsRespawn = true;
      ventWidth = next;
    }
    if (typeof patch.ventHeight === "number" && Number.isFinite(patch.ventHeight)) {
      const next = clamp(patch.ventHeight, 0.04, 0.65);
      if (Math.abs(next - ventHeight) > 1e-6) needsRespawn = true;
      ventHeight = next;
    }
    if (typeof patch.activeParticleCount === "number" && Number.isFinite(patch.activeParticleCount)) {
      const next = Math.max(30, Math.min(particleCapacity, Math.floor(patch.activeParticleCount)));
      if (next !== activeParticleCount) needsRespawn = needsRespawn || active;
      activeParticleCount = next;
      geometry.setDrawRange(0, activeParticleCount * 2);
      pointsGeo.setDrawRange(0, activeParticleCount);
    }

    if (!active) return;
    if (needsRespawn) {
      for (let i = 0; i < activeParticleCount; i += 1) respawn(i);
      return;
    }
    for (let i = 0; i < activeParticleCount; i += 1) if (ages[i] > lifes[i]) respawn(i);
  }

  function tick(dt: number): void {
    if (!active) return;

    const d = Math.max(0.001, Math.min(0.05, dt));
    t += d;

    const colorBase = baseColorForMode(mode);
    const buoyancy = mode === "heat" ? 0.85 : mode === "cool" ? -0.72 : 0.0;
    const buoy = buoyancy * clamp(intensity, 0.2, 3.0);
    const maxDist = 3.2;
    const speedScale = 1.0 + 0.35 * clamp(intensity, 0.2, 3.0);
    const tailLen = 0.09 + 0.09 * clamp(intensity, 0.2, 3.0);
    const damping = Math.pow(0.985, d * 60);

    for (let i = 0; i < activeParticleCount; i += 1) {
      ages[i] += d;
      if (ages[i] >= lifes[i]) {
        respawn(i);
        continue;
      }

      const idx = i * 3;
      const px = heads[idx + 0];
      const py = heads[idx + 1];
      const pz = heads[idx + 2];

      const dz = pz - origin.z;
      const dist01 = clamp(dz / maxDist, 0, 1);

      const turbH = (0.22 + 0.65 * dist01) * speedScale;
      const turbV = (0.12 + 0.40 * dist01) * speedScale;
      const s = seeds[i];
      const n1 = Math.sin(t * 2.8 + px * 1.8 + pz * 1.4 + s) * turbH;
      const n2 = Math.cos(t * 2.2 + py * 1.6 + pz * 1.1 + s * 0.8) * turbV;

      vels[idx + 0] += n1 * d;
      vels[idx + 1] += (n2 + buoy) * d;

      vels[idx + 0] *= damping;
      vels[idx + 1] *= damping;
      vels[idx + 2] *= damping;

      heads[idx + 0] += vels[idx + 0] * d;
      heads[idx + 1] += vels[idx + 1] * d;
      heads[idx + 2] += vels[idx + 2] * d;

      const dzNext = heads[idx + 2] - origin.z;
      if (dzNext > maxDist || dzNext < -0.4) {
        respawn(i);
        continue;
      }

      const age01 = clamp(ages[i] / lifes[i], 0, 1);
      const fadeIn = smoothstep(0.0, 0.12, age01);
      const fadeOut = 1 - smoothstep(0.6, 1.0, age01);
      const fadeDist = 1 - smoothstep(0.35, 0.9, dist01);
      const fade = clamp(fadeIn * fadeOut * fadeDist, 0, 1);

      tmpColor.copy(colorBase);
      const boost = 0.7 + 0.55 * clamp(intensity, 0.2, 3.0);
      tmpColor.multiplyScalar(fade * boost);

      const headPos = i * 2 * 3;
      linePositions[headPos + 0] = heads[idx + 0];
      linePositions[headPos + 1] = heads[idx + 1];
      linePositions[headPos + 2] = heads[idx + 2];

      const dirIdx = idx;
      const vx = vels[dirIdx + 0];
      const vy = vels[dirIdx + 1];
      const vz = vels[dirIdx + 2];
      const len = Math.sqrt(vx * vx + vy * vy + vz * vz) || 1e-9;
      const tx = vx / len;
      const ty = vy / len;
      const tz = vz / len;

      linePositions[headPos + 3] = heads[idx + 0] - tx * tailLen;
      linePositions[headPos + 4] = heads[idx + 1] - ty * tailLen;
      linePositions[headPos + 5] = heads[idx + 2] - tz * tailLen;

      const r = tmpColor.r;
      const g = tmpColor.g;
      const b = tmpColor.b;
      lineColors[headPos + 0] = r;
      lineColors[headPos + 1] = g;
      lineColors[headPos + 2] = b;
      lineColors[headPos + 3] = r * 0.15;
      lineColors[headPos + 4] = g * 0.15;
      lineColors[headPos + 5] = b * 0.15;

      pointColors[idx + 0] = r;
      pointColors[idx + 1] = g;
      pointColors[idx + 2] = b;
    }

    (pointsGeo.attributes.position as any).needsUpdate = true;
    (geometry.attributes.position as any).needsUpdate = true;
    (geometry.attributes.color as any).needsUpdate = true;
    (pointsGeo.attributes.color as any).needsUpdate = true;
  }

  function dispose(): void {
    geometry.dispose();
    pointsGeo.dispose();
    material.dispose();
    sprite.dispose();
  }

  return {
    object: root,
    update,
    tick,
    dispose,
  };
}
