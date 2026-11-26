/**
 * The world of the evolution figure: one journey out of the sea, up the beach,
 * across the land, and upright.
 *
 * The figure stays in the middle of its box and the world passes under it. The
 * camera follows the ground under the animal, so the ground line stays under the
 * feet while the sea surface drops, and the shore passes as the first legs form.
 *
 * Each animal also moves the way it travels: the fish bend in a wave from head to
 * tail, the four-legged animals swing their legs in a trot, the upright ones stride.
 * The movement is a warp on the points of the animal, so it keeps going while one
 * animal flows into the next. The last animal, the human, runs the last stretch,
 * then slows down and stands exactly as authored: the journey ends there, and the
 * still figure of other screens matches it.
 *
 * Time is in the native milliseconds of the morph timeline (see evolution-morph.tsx).
 */

import type { PartRole } from "./evolution-animals.ts";
import type { Animal, Evolution, Part, Warp } from "./evolution-poses.ts";

/** How an animal travels. Lengths are in the units of the 200x160 figure box. */
type Gait = {
  /** How far above the ground the lowest point of the animal rests (a swimmer floats). */
  clearance: number;
  /** One full stroke or stride. */
  cycleMs: number;
  /** How far a hind limb swings to each side, in radians. */
  swing: number;
  /** The same for a fore limb, when it differs. */
  foreSwing?: number;
  /** Share of its length that a limb shortens by while it swings forward, so the foot clears the ground. */
  lift: number;
  /** How far the body rises and falls with each step. */
  bob: number;
  /** The wave that runs down the body or the tail: its height at `to`, growing from none at `from`. */
  wave?: { amp: number; from: number; to: number; length: number };
  /** How far a knee folds while its leg swings forward, in radians: a runner's heel comes up behind. */
  knee?: number;
  /** How high a runner leaves the ground between two steps. */
  hop?: number;
  /** How far a runner leans forward, in radians. */
  lean?: number;
  /** The limb that carries something, so it does not swing. */
  holds?: PartRole;
  /** How far that limb lifts what it carries off the ground while the animal moves, in radians. */
  carry?: number;
  /** How far the carried thing tips forward in the hand, in radians. */
  tip?: number;
};

const DEG = Math.PI / 180;

const GAITS: Record<string, Gait> = {
  haikouichthys: {
    clearance: 15,
    cycleMs: 290,
    swing: 0,
    lift: 0,
    bob: 0.5,
    wave: { amp: 2.8, from: 137, to: 57, length: 60 },
  },
  eusthenopteron: {
    clearance: 8,
    cycleMs: 330,
    swing: 14 * DEG,
    lift: 0,
    bob: 0.5,
    wave: { amp: 3.4, from: 146, to: 46, length: 74 },
  },
  tiktaalik: {
    clearance: 0,
    cycleMs: 380,
    swing: 24 * DEG,
    lift: 0.1,
    bob: 0.5,
    wave: { amp: 2.6, from: 96, to: 25, length: 64 },
  },
  acanthostega: {
    clearance: 0,
    cycleMs: 380,
    swing: 19 * DEG,
    lift: 0.1,
    bob: 0.6,
    wave: { amp: 2.4, from: 84, to: 41, length: 52 },
  },
  dimetrodon: {
    clearance: 0,
    cycleMs: 430,
    swing: 15 * DEG,
    lift: 0.1,
    bob: 0.8,
    wave: { amp: 1.8, from: 66, to: 18, length: 60 },
  },
  thrinaxodon: {
    clearance: 0,
    cycleMs: 310,
    swing: 21 * DEG,
    lift: 0.12,
    bob: 0.7,
    wave: { amp: 1.4, from: 84, to: 48, length: 44 },
  },
  morganucodon: {
    clearance: 0,
    cycleMs: 250,
    swing: 24 * DEG,
    lift: 0.14,
    bob: 0.6,
    wave: { amp: 1.6, from: 86, to: 49, length: 46 },
  },
  proconsul: { clearance: 0, cycleMs: 400, swing: 13 * DEG, lift: 0.07, bob: 1 },
  australopithecus: {
    clearance: 0,
    cycleMs: 440,
    swing: 13 * DEG,
    foreSwing: 8 * DEG,
    lift: 0.06,
    bob: 1,
  },
  "homo-sapiens": {
    clearance: 0,
    cycleMs: 340,
    swing: 30 * DEG,
    foreSwing: 24 * DEG,
    lift: 0.03,
    bob: 0,
    knee: 62 * DEG,
    hop: 1.5,
    lean: 7 * DEG,
    holds: "fore-near",
    carry: 20 * DEG,
    tip: 6 * DEG,
  },
};

/** A trot, and an upright stride: the two limbs of a diagonal move together. */
const LIMB_PHASE: Partial<Record<PartRole, number>> = {
  "fore-near": 0,
  "hind-far": 0,
  "fore-far": Math.PI,
  "hind-near": Math.PI,
};

/** The durations of the morph timeline that the scene follows. */
export type SceneTimeline = { firstHoldMs: number; holdMs: number; morphMs: number };

// The animal travels this far for each millisecond, until the human slows down to stand.
const SPEED = 0.3;
// The human runs on for this long after it forms, and slows to a stand over the last part of it.
const RUN_MS = 640;
const SETTLE_MS = 260;
// A knee starts to fold a little before its foot leaves the ground, and is straight again to land.
const KNEE_LEAD = 0.5;
// The movement starts from the still pose, so the server-drawn first frame does not jump.
const START_RAMP_MS = 140;

// The sea bed lies this far under the land, and the sea surface a little under it.
const SEA_BED = -48;
const SEA_LEVEL = -1.2;
// The gentle rise and fall of the land.
const HILL = 1.1;
const HILL_LENGTH = 21;

// The world is drawn this far past the two sides of the 200-unit figure box.
export const SCENE_MARGIN = 86;

const r = (v: number) => Math.round(v * 10) / 10;

function smoothstep(u: number): number {
  const c = Math.min(1, Math.max(0, u));
  return c * c * (3 - 2 * c);
}

/** A small fixed random series, so the server and the client draw the same world. */
function series(seed: number): () => number {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Limb = {
  px: number;
  py: number;
  phase: number;
  swing: number;
  /** The knee, for a leg that folds. */
  knee?: { x: number; y: number };
};

/** The limb that holds something: its joint, its hand, and the parts that the hand holds. */
type Hold = { role: PartRole; px: number; py: number; hx: number; hy: number; held: Set<Part> };

type Seated = {
  gait: Gait;
  /** How far down the animal moves to rest on the ground of the scene. */
  dy: number;
  limbs: Map<Part, Limb>;
  hold?: Hold;
};

function lowest(animal: Animal): number {
  let y = -Infinity;
  for (const p of animal.parts) {
    for (let i = 1; i < p.dense.length; i += 2) y = Math.max(y, p.dense[i]!);
  }
  return y;
}

/** The knee of a leg: half way down from the joint, in the middle of the outline at that height. */
function kneeOf(part: Part, px: number, py: number): { x: number; y: number } {
  let foot = py;
  for (let i = 1; i < part.dense.length; i += 2) foot = Math.max(foot, part.dense[i]!);
  const y = (py + foot) / 2;
  let sum = 0;
  let count = 0;
  for (let i = 0; i < part.dense.length; i += 2) {
    if (Math.abs(part.dense[i + 1]! - y) > 3) continue;
    sum += part.dense[i]!;
    count++;
  }
  return { x: count ? sum / count : px, y };
}

/** The hand of a limb: the point of its outline that is farthest from the joint. */
function handOf(part: Part, px: number, py: number): { hx: number; hy: number } {
  let hx = px;
  let hy = py;
  let far = 0;
  for (let i = 0; i < part.dense.length; i += 2) {
    const d = Math.hypot(part.dense[i]! - px, part.dense[i + 1]! - py);
    if (d > far) {
      far = d;
      hx = part.dense[i]!;
      hy = part.dense[i + 1]!;
    }
  }
  return { hx, hy };
}

function seat(animal: Animal, baseline: number): Seated {
  const gait = GAITS[animal.key];
  if (!gait) throw new Error(`No gait for ${animal.key}`);
  const pivots = new Map<PartRole, Limb>();
  let hold: Hold | undefined;
  for (const p of animal.parts) {
    const phase = p.role ? LIMB_PHASE[p.role] : undefined;
    if (!p.role || phase === undefined) continue;
    // A limb is drawn from the body and back to it, so its two ends mark the joint.
    const n = p.bez.length;
    const px = (p.bez[0]! + p.bez[n - 2]!) / 2;
    const py = (p.bez[1]! + p.bez[n - 1]!) / 2;
    if (p.role === gait.holds) {
      hold = { role: p.role, px, py, ...handOf(p, px, py), held: new Set() };
      continue;
    }
    const fore = p.role.startsWith("fore");
    pivots.set(p.role, {
      px,
      py,
      phase,
      swing: fore ? (gait.foreSwing ?? gait.swing) : gait.swing,
      knee: gait.knee && !fore ? kneeOf(p, px, py) : undefined,
    });
  }
  const limbs = new Map<Part, Limb>();
  for (const p of animal.parts) {
    const limb = p.host ? pivots.get(p.host) : undefined;
    if (limb) limbs.set(p, limb);
  }
  // What the hand holds has no name and lies in front of the hand: the hand itself and the spear.
  if (hold) {
    for (const p of animal.parts) if (!p.role && p.cx > hold.hx - 2) hold.held.add(p);
  }
  return { gait, dy: baseline - gait.clearance - lowest(animal), limbs, hold };
}

/** One layer of the drawn world. Paths are in world coordinates; `camera` gives the offset. */
export type SceneArt = {
  /** The body of the sea, to fill. */
  water: string;
  /** The sea surface. */
  surface: string;
  /** The ground line, from the sea bed up the beach and over the land. */
  ground: string;
  /** Short marks under the ground line, so the ground reads as moving. */
  strata: string;
  /** Weeds, reeds, ferns, grass, stones and one tree. */
  plants: string;
};

export type Scene = {
  /** The height on the figure box of the ground line under the animal. */
  groundY: number;
  /** When the human stands and all movement stops. */
  durationMs: number;
  art: SceneArt;
  /** The translation of the world layers at a time. */
  camera: (elapsedMs: number) => { x: number; y: number };
  /**
   * The movement of animal `index` at a time, or nothing when it is at rest as authored.
   * `clockMs` is the real time since the start: a timeline that plays faster or slower
   * moves the world at its own speed, but the legs keep a natural pace.
   */
  warp: (index: number, elapsedMs: number, clockMs?: number) => Warp | undefined;
  /** How far the animal travelled. */
  travel: (elapsedMs: number) => number;
  /** The height of the ground at a place in the world (the land is 0, the sea bed is below). */
  elevation: (worldX: number) => number;
  /** Where the sea ends. */
  shoreX: number;
  /** Where each animal is in the world at the middle of its rest. */
  stops: number[];
};

export function buildScene(evolution: Evolution, timeline: SceneTimeline): Scene {
  const { animals } = evolution;
  const { firstHoldMs, holdMs, morphMs } = timeline;
  const period = morphMs + holdMs;
  const lastStart = firstHoldMs + (animals.length - 2) * period;
  const durationMs = lastStart + morphMs + RUN_MS;
  const settleStart = durationMs - SETTLE_MS;

  // The human is the reference: it ends as authored, so the still figure matches the last frame.
  const baseline = lowest(animals[animals.length - 1]!);
  const groundY = baseline + 1;
  const seated = animals.map((animal) => seat(animal, baseline));

  const travel = (e: number) => {
    if (e <= settleStart) return SPEED * Math.max(0, e);
    const u = Math.min(1, (e - settleStart) / SETTLE_MS);
    return SPEED * (settleStart + SETTLE_MS * u * (1 - u / 2));
  };
  const stops = animals.map((_, i) =>
    travel(i === 0 ? firstHoldMs / 2 : firstHoldMs + i * period - holdMs / 2),
  );
  const end = travel(durationMs);
  const step = SPEED * period;

  // The beach starts under the second fish and reaches the land before the fifth animal:
  // the third wades with its back out of the water, and the fourth stands at the water's edge.
  const beachFrom = stops[1]! - 0.48 * step;
  const beachTo = beachFrom + 3.57 * step;
  const elevation = (x: number) => {
    const land = smoothstep((x - beachTo) / 60) * smoothstep((end - 34 - x) / 70);
    return (
      SEA_BED * (1 - smoothstep((x - beachFrom) / (beachTo - beachFrom))) +
      HILL * land * Math.sin((x - beachTo) / HILL_LENGTH)
    );
  };
  let shoreX = beachFrom;
  while (elevation(shoreX) < SEA_LEVEL) shoreX += 0.25;

  const camera = (e: number) => {
    const x = travel(e);
    return { x: r(100 - x), y: r(groundY + elevation(x)) };
  };

  const warp = (index: number, e: number, clockMs: number = e): Warp | undefined => {
    const { gait, dy, limbs, hold } = seated[index]!;
    // The movement dies away at the rate the human slows down, so the feet do not slide
    // and it ends in the authored pose.
    const slowing = Math.min(1, Math.max(0, (e - settleStart) / SETTLE_MS));
    const k = smoothstep(clockMs / START_RAMP_MS) * (1 - slowing);
    if (index === animals.length - 1 && dy === 0 && k === 0) return undefined;
    const phase = (2 * Math.PI * clockMs) / gait.cycleMs;
    // A runner is in the air when its legs are apart, and lowest when a leg is under it.
    const rise = k * (gait.bob * Math.sin(2 * phase) - (gait.hop ?? 0) * Math.sin(phase) ** 2);
    // A runner leans into its run.
    const lean = k * (gait.lean ?? 0);
    const tc = Math.cos(lean);
    const ts = Math.sin(lean);
    const at = travel(e);
    const here = elevation(at);
    // A runner lifts what it carries: the arm turns up at the shoulder, and the hand takes
    // the carried thing with it and tips it forward.
    const raise = -k * (gait.carry ?? 0);
    const rc = Math.cos(raise);
    const rs = Math.sin(raise);
    const tipped = k * (gait.tip ?? 0);
    const pc = Math.cos(tipped);
    const ps = Math.sin(tipped);
    const wave = gait.wave;
    const waveAt = (x: number) => {
      if (!wave) return 0;
      const along = Math.min(1, Math.max(0, (wave.from - x) / (wave.from - wave.to)));
      return k * wave.amp * along ** 1.4 * Math.sin(2 * Math.PI * (x / wave.length) + phase);
    };
    return (part, xy) => {
      const limb = limbs.get(part);
      let c = 1;
      let s = 0;
      let reach = 1;
      let fold = 0;
      if (limb) {
        const stride = phase + limb.phase;
        const angle = -k * limb.swing * Math.sin(stride);
        c = Math.cos(angle);
        s = Math.sin(angle);
        reach = 1 - k * gait.lift * Math.max(0, Math.cos(stride));
        if (limb.knee) fold = k * (gait.knee ?? 0) * Math.max(0, Math.cos(stride + KNEE_LEAD));
      }
      for (let i = 0; i < xy.length; i += 2) {
        let x = xy[i]!;
        let y = xy[i + 1]!;
        if (limb) {
          let lx = x - limb.px;
          let ly = (y - limb.py) * reach;
          if (fold && limb.knee) {
            // The shin turns back about the knee; a short band at the knee keeps the outline in one piece.
            const kx = limb.knee.x - limb.px;
            const ky = (limb.knee.y - limb.py) * reach;
            const turn = fold * smoothstep((ly - ky + 4) / 8);
            const dx = lx - kx;
            const down = ly - ky;
            lx = kx + dx * Math.cos(turn) - down * Math.sin(turn);
            ly = ky + dx * Math.sin(turn) + down * Math.cos(turn);
          }
          x = limb.px + lx * c - ly * s;
          y = limb.py + lx * s + ly * c;
        } else if (hold && raise) {
          if (part.role === hold.role) {
            const lx = x - hold.px;
            const ly = y - hold.py;
            x = hold.px + lx * rc - ly * rs;
            y = hold.py + lx * rs + ly * rc;
          } else if (hold.held.has(part)) {
            const ax = hold.hx - hold.px;
            const ay = hold.hy - hold.py;
            const lx = x - hold.hx;
            const ly = y - hold.hy;
            x = hold.px + ax * rc - ay * rs + lx * pc - ly * ps;
            y = hold.py + ax * rs + ay * rc + lx * ps + ly * pc;
          }
        }
        // A limb rides the wave at its joint, so the wave does not bend it.
        y += waveAt(limb ? limb.px : x) + rise + dy;
        const ox = x - 100;
        const oy = y - groundY;
        x = 100 + ox * tc - oy * ts;
        // A foot or a fin that swings down presses flat on the ground; it does not go into it.
        y = Math.min(groundY + ox * ts + oy * tc, groundY - 1);
        // Each point rides the ground under it, so a long animal follows the beach it climbs
        // and no part of it sinks into a slope.
        xy[i] = x;
        xy[i + 1] = y - (elevation(at + x - 100) - here);
      }
    };
  };

  return {
    groundY,
    durationMs,
    art: drawWorld(elevation, shoreX, stops, end),
    camera,
    warp,
    travel,
    elevation,
    shoreX,
    stops,
  };
}

/** Draw the world once. Heights go down the page, so a height is the negative of an elevation. */
function drawWorld(
  elevation: (x: number) => number,
  shoreX: number,
  stops: number[],
  end: number,
): SceneArt {
  const left = -100 - SCENE_MARGIN - 20;
  const right = end + 100 + SCENE_MARGIN + 20;
  const y = (x: number) => -elevation(x);
  const rnd = series(11);

  let ground = "";
  for (let x = left; x <= right; x += 4) ground += `${ground ? "L" : "M"}${r(x)} ${r(y(x))}`;

  let bed = "";
  for (let x = shoreX; x >= left; x -= 4) bed += `L${r(x)} ${r(y(x))}`;
  const water = `M${r(left)} ${r(-SEA_LEVEL)}L${r(shoreX)} ${r(-SEA_LEVEL)}${bed}Z`;

  // Ripples that flatten toward the shore.
  let surface = "";
  for (let x = left; x <= shoreX; x += 2) {
    const calm = Math.min(1, (shoreX - x) / 30);
    surface += `${surface ? "L" : "M"}${r(x)} ${r(-SEA_LEVEL + 0.8 * calm * Math.sin(x / 3.1))}`;
  }

  let strata = "";
  for (let x = left; x < right; x += 9 + rnd() * 16) {
    const depth = 3.5 + rnd() * 6.5;
    const length = 3 + rnd() * 6;
    strata += `M${r(x)} ${r(y(x) + depth)}h${r(length)}`;
  }
  // Short lines of light in the water, which pass the fish as it swims.
  for (let x = left; x < shoreX - 30; x += 12 + rnd() * 18) {
    const depth = 5 + rnd() * Math.max(2, y(x) + SEA_LEVEL - 10);
    strata += `M${r(x)} ${r(-SEA_LEVEL + depth)}h${r(4 + rnd() * 5)}`;
  }

  let plants = "";
  const weed = (x: number, height: number) => {
    const y0 = y(x);
    const lean = 2 + rnd() * 2;
    plants +=
      `M${r(x)} ${r(y0)}C${r(x - lean)} ${r(y0 - height * 0.35)} ${r(x + lean)} ${r(y0 - height * 0.65)} ${r(x - 0.5)} ${r(y0 - height)}` +
      `M${r(x + 2.4)} ${r(y(x + 2.4))}C${r(x + 2.4 + lean)} ${r(y0 - height * 0.25)} ${r(x + 1)} ${r(y0 - height * 0.5)} ${r(x + 3.6)} ${r(y0 - height * 0.72)}`;
  };
  const stone = (x: number, size: number) => {
    plants += `M${r(x - size)} ${r(y(x - size))}a${r(size)} ${r(size * 0.8)} 0 0 1 ${r(size * 2)} 0`;
  };
  const reed = (x: number, height: number) => {
    const y0 = y(x);
    plants += `M${r(x)} ${r(y0)}L${r(x + 1.2)} ${r(y0 - height)}M${r(x + 0.9)} ${r(y0 - height * 0.72)}l2.4 -2.6`;
  };
  const fern = (x: number, size: number) => {
    const y0 = y(x);
    for (const lean of [-0.9, -0.45, 0, 0.45, 0.9]) {
      const tip = size * (1 - 0.25 * Math.abs(lean));
      plants += `M${r(x)} ${r(y0)}q${r(lean * tip * 0.3)} ${r(-tip * 0.75)} ${r(lean * tip)} ${r(-tip)}`;
    }
  };
  const grass = (x: number) => {
    const y0 = y(x);
    plants += `M${r(x - 1.6)} ${r(y0)}l-1.3 -4M${r(x)} ${r(y0)}l0.3 -5.4M${r(x + 1.6)} ${r(y0)}l1.6 -3.8`;
  };
  const tree = (x: number) => {
    const y0 = y(x);
    plants +=
      `M${r(x)} ${r(y0)}C${r(x + 2)} ${r(y0 - 13)} ${r(x - 1.5)} ${r(y0 - 22)} ${r(x + 1.5)} ${r(y0 - 32)}` +
      `M${r(x + 0.5)} ${r(y0 - 19)}q6 -5 10 -11M${r(x + 0.2)} ${r(y0 - 23)}q-6 -4.5 -11.5 -8` +
      `M${r(x - 22)} ${r(y0 - 31)}q23 -12.5 45 -0.6`;
  };

  const [fish, lobeFin, wader, firstLegs, sail, burrower, shrew, ape, lucy] = stops as [
    number,
    number,
    number,
    number,
    number,
    number,
    number,
    number,
    number,
  ];
  weed(fish - 62, 17);
  stone(fish - 24, 1.6);
  weed(fish + 34, 13);
  stone(fish + 70, 1.3);
  weed(lobeFin - 18, 15);
  stone(lobeFin + 22, 1.8);
  weed(lobeFin + 44, 9);
  stone(wader + 8, 1.4);
  reed(shoreX - 17, 9);
  reed(shoreX - 9, 12);
  reed(shoreX + 4, 10);
  stone(firstLegs + 34, 1.5);
  fern(sail - 26, 9);
  fern(sail + 38, 11);
  stone(burrower - 12, 1.7);
  fern(burrower + 26, 8);
  grass(shrew - 20);
  stone(shrew + 16, 1.3);
  grass(shrew + 40);
  grass(ape - 14);
  tree(ape + 36);
  grass(lucy - 22);
  stone(lucy + 14, 1.4);
  // The open land that the human runs over.
  grass(end - 168);
  fern(end - 132, 7);
  stone(end - 104, 1.6);
  grass(end - 78);
  grass(end - 46);
  stone(end + 27, 1.5);
  grass(end + 52);

  return { water, surface, ground, strata, plants };
}
