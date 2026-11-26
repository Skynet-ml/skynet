/**
 * The landing page's two WebGL scenes, on three.js. This module is only
 * loaded with a dynamic import, so three stays out of the first bundle.
 *
 * - The hero field: a cloud of attempts with their search lineage, which
 *   converges on one best point as the visitor scrolls.
 * - The finale tunnel: rings of attempts that the camera flies through as the
 *   finale scrolls by.
 */

import * as THREE from "three";

/** One mounted scene. The caller owns the frame loop. */
export interface LandingScene {
  /** Draw one frame; `t` is seconds since mount. */
  render(t: number): void;
  /** Match the drawing buffer to the canvas size. */
  resize(): void;
  /** Free every GPU resource. */
  dispose(): void;
}

interface SceneOptions {
  /** prefers-reduced-motion: draw a still, settled frame. */
  reduce: boolean;
}

const clamp = (v: number, a: number, b: number) => Math.min(b, Math.max(a, v));

/** A seeded generator, so each scene has the same shape on every visit. */
function seeded(seed: number) {
  let s = seed;
  const rand = () => (s = (s * 16807) % 2147483647) / 2147483647;
  const gauss = () => {
    const u = rand() || 1e-9;
    const v = rand();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  };
  return { rand, gauss };
}

const CREAM = new THREE.Color("#efe6da");
const RUST = new THREE.Color("#b4521f");
const EMBER = new THREE.Color("#e0935a");

/** Soft round points with a size, color and generation (for the reveal) for each point. */
function pointMaterial(pixelRatio: number, reveal: number) {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uReveal: { value: reveal },
      uPR: { value: pixelRatio },
      uNear: { value: 3 },
      uFar: { value: 10 },
    },
    vertexShader: `attribute float aSize; attribute vec3 aColor; attribute float aGen; uniform float uPR, uReveal, uNear, uFar;
      varying vec3 vColor; varying float vA;
      void main() { vec4 mv = modelViewMatrix * vec4(position, 1.0); float d = -mv.z;
        gl_PointSize = aSize * uPR * (9.0 / d); gl_Position = projectionMatrix * mv; vColor = aColor;
        vA = smoothstep(uFar, uNear, d) * smoothstep(aGen, aGen + 0.08, uReveal); }`,
    fragmentShader: `varying vec3 vColor; varying float vA;
      void main() { float d = length(gl_PointCoord - 0.5); float a = smoothstep(0.5, 0.0, d); gl_FragColor = vec4(vColor, a * a * vA); }`,
  });
}

function pointGeometry(pos: Float32Array, col: Float32Array, size: Float32Array, gen: Float32Array) {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("aColor", new THREE.BufferAttribute(col, 3));
  geo.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
  geo.setAttribute("aGen", new THREE.BufferAttribute(gen, 1));
  return geo;
}

function glowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  if (g) {
    const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    grd.addColorStop(0, "rgba(255,255,255,1)");
    grd.addColorStop(0.25, "rgba(255,255,255,.45)");
    grd.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = grd;
    g.fillRect(0, 0, 128, 128);
  }
  return new THREE.CanvasTexture(c);
}

/** A renderer, or null when the browser has no WebGL. */
function makeRenderer(canvas: HTMLCanvasElement) {
  try {
    const r = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "high-performance" });
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    return r;
  } catch {
    return null;
  }
}

/** Dispose every geometry and material under `root`, then the renderer and its context. */
function disposeAll(root: THREE.Object3D, renderer: THREE.WebGLRenderer, extra: Array<{ dispose(): void }> = []) {
  root.traverse((obj) => {
    const o = obj as THREE.Mesh;
    o.geometry?.dispose();
    const m = o.material;
    if (Array.isArray(m)) m.forEach((x) => x.dispose());
    else m?.dispose();
  });
  extra.forEach((x) => x.dispose());
  renderer.dispose();
  renderer.forceContextLoss();
}

/** The hero: a search tree of attempts that converges on one best point as the page scrolls. */
export function createHeroField(canvas: HTMLCanvasElement, { reduce }: SceneOptions): LandingScene | null {
  const renderer = makeRenderer(canvas);
  if (!renderer) return null;
  const { gauss } = seeded(7);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 50);
  camera.position.set(0, 0, 7.2);
  const group = new THREE.Group();
  scene.add(group);

  const N = 900;
  const best = new THREE.Vector3(1.35, 0.55, 0.3);
  const P: THREE.Vector3[] = [];
  const Q: THREE.Vector3[] = [];
  const parent = new Int32Array(N);
  const pos = new Float32Array(N * 3);
  const col = new Float32Array(N * 3);
  const size = new Float32Array(N);
  const gen = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const g = i / N;
    gen[i] = g;
    const spread = 1 - g * 0.55;
    P.push(new THREE.Vector3(gauss() * 2.1 * spread - 0.6 * (1 - g), gauss() * 1.1 * spread - 0.2, gauss() * 1.4));
    const t = Math.pow(1 - g, 1.4);
    Q.push(new THREE.Vector3(best.x + gauss() * 1.1 * t, best.y + gauss() * 0.75 * t, best.z + gauss() * 0.9 * t));
    const c = g > 0.9 ? RUST.clone().lerp(EMBER, (g - 0.9) * 10) : CREAM.clone().multiplyScalar(0.45 + g * 0.45);
    col.set([c.r, c.g, c.b], i * 3);
    size[i] = (g > 0.9 ? 7 : 3.2) + g * 3;
    // Each attempt's parent is its nearest earlier attempt in a recent window.
    let bd = Infinity;
    parent[i] = -1;
    for (let j = Math.max(0, i - 160); j < i; j++) {
      const d = P[j]!.distanceToSquared(P[i]!);
      if (d < bd) {
        bd = d;
        parent[i] = j;
      }
    }
  }
  // The winning lineage: from the attempt nearest the best point back to the first.
  let bestIdx = N - 1;
  let bestDist = Infinity;
  for (let i = N - 40; i < N; i++) {
    const d = Q[i]!.distanceToSquared(best);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = i;
    }
  }
  const lineage = new Set<number>();
  for (let i = bestIdx; i >= 0; i = parent[i]!) lineage.add(i);

  const geo = pointGeometry(pos, col, size, gen);
  const mat = pointMaterial(renderer.getPixelRatio(), reduce ? 2 : 0);
  group.add(new THREE.Points(geo, mat));

  // Long edges cross the whole cloud and read as noise, so only short ones are drawn.
  const edges: Array<[number, number]> = [];
  const hot: Array<[number, number]> = [];
  for (let i = 1; i < N; i++) {
    const p = parent[i]!;
    if (p < 0) continue;
    const onPath = lineage.has(i);
    if (P[i]!.distanceTo(P[p]!) < (onPath ? 1.6 : 0.9)) (onPath ? hot : edges).push([i, p]);
  }
  const mkLines = (list: Array<[number, number]>, color: number, opacity: number) => {
    const a = new Float32Array(list.length * 6);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(a, 3));
    const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity: reduce ? opacity : 0, depthWrite: false });
    group.add(new THREE.LineSegments(g, m));
    return { list, a, g, m, opacity };
  };
  const lines = [mkLines(edges, 0xd9c7b2, 0.12), mkLines(hot, 0xe0935a, 0.85)];

  const tex = glowTexture();
  const sprite = (color: number) =>
    new THREE.Sprite(
      new THREE.SpriteMaterial({ map: tex, color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }),
    );
  const glow = sprite(0xb4521f);
  const core = sprite(0xfff3e6);
  group.add(glow, core);

  let mx = 0;
  let my = 0;
  let rx = 0;
  let ry = 0;
  const onPointer = (e: PointerEvent) => {
    const r = canvas.getBoundingClientRect();
    mx = (e.clientX - r.left) / r.width - 0.5;
    my = (e.clientY - r.top) / r.height - 0.5;
  };
  if (!reduce) window.addEventListener("pointermove", onPointer, { passive: true });

  const tmp = new THREE.Vector3();
  return {
    resize() {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.position.z = w / h < 1 ? 10.5 : 7.2;
      camera.updateProjectionMatrix();
    },
    render(t) {
      const k = reduce ? 0.6 : clamp(window.scrollY / (canvas.clientHeight * 0.85 || 1), 0, 1);
      const e = 1 - Math.pow(1 - k, 3);
      mat.uniforms.uReveal!.value = reduce ? 2 : Math.min(2, (t / 2.2) * 1.1);
      const lineIn = reduce ? 1 : clamp((t - 1.2) / 1.6, 0, 1);
      for (const L of lines) L.m.opacity = L.opacity * lineIn;
      for (let i = 0; i < N; i++) {
        tmp.copy(P[i]!).lerp(Q[i]!, e);
        if (!reduce) {
          tmp.x += Math.sin(t * 0.7 + i) * 0.03;
          tmp.y += Math.cos(t * 0.6 + i * 1.3) * 0.03;
        }
        pos[i * 3] = tmp.x;
        pos[i * 3 + 1] = tmp.y;
        pos[i * 3 + 2] = tmp.z;
      }
      geo.attributes.position!.needsUpdate = true;
      for (const L of lines) {
        L.list.forEach(([a, b], j) => {
          L.a.set(pos.subarray(a * 3, a * 3 + 3), j * 6);
          L.a.set(pos.subarray(b * 3, b * 3 + 3), j * 6 + 3);
        });
        L.g.attributes.position!.needsUpdate = true;
      }
      const pulse = reduce ? 1 : 1 + Math.sin(t * 2.2) * 0.1;
      glow.position.copy(best);
      core.position.copy(best);
      glow.scale.setScalar((1.4 + e * 1.2) * pulse);
      core.scale.setScalar(0.32 + e * 0.1);
      rx += (my * 0.45 - rx) * 0.05;
      ry += (mx * 0.7 - ry) * 0.05;
      group.rotation.set(-0.12 + rx, ry + (reduce ? 0 : t * 0.04), 0);
      group.position.x = -e * 0.9;
      group.position.y = -e * 0.1;
      renderer.render(scene, camera);
    },
    dispose() {
      window.removeEventListener("pointermove", onPointer);
      disposeAll(scene, renderer, [tex]);
    },
  };
}

/** The finale: fly through rings of attempts while the color shifts from cream to rust. */
export function createTunnel(
  canvas: HTMLCanvasElement,
  { reduce, progress, fog }: SceneOptions & { progress: () => number; fog: string },
): LandingScene | null {
  const renderer = makeRenderer(canvas);
  if (!renderer) return null;
  const { rand, gauss } = seeded(11);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(70, 1, 0.1, 60);
  const R = 110;
  const M = 46;
  const N = R * M;
  const depth = 0.55;
  const pos = new Float32Array(N * 3);
  const col = new Float32Array(N * 3);
  const size = new Float32Array(N);
  const gen = new Float32Array(N);
  const stops = [new THREE.Color("#6b5d50"), CREAM, EMBER, RUST, CREAM];
  for (let r = 0, i = 0; r < R; r++) {
    const f = (r / (R - 1)) * (stops.length - 1);
    const c = stops[Math.floor(f)]!.clone().lerp(stops[Math.min(stops.length - 1, Math.floor(f) + 1)]!, f % 1);
    for (let m = 0; m < M; m++, i++) {
      const a = (m / M) * Math.PI * 2 + r * 0.19 + rand() * 0.1;
      const rad = 2.4 + gauss() * 0.35;
      pos.set([Math.cos(a) * rad, Math.sin(a) * rad, -r * depth - rand() * depth], i * 3);
      col.set([c.r, c.g, c.b], i * 3);
      size[i] = 2 + rand() * 4.5;
    }
  }
  const mat = pointMaterial(renderer.getPixelRatio(), 2);
  mat.uniforms.uNear!.value = 1.5;
  mat.uniforms.uFar!.value = 22;
  const pts = new THREE.Points(pointGeometry(pos, col, size, gen), mat);
  scene.add(pts);
  const lp: number[] = [];
  for (let r = 0; r < R - 1; r++)
    for (let m = 0; m < M; m += 7) {
      const a = (r * M + m) * 3;
      const b = ((r + 1) * M + ((m + 2) % M)) * 3;
      lp.push(...pos.subarray(a, a + 3), ...pos.subarray(b, b + 3));
    }
  const lg = new THREE.BufferGeometry();
  lg.setAttribute("position", new THREE.Float32BufferAttribute(lp, 3));
  const links = new THREE.LineSegments(
    lg,
    new THREE.LineBasicMaterial({ color: 0xe0935a, transparent: true, opacity: 0.1, depthWrite: false }),
  );
  scene.add(links);
  scene.fog = new THREE.Fog(new THREE.Color(fog), 4, 20);

  let z = 2;
  return {
    resize() {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    },
    render(t) {
      const target = 2 - progress() * (R * depth - 14);
      z = reduce ? target : z + (target - z) * 0.06;
      camera.position.set(reduce ? 0 : Math.sin(t * 0.3) * 0.2, reduce ? 0 : Math.cos(t * 0.25) * 0.2, z);
      pts.rotation.z = reduce ? 0 : t * 0.05;
      links.rotation.z = pts.rotation.z;
      renderer.render(scene, camera);
    },
    dispose() {
      disposeAll(scene, renderer);
    },
  };
}
