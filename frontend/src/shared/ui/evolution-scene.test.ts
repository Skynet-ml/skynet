import assert from "node:assert/strict";
import test from "node:test";

import { ANIMALS } from "./evolution-animals.ts";
import { buildEvolution, restFrame, type Animal, type Warp } from "./evolution-poses.ts";
import { buildScene } from "./evolution-scene.ts";

// The native timeline of evolution-morph.tsx.
const FIRST_HOLD_MS = 378;
const HOLD_MS = 68;
const MORPH_MS = 142;
const PERIOD_MS = HOLD_MS + MORPH_MS;

const evolution = buildEvolution(ANIMALS);
const { animals } = evolution;
const scene = buildScene(evolution, {
  firstHoldMs: FIRST_HOLD_MS,
  holdMs: HOLD_MS,
  morphMs: MORPH_MS,
});
const human = animals.length - 1;

/** The middle of the rest of animal `index`. */
const restAt = (index: number) =>
  index === 0 ? FIRST_HOLD_MS / 2 : FIRST_HOLD_MS + index * PERIOD_MS - HOLD_MS / 2;

/** The highest and the lowest point of an animal on the figure box; heights go down the page. */
function extent(animal: Animal, warp: Warp | undefined): { top: number; bottom: number } {
  let top = Infinity;
  let bottom = -Infinity;
  for (const part of animal.parts) {
    const xy = Float64Array.from(part.dense);
    warp?.(part, xy);
    for (let i = 1; i < xy.length; i += 2) {
      top = Math.min(top, xy[i]!);
      bottom = Math.max(bottom, xy[i]!);
    }
  }
  return { top, bottom };
}

/** The height of the sea surface on the figure box when the animal is at `elapsedMs`. */
const surfaceAt = (elapsedMs: number) => scene.camera(elapsedMs).y + 1.2;

test("the human ends as authored, so the still figure matches the last frame", () => {
  for (const elapsed of [scene.durationMs, scene.durationMs + 400]) {
    assert.equal(scene.warp(human, elapsed, 5000), undefined);
  }
  assert.deepEqual(
    restFrame(animals[human]!, scene.warp(human, scene.durationMs)),
    restFrame(animals[human]!),
  );
  assert.equal(extent(animals[human]!, undefined).bottom, scene.groundY - 1);
});

test("the journey only goes forward, and the world stops when the human lands", () => {
  let before = -1;
  for (let elapsed = 0; elapsed <= scene.durationMs; elapsed += 10) {
    const at = scene.travel(elapsed);
    assert.ok(at >= before, `travel went back at ${elapsed} ms`);
    before = at;
  }
  assert.deepEqual(scene.camera(scene.durationMs + 400), scene.camera(scene.durationMs));
  assert.equal(scene.camera(scene.durationMs).y, scene.groundY);
});

test("the first frame is the still fish, so the server-drawn frame does not jump", () => {
  const fish = animals[0]!;
  const moved = extent(fish, scene.warp(0, 0, 0));
  const authored = extent(fish, undefined);
  assert.ok(Math.abs(moved.bottom - moved.top - (authored.bottom - authored.top)) < 1e-6);
});

test("the human runs after it forms, then slows down to a stand", () => {
  // Each point is measured from the ground under it: a foot far from the middle stands on its own slope.
  const lift = (elapsed: number, clock: number) => {
    const warp = scene.warp(human, elapsed, clock);
    const at = scene.travel(elapsed);
    let off = Infinity;
    for (const part of animals[human]!.parts) {
      const xy = Float64Array.from(part.dense);
      warp?.(part, xy);
      for (let i = 0; i < xy.length; i += 2) {
        const ground = scene.groundY - (scene.elevation(at + xy[i]! - 100) - scene.elevation(at));
        off = Math.min(off, ground - 1 - xy[i + 1]!);
      }
    }
    return off;
  };
  const running = restAt(human) + 150;
  let highest = 0;
  for (let clock = 400; clock < 1100; clock += 10) {
    const off = lift(running, clock);
    assert.ok(off > -0.01, `the runner is ${-off} under the ground at ${clock} ms`);
    highest = Math.max(highest, off);
  }
  assert.ok(highest > 1.5, "the runner never leaves the ground");
  // The spear is the lowest part of the standing human; a runner carries it off the ground.
  const spear = animals[human]!.parts.find((part) => !part.role && part.length > 100)!;
  const butt = Float64Array.from(spear.dense);
  scene.warp(human, running, 500)!(spear, butt);
  assert.ok(scene.groundY - Math.max(butt[1]!, butt[butt.length - 1]!) > 5, "the spear drags");
  assert.ok(scene.travel(running + 100) - scene.travel(running) > 25, "the world stands still");
  // Just before the end the movement is nearly gone, so the stand is not a jump.
  const { top, bottom } = extent(animals[human]!, scene.warp(human, scene.durationMs - 8, 5000));
  const authored = extent(animals[human]!, undefined);
  assert.ok(Math.abs(top - authored.top) < 0.5 && Math.abs(bottom - authored.bottom) < 0.5);
});

test("the two fish swim under the surface and above the sea bed", () => {
  for (const index of [0, 1]) {
    const elapsed = restAt(index);
    for (let clock = 200; clock < 1200; clock += 20) {
      const { top, bottom } = extent(animals[index]!, scene.warp(index, elapsed, clock));
      assert.ok(top > surfaceAt(elapsed), `${animals[index]!.key} breaks the surface`);
      assert.ok(bottom < scene.groundY, `${animals[index]!.key} touches the sea bed`);
    }
  }
});

test("the first wader has its head out of the water", () => {
  const elapsed = restAt(2);
  const { top } = extent(animals[2]!, scene.warp(2, elapsed, 400));
  assert.ok(top < surfaceAt(elapsed));
});

test("the shore lies between the fish and the first land animal", () => {
  assert.ok(scene.shoreX > scene.stops[1]!);
  assert.ok(scene.shoreX < scene.stops[4]!);
  for (let index = 4; index <= human; index++) {
    assert.ok(
      scene.elevation(scene.stops[index]!) > -1.2,
      `${animals[index]!.key} stands in water`,
    );
  }
});

test("the land animals keep their feet on the ground through a stride", () => {
  for (let index = 4; index < human; index++) {
    const elapsed = restAt(index);
    for (let clock = 200; clock < 1200; clock += 20) {
      const { bottom } = extent(animals[index]!, scene.warp(index, elapsed, clock));
      assert.ok(
        Math.abs(bottom - scene.groundY) <= 3.5,
        `${animals[index]!.key} is ${bottom - scene.groundY} from the ground at ${clock} ms`,
      );
    }
  }
});
