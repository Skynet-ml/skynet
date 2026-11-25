import assert from "node:assert/strict";
import test from "node:test";

import { ANIMALS } from "./evolution-animals.ts";
import { buildEvolution, morphFrame, parsePoints, restFrame } from "./evolution-poses.ts";

const evolution = buildEvolution(ANIMALS);

test("the chain runs from the first fish to us through ten animals", () => {
  assert.equal(ANIMALS.length, 10);
  assert.equal(ANIMALS[0]!.key, "haikouichthys");
  assert.equal(ANIMALS.at(-1)!.key, "homo-sapiens");
  assert.equal(evolution.transitions.length, 9);
});

test("every part parses into finite points inside the 200x160 box, with room for overflow", () => {
  for (const animal of ANIMALS) {
    for (const [, d] of animal.parts) {
      for (const p of parsePoints(d)) {
        assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y), `${animal.key}: bad point in ${d}`);
        assert.ok(p.x > -20 && p.x < 220 && p.y > -20 && p.y < 180, `${animal.key}: ${p.x},${p.y} far outside the box`);
      }
    }
  }
});

test("each step pairs most of the outline so the figure flows instead of cross-fading", () => {
  for (const tr of evolution.transitions) {
    const outlines = tr.from.parts.filter((p) => p.cls === "outline").length;
    const paired = tr.pairs.filter((p) => tr.from.parts[p.a]!.cls === "outline").length;
    assert.ok(paired >= Math.min(2, outlines), `${tr.from.key} -> ${tr.to.key} pairs ${paired} of ${outlines} outlines`);
  }
});

test("details stay on the body part they decorate, so a finger never flies off to become a toe", () => {
  for (const tr of evolution.transitions) {
    for (const { a, b } of tr.pairs) {
      const pa = tr.from.parts[a]!;
      const pb = tr.to.parts[b]!;
      assert.equal(pa.host, pb.host, `${tr.from.key} -> ${tr.to.key}: part ${a} -> ${b}`);
    }
  }
});

test("a tail fin never crosses the animal to become the sail", () => {
  const toDimetrodon = evolution.transitions[3]!;
  assert.equal(toDimetrodon.to.key, "dimetrodon");
  assert.ok(toDimetrodon.pairs.every((p) => p.b !== 2));
});

test("a new part outside the animal grows out of it, together with the parts that touch it", () => {
  // The sail with its first and last ray, then the spear shaft with its tip and the hand.
  const groups = [
    { tr: evolution.transitions[3]!, parts: [2, 3, 12], role: "body" },
    { tr: evolution.transitions[8]!, parts: [5, 6, 10], role: "fore-near" },
  ];
  for (const { tr, parts, role } of groups) {
    const anchors = parts.map((i) => tr.anchors.get(`b${i}`)!);
    assert.equal(tr.to.parts[anchors[0]!.part]!.role, role, tr.to.key);
    assert.ok(anchors.every((a) => a.x === anchors[0]!.x && a.y === anchors[0]!.y), tr.to.key);
  }
});

test("an unpaired detail follows the body part it sits on, so no eye floats beside a moving head", () => {
  // The face of the first ape: its head is new, so the eye and the mouth grow out of the body with it.
  const toProconsul = evolution.transitions[6]!;
  assert.equal(toProconsul.to.key, "proconsul");
  const face = toProconsul.arriving.filter((i) => toProconsul.to.parts[i]!.host === "head");
  assert.ok(face.length >= 3);
  for (const i of face) {
    const ride = toProconsul.rides.get(`b${i}`)!;
    assert.ok("bud" in ride && toProconsul.to.parts[ride.bud.b]!.role === "head", `part ${i}`);
  }
  for (const tr of evolution.transitions) {
    for (const [side, list, animal] of [["a", tr.leaving, tr.from], ["b", tr.arriving, tr.to]] as const) {
      for (const i of list) {
        const anchor = tr.anchors.get(side + i);
        const role = anchor ? animal.parts[anchor.part]!.role : animal.parts[i]!.host;
        const moves = tr.pairs.some((p) => animal.parts[p[side]]!.role === role);
        if (role && moves) assert.ok(tr.rides.has(side + i), `${tr.from.key} -> ${tr.to.key}: ${side}${i}`);
      }
    }
  }
});

test("the parts that follow a group leader only show above half size", () => {
  // The sail outline leads; its rays would run together into a grey patch at a small size.
  const toDimetrodon = evolution.transitions[3]!;
  const rays = toDimetrodon.arriving.filter((i) => toDimetrodon.anchors.get(`b${i}`)?.lead === false);
  assert.ok(rays.length >= 2);
  // The sail takes the whole morph, so half size is the middle. The rays of the tail fin leave there.
  const gone = toDimetrodon.leaving.filter((i) => {
    const anchor = toDimetrodon.anchors.get(`a${i}`);
    return anchor?.slow && !anchor.lead;
  });
  const shown = (t: number) => morphFrame(toDimetrodon, t).length;
  assert.equal(shown(0.51) - shown(0.49), rays.length - gone.length);
});

test("a long new part grows over the whole morph, so a sail or a spear never pops in", () => {
  for (const [n, lead] of [[3, 2], [8, 5]] as const) {
    const tr = evolution.transitions[n]!;
    assert.equal(tr.anchors.get(`b${lead}`)!.slow, true, tr.to.key);
    // At 40% the short parts of the old animal are gone and those of the new one do not show yet.
    const leaving = tr.leaving.filter((i) => tr.anchors.get(`a${i}`)?.slow).length;
    const leads = tr.arriving.filter((i) => tr.anchors.get(`b${i}`)?.slow && tr.anchors.get(`b${i}`)!.lead).length;
    assert.equal(morphFrame(tr, 0.4).length, tr.pairs.length + tr.buds.length + leaving + leads, tr.to.key);
  }
});

test("leaving parts clear early and arriving parts show late, so no ghost lingers mid-morph", () => {
  for (const tr of evolution.transitions) {
    // Only the leads of the long groups show: they change size at the body and do not fade in place.
    const leads = [...tr.anchors.values()].filter((a) => a.slow && a.lead).length;
    assert.equal(morphFrame(tr, 0.5).length, tr.pairs.length + tr.buds.length + leads, `${tr.from.key} -> ${tr.to.key} mid-frame`);
    assert.ok(morphFrame(tr, 0.5).every((p) => p.opacity === 1));
  }
});

test("a morph ends with every part of the next animal on screen", () => {
  for (const tr of evolution.transitions) {
    const end = morphFrame(tr, 1);
    assert.equal(end.length, tr.to.parts.length);
    assert.ok(end.every((p) => p.opacity === 1));
    assert.ok(end.length <= evolution.maxParts);
  }
  assert.equal(restFrame(evolution.animals.at(-1)!).length, evolution.animals.at(-1)!.parts.length);
});
