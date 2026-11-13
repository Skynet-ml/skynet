/*
 * Mounts useDictation in jsdom with a fake mic, recorder and API. node --test
 * cannot load .tsx, so use-dictation.render.test.ts spawns this through jiti
 * inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, afterEach, it } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
// jsdom has no canvas; the wave already draws nothing when there is no context.
dom.window.HTMLCanvasElement.prototype.getContext = (() => null) as never;
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  HTMLTextAreaElement: dom.window.HTMLTextAreaElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});
after(() => dom.window.close());

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

class FakeTrack {
  stopped = false;
  stop() {
    this.stopped = true;
  }
}

class FakeStream {
  track = new FakeTrack();
  getTracks() {
    return [this.track];
  }
}

const recorders: FakeRecorder[] = [];

// A WebM Cluster head (its ID, a 1-byte size, the Timecode ID): where a unit of
// audio starts. Fake chunks are latin1 text.
const C = "\x1f\x43\xb6\x75\x81\xe7";
const latin1 = (text: string) => Uint8Array.from(text, (ch) => ch.charCodeAt(0));

class FakeRecorder {
  /** The one type the browser records. jsdom says its vendor is Apple, so the hook tries MP4 first. */
  static only = "audio/webm;codecs=opus";
  /** The type the recorder says it records; empty, as some browsers say before start. */
  static reports = "";
  get mimeType() {
    return FakeRecorder.reports;
  }
  static isTypeSupported(type: string) {
    return type === FakeRecorder.only;
  }
  spoken = 0;
  state: "inactive" | "recording" = "inactive";
  ondataavailable: ((ev: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor() {
    recorders.push(this);
  }
  start() {
    this.state = "recording";
  }
  /** When set, stop() only queues the stop event, as a browser does; fireStop() sends it. */
  static lazyStop = false;
  stop() {
    this.state = "inactive";
    if (!FakeRecorder.lazyStop) this.onstop?.();
  }
  fireStop() {
    this.onstop?.();
  }
  /** By default the first chunk holds the header, and every chunk ends at a new unit. */
  speak(data?: string) {
    const text = data ?? (this.spoken === 0 ? `HEAD${C}voice${C}` : `voice${C}`);
    this.spoken += 1;
    this.ondataavailable?.({ data: new Blob([latin1(text)]) });
  }
}

let micPrompts: Array<Deferred<FakeStream>> = [];
Object.defineProperty(dom.window.navigator, "mediaDevices", {
  configurable: true,
  value: {
    getUserMedia: () => {
      const prompt = deferred<FakeStream>();
      micPrompts.push(prompt);
      return prompt.promise;
    },
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
Object.assign(globalThis, { MediaRecorder: FakeRecorder });

// The hook's 500 ms tick and its clock run by hand, so a test never waits.
let now = 1_000_000;
Date.now = () => now;
let ticks: Array<() => void> = [];
dom.window.setInterval = ((fn: () => void) => {
  ticks.push(fn);
  return ticks.length;
}) as typeof dom.window.setInterval;
dom.window.clearInterval = ((id?: number) => {
  if (id) ticks[id - 1] = () => undefined;
}) as typeof dom.window.clearInterval;

interface Call {
  partial: boolean;
  audio: Blob;
  signal?: AbortSignal | null;
  reply: Deferred<Response>;
}
let calls: Call[] = [];
globalThis.fetch = async (_url, init) => {
  const body = init?.body as FormData;
  const reply = deferred<Response>();
  calls.push({
    partial: body.get("partial") === "true",
    audio: body.get("audio") as Blob,
    signal: init?.signal,
    reply,
  });
  return reply.promise;
};

// The backend marks a preview answer as partial; the final call does not read the mark.
const ok = (text: string, partial = true) =>
  new Response(JSON.stringify({ text, provider: "test", partial }), {
    headers: { "Content-Type": "application/json" },
  });

const refusal = (status: number, code: string) =>
  new Response(JSON.stringify({ code, detail: code }), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// The composer test below needs these browser parts too.
Object.assign(globalThis, {
  localStorage: dom.window.localStorage,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;

const { createRoot } = await import("react-dom/client");
const { useDictation } = await import("./use-dictation");
const { Composer } = await import("./composer");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { UserPrefsProvider } = await import("@/features/settings/hooks/use-user-prefs");
const { msg } = await import("@/shared/lib/messages");
const { DictationWave } = await import("./dictation-wave");
type MessageKey = Parameters<typeof msg>[0];

type Dictation = ReturnType<typeof useDictation>;

async function mount() {
  const view: { current: Dictation | null; texts: string[] } = { current: null, texts: [] };
  function Harness() {
    view.current = useDictation({ onText: (text) => view.texts.push(text) });
    return null;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  await React.act(async () => root.render(<Harness />));
  return { view, unmount: () => React.act(async () => root.unmount()) };
}

async function startTake(view: { current: Dictation | null }) {
  await React.act(async () => {
    void view.current!.start();
  });
  const stream = new FakeStream();
  await React.act(async () => micPrompts.at(-1)!.resolve(stream));
  return stream;
}

/** Let the take grow past the first live update and run one tick. */
async function tickTo(ms: number) {
  now += ms;
  await React.act(async () => {
    ticks.forEach((tick) => tick());
    // The hook reads the take's bytes before it sends; Blob reads end on a later turn.
    for (let i = 0; i < 3; i++) await new Promise((r) => setImmediate(r));
  });
}

afterEach(() => {
  FakeRecorder.lazyStop = false;
  FakeRecorder.only = "audio/webm;codecs=opus";
  FakeRecorder.reports = "";
  recorders.length = 0;
  micPrompts = [];
  ticks = [];
  calls = [];
});

void it("starts one recorder when the mic button is clicked twice during the prompt", async () => {
  const { view, unmount } = await mount();
  await React.act(async () => {
    void view.current!.start();
    void view.current!.start();
  });
  assert.equal(view.current!.starting, true);
  assert.equal(micPrompts.length, 1);
  await React.act(async () => micPrompts[0]!.resolve(new FakeStream()));
  assert.equal(recorders.length, 1);
  assert.equal(view.current!.starting, false);
  assert.equal(view.current!.state.kind, "rec");
  await unmount();
});

void it("closes the mic when the host unmounts during the prompt", async () => {
  const { view, unmount } = await mount();
  await React.act(async () => {
    void view.current!.start();
  });
  await unmount();
  const stream = new FakeStream();
  await React.act(async () => micPrompts[0]!.resolve(stream));
  assert.equal(recorders.length, 0);
  assert.equal(stream.track.stopped, true);
});

void it("shows live words while recording", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  assert.equal(calls.length, 1);
  assert.equal(calls[0]!.partial, true);
  await React.act(async () => calls[0]!.reply.resolve(ok("hello there")));
  assert.equal(view.current!.live, "hello there");
  await unmount();
});

void it("an answer without the partial mark stops the take's live updates, and the final text still arrives", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  await React.act(async () => calls[0]!.reply.resolve(ok("hello", false)));
  const live = view.current!.live;
  recorders[0]!.speak();
  await tickTo(10_000);
  const partials = calls.length;
  await React.act(async () => view.current!.finish());
  const final = calls.at(-1)!;
  await React.act(async () => final.reply.resolve(ok("final words", false)));
  const texts = [...view.texts];
  await unmount();
  assert.equal(live, "hello");
  assert.equal(partials, 1);
  assert.equal(final.partial, false);
  assert.deepEqual(texts, ["final words"]);
});

void it("with no Intl.Segmenter, a take sends no live update and the final text still arrives", async () => {
  const segmenter = Intl.Segmenter;
  Object.assign(Intl, { Segmenter: undefined });
  try {
    const { view, unmount } = await mount();
    await startTake(view);
    recorders[0]!.speak();
    await tickTo(10_000);
    const partials = calls.length;
    await React.act(async () => view.current!.finish());
    const final = calls.at(-1)!;
    await React.act(async () => final.reply.resolve(ok("final words")));
    const live = view.current!.live;
    const texts = [...view.texts];
    await unmount();
    assert.equal(partials, 0);
    assert.equal(final.partial, false);
    assert.equal(live, "");
    assert.deepEqual(texts, ["final words"]);
  } finally {
    Object.assign(Intl, { Segmenter: segmenter });
  }
});

const sentText = async (call: Call) =>
  new TextDecoder("latin1").decode(new Uint8Array(await call.audio.arrayBuffer()));

/** Move the clock on, report one mic level as the wave would, and run one tick. */
async function tickWithLevel(view: { current: Dictation | null }, ms: number, level: number) {
  now += ms;
  view.current!.hearLevel(level);
  await tickTo(0);
}

void it("a silent stretch sends no live update, and the next sound sends only its own units", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one${C}`);
  // The wave keeps reading each 100 ms, so only the level itself marks the sound.
  // The first update is due at 2.5 s and the next one 3 s later.
  for (let i = 0; i < 25; i++) await tickWithLevel(view, 100, 0);
  const silent = calls.length;
  recorders[0]!.speak(`two${C}`);
  for (let i = 0; i < 29; i++) await tickWithLevel(view, 100, 0);
  await tickWithLevel(view, 100, 0.4);
  const sent = await sentText(calls[0]!);
  await unmount();
  assert.equal(silent, 0);
  assert.equal(sent, `HEAD${C}two`);
});

void it("speech before the wave's first level read still reaches the live words", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one${C}`);
  // The audio starts late, so its first read is silent and comes 3 s into the take.
  await tickWithLevel(view, 3000, 0);
  const sent = calls.length;
  await unmount();
  assert.equal(sent, 1);
});

void it("speech while the tab is hidden still reaches the live words after the tab shows", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}a${C}`);
  await tickWithLevel(view, 3000, 0.4);
  await React.act(async () => calls[0]!.reply.resolve(ok("a")));
  // A hidden tab reads no levels, so these updates go out without a fresh level.
  recorders[0]!.speak(`b${C}`);
  await tickTo(3000);
  await React.act(async () => calls[1]!.reply.resolve(ok("b")));
  recorders[0]!.speak(`c${C}`);
  await tickTo(3000);
  await React.act(async () => calls[2]!.reply.resolve(ok("c")));
  recorders[0]!.speak(`c2${C}`);
  await tickTo(3000);
  await React.act(async () => calls[3]!.reply.resolve(ok("c2")));
  // The last sound came before the chunk ahead of "d" arrived, so only the
  // gap in the reads counts this unit as sound.
  recorders[0]!.speak(`d${C}`);
  await tickWithLevel(view, 3000, 0);
  const sent = calls.length;
  await unmount();
  assert.equal(sent, 5);
});

// A unit's audio starts no earlier than the arrival of the chunk before the one
// with its first byte; 1 s more covers the encoder's delay.
for (const [soundAt, sent] of [
  [3000, 1],
  [2999, 0],
] as const) {
  void it(`sound ${soundAt} ms into the take, before a unit that arrives at 5000 ms, sends ${sent} update`, async () => {
    const { view, unmount } = await mount();
    await startTake(view);
    const t0 = now;
    // The wave reads silence every 100 ms up to `at` ms into the take, and the
    // recorder hands over a chunk each second.
    let nextChunk = 1000;
    const readUntil = async (at: number) => {
      while (now - t0 + 100 <= at) {
        const step = nextChunk < 5000 ? Math.min(100, nextChunk - (now - t0)) : 100;
        await tickWithLevel(view, step, 0);
        if (now - t0 === nextChunk && nextChunk < 5000) {
          recorders[0]!.speak("HEAD");
          nextChunk += 1000;
        }
      }
    };
    recorders[0]!.speak("HEAD");
    await readUntil(2900);
    now = t0 + soundAt;
    view.current!.hearLevel(0.4);
    await readUntil(4900);
    now = t0 + 5000;
    view.current!.hearLevel(0);
    // A long unit: its first byte comes only now, and it may hold audio from 4000 ms.
    recorders[0]!.speak(`${C}one${C}`);
    await readUntil(9000);
    const made = calls.length;
    await unmount();
    assert.equal(made, sent);
  });
}

void it("sound at the start of a take whose first chunk comes late sends an update", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  view.current!.hearLevel(0.4);
  for (let i = 0; i < 29; i++) await tickWithLevel(view, 100, 0);
  // The first chunk holds the header and a unit with audio from the start.
  recorders[0]!.speak(`HEAD${C}one${C}`);
  for (let i = 0; i < 60; i++) await tickWithLevel(view, 100, 0);
  const sent = calls.length;
  await unmount();
  assert.equal(sent, 1);
});

void it("with no fresh mic level, a live update goes out", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  view.current!.hearLevel(0);
  recorders[0]!.speak();
  // The wave stopped reading (a hidden tab), so its last level is stale.
  await tickTo(3000);
  const sent = calls.length;
  await unmount();
  assert.equal(sent, 1);
});

void it("later live updates send the header and only the new whole units, and join the words", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one`);
  recorders[0]!.speak(`${C}two`);
  await tickTo(3000);
  await React.act(async () => calls[0]!.reply.resolve(ok("hello")));
  recorders[0]!.speak(`${C}three`);
  recorders[0]!.speak(`${C}four`);
  await tickTo(3000);
  await React.act(async () => calls[1]!.reply.resolve(ok("there")));
  const live = view.current!.live;
  const sent = [await sentText(calls[0]!), await sentText(calls[1]!)];
  await unmount();
  // The last unit may still grow, so each update stops before it.
  assert.deepEqual(sent, [`HEAD${C}one`, `HEAD${C}two${C}three`]);
  assert.equal(live, "hello there");
});

void it("speech at the start of an MP4 fragment that is written late still reaches the live words", async () => {
  FakeRecorder.only = "audio/mp4";
  const { view, unmount } = await mount();
  await startTake(view);
  const t0 = now;
  const frag = (body: string) => `\x00\x00\x00\x11moof\x00\x00\x00\x10mfhd${body}`;
  // Each fragment holds 4 s of audio, and its moof comes 1.5 s after that audio
  // ends: fragment "b" holds 4000-8000 ms but its chunk comes at 9500 ms.
  const chunks = new Map([
    [500, `\x00\x00\x00\x08ftypmoov`],
    [5500, frag("a")],
    [9500, frag("b")],
    [13500, frag("c")],
  ]);
  while (now - t0 < 16000) {
    const at = now - t0 + 100;
    // The only sound is at 4200 ms, at the start of fragment "b".
    await tickWithLevel(view, 100, at === 4200 ? 0.4 : 0);
    const chunk = chunks.get(at);
    if (chunk) recorders[0]!.speak(chunk);
    for (const call of calls) await React.act(async () => call.reply.resolve(ok("word")));
  }
  const sent = await Promise.all(calls.map(sentText));
  await unmount();
  assert.equal(sent.at(-1), `\x00\x00\x00\x08ftypmoov${frag("b")}`);
});

void it("a chunk that starts inside a unit waits for the unit to end", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one`);
  recorders[0]!.speak(`${C}two`);
  await tickTo(3000);
  await React.act(async () => calls[0]!.reply.resolve(ok("hello")));
  // The browser cut this chunk in the middle of a unit.
  recorders[0]!.speak("more two");
  await tickTo(3000);
  const waited = calls.length;
  recorders[0]!.speak(`${C}three`);
  await tickTo(3000);
  const sent = await sentText(calls[1]!);
  await unmount();
  assert.equal(waited, 1);
  assert.equal(sent, `HEAD${C}twomore two`);
});

void it("live units too large for one update count as heard, and live updates go on", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one${C}`);
  await tickTo(3000);
  await React.act(async () => calls[0]!.reply.resolve(refusal(413, "transcription.too_large")));
  recorders[0]!.speak(`two${C}`);
  await tickTo(3000);
  const sent = await sentText(calls[1]!);
  await unmount();
  assert.equal(sent, `HEAD${C}two`);
});

void it("a failed live update sends its audio again with the next one", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one${C}`);
  await tickTo(3000);
  await React.act(async () => calls[0]!.reply.resolve(ok("hello")));
  recorders[0]!.speak(`two${C}`);
  await tickTo(3000);
  await React.act(async () => calls[1]!.reply.resolve(refusal(502, "transcription.failed")));
  recorders[0]!.speak(`three${C}`);
  await tickTo(3000);
  const sent = await sentText(calls[2]!);
  await unmount();
  assert.equal(sent, `HEAD${C}two${C}three`);
});

void it("sends no live update until a whole unit of audio is recorded", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak("HEAD");
  await tickTo(3000);
  recorders[0]!.speak(`${C}one`);
  await tickTo(3000);
  const early = calls.length;
  recorders[0]!.speak(`${C}two`);
  await tickTo(3000);
  const sent = await sentText(calls[0]!);
  await unmount();
  assert.equal(early, 0);
  assert.equal(sent, `HEAD${C}one`);
});

void it("a unit head that a chunk cuts in two is found on the next scan", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one${C.slice(0, 3)}`);
  await tickTo(3000);
  const early = calls.length;
  recorders[0]!.speak(`${C.slice(3)}two${C}`);
  await tickTo(3000);
  const sent = await sentText(calls[0]!);
  await unmount();
  assert.equal(early, 0);
  assert.equal(sent, `HEAD${C}one${C}two`);
});

void it("audio bytes that look like a cluster ID do not cut a unit", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak(`HEAD${C}one\x1f\x43\xb6\x75\x81\x12more${C}`);
  await tickTo(3000);
  const sent = await sentText(calls[0]!);
  await unmount();
  assert.equal(sent, `HEAD${C}one\x1f\x43\xb6\x75\x81\x12more`);
});

void it("Chinese and Japanese live words join with no space", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  await React.act(async () => calls[0]!.reply.resolve(ok("你好")));
  recorders[0]!.speak();
  await tickTo(3000);
  await React.act(async () => calls[1]!.reply.resolve(ok("世界")));
  recorders[0]!.speak();
  await tickTo(3000);
  await React.act(async () => calls[2]!.reply.resolve(ok("hello")));
  const live = view.current!.live;
  await unmount();
  assert.equal(live, "你好世界hello");
});

void it("names the file by the type the recorder reports", async () => {
  // No asked-for type is supported, so the browser picks one itself.
  FakeRecorder.only = "";
  FakeRecorder.reports = "audio/mp4";
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak("x");
  await React.act(async () => view.current!.finish());
  const call = calls.at(-1)!;
  await unmount();
  assert.equal((call.audio as File).name, "take.m4a");
  assert.equal(call.audio.type, "audio/mp4");
});

void it("an MP4 take sends whole fragments as take.m4a", async () => {
  FakeRecorder.only = "audio/mp4";
  const { view, unmount } = await mount();
  await startTake(view);
  const frag = (body: string) => `\x00\x00\x00\x11moof\x00\x00\x00\x10mfhd${body}`;
  recorders[0]!.speak(`\x00\x00\x00\x08ftypmoov`);
  recorders[0]!.speak(frag("a") + frag("b"));
  await tickTo(3000);
  const call = calls[0]!;
  const sent = await sentText(call);
  await unmount();
  assert.equal((call.audio as File).name, "take.m4a");
  assert.equal(call.audio.type, "audio/mp4");
  assert.equal(sent, `\x00\x00\x00\x08ftypmoov${frag("a")}`);
});

void it("finish while the take is still being read sends no live update", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  const read = deferred<ArrayBuffer>();
  const real = Blob.prototype.arrayBuffer;
  Blob.prototype.arrayBuffer = () => read.promise;
  try {
    await tickTo(3000);
    await React.act(async () => view.current!.finish());
  } finally {
    Blob.prototype.arrayBuffer = real;
  }
  await React.act(async () => read.resolve(latin1(`HEAD${C}voice${C}`).buffer as ArrayBuffer));
  const partials = calls.filter((call) => call.partial).length;
  await unmount();
  assert.equal(partials, 0);
  assert.equal(calls.length, 1, "only the final call");
});

for (const [name, end] of [
  ["finish", (d: Dictation) => d.finish()],
  ["cancel", (d: Dictation) => d.cancel()],
] as const) {
  void it(`${name} aborts the live update and ignores its late reply`, async () => {
    const { view, unmount } = await mount();
    await startTake(view);
    recorders[0]!.speak();
    await tickTo(3000);
    const partial = calls[0]!;
    await React.act(async () => end(view.current!));
    assert.equal(partial.signal?.aborted, true);
    await React.act(async () => partial.reply.resolve(ok("late words")));
    assert.equal(view.current!.live, "");
    await unmount();
  });
}

void it("unmount aborts the live update", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  await unmount();
  assert.equal(calls[0]!.signal?.aborted, true);
});

void it("sends the take when the browser stops the recorder by itself", async () => {
  const { view, unmount } = await mount();
  const stream = await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  const partial = calls[0]!;
  // The mic goes away: the browser stops the recorder with no call to finish().
  await React.act(async () => {
    recorders[0]!.state = "inactive";
    recorders[0]!.fireStop();
  });
  const aborted = partial.signal?.aborted;
  const kind = view.current!.state.kind;
  const final = calls.at(-1)!;
  const sent = calls.length;
  await tickTo(10_000);
  const afterTick = calls.length;
  await React.act(async () => final.reply.resolve(ok("kept words")));
  const texts = [...view.texts];
  await unmount();
  assert.equal(aborted, true);
  assert.equal(kind, "busy");
  assert.equal(sent, 2);
  assert.equal(final.partial, false);
  assert.equal(afterTick, 2);
  assert.equal(stream.track.stopped, true);
  assert.deepEqual(texts, ["kept words"]);
});

void it("unmount aborts the final request", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await React.act(async () => view.current!.finish());
  const final = calls.at(-1)!;
  const partial = final.partial;
  await unmount();
  assert.equal(partial, false);
  assert.equal(final.signal?.aborted, true);
});

void it("stops live updates after the rate limit answers 429", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  await React.act(async () =>
    calls[0]!.reply.resolve(
      new Response(JSON.stringify({ code: "rate_limit.exceeded", detail: "slow down" }), {
        status: 429,
      }),
    ),
  );
  await tickTo(10_000);
  await tickTo(10_000);
  assert.equal(calls.length, 1);
  await unmount();
});

void it("shows the rate limit's reason when the final transcript answers 429", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await React.act(async () => view.current!.finish());
  await React.act(async () =>
    calls.at(-1)!.reply.resolve(
      new Response(
        JSON.stringify({
          code: "rate_limit.exceeded",
          detail: "slow down",
          params: { retry_after: 30 },
        }),
        { status: 429 },
      ),
    ),
  );
  const state = view.current!.state;
  await unmount();
  // The server's localized reason, not the generic "try again" message.
  assert.deepEqual(state, {
    kind: "err",
    message: "Too many requests. Please wait 30 seconds and try again.",
  });
});

void it("releases the mic when the recorder cannot start", async () => {
  const { view, unmount } = await mount();
  const start = FakeRecorder.prototype.start;
  FakeRecorder.prototype.start = () => {
    throw new Error("NotSupportedError");
  };
  let stream: FakeStream;
  try {
    stream = await startTake(view);
  } finally {
    FakeRecorder.prototype.start = start;
  }
  const state = view.current!.state;
  await unmount();
  assert.equal(stream.track.stopped, true);
  assert.deepEqual(state, { kind: "err", message: msg("agent.composer.mic_unavailable") });
});

void it("stops live updates after a reason that retrying cannot fix", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  await React.act(async () =>
    calls[0]!.reply.resolve(refusal(503, "transcription.budget_exhausted")),
  );
  await tickTo(10_000);
  await tickTo(10_000);
  assert.equal(calls.length, 1);
  await unmount();
});

void it("sends no second live update while one is still pending", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await tickTo(3000);
  recorders[0]!.speak();
  await tickTo(10_000);
  await tickTo(10_000);
  assert.equal(calls.length, 1);
  await React.act(async () => calls[0]!.reply.resolve(ok("hello")));
  await tickTo(10_000);
  assert.equal(calls.length, 2);
  await unmount();
});

void it("stops live updates after three failures in a row", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  for (let i = 0; i < 3; i++) {
    await tickTo(i === 0 ? 3000 : 10_000);
    assert.equal(calls.length, i + 1);
    await React.act(async () => calls[i]!.reply.resolve(refusal(502, "transcription.failed")));
  }
  await tickTo(10_000);
  await tickTo(10_000);
  assert.equal(calls.length, 3);
  await unmount();
});

void it("keeps live updates going when a success breaks the run of failures", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  const replies = [
    refusal(502, "transcription.failed"),
    refusal(502, "transcription.failed"),
    ok("hello"),
    refusal(502, "transcription.failed"),
    refusal(502, "transcription.failed"),
  ];
  for (const [i, reply] of replies.entries()) {
    // Each update after the success needs new audio to send.
    recorders[0]!.speak();
    await tickTo(i === 0 ? 3000 : 10_000);
    assert.equal(calls.length, i + 1);
    await React.act(async () => calls[i]!.reply.resolve(reply));
  }
  recorders[0]!.speak();
  await tickTo(10_000);
  assert.equal(calls.length, 6);
  await unmount();
});

for (const [name, reply] of [
  ["fails", () => refusal(502, "transcription.failed")],
  ["returns no words", () => ok("  ")],
] as const) {
  void it(`clears the live words and shows an error when the final call ${name}`, async () => {
    const { view, unmount } = await mount();
    await startTake(view);
    recorders[0]!.speak();
    await tickTo(3000);
    await React.act(async () => calls[0]!.reply.resolve(ok("hello there")));
    assert.equal(view.current!.live, "hello there");
    await React.act(async () => view.current!.finish());
    const final = calls.at(-1)!;
    assert.equal(final.partial, false);
    await React.act(async () => final.reply.resolve(reply()));
    assert.equal(view.current!.state.kind, "err");
    assert.equal(view.current!.live, "");
    assert.deepEqual(view.texts, []);
    await unmount();
  });
}

void it("drops the final text when the host unmounts while the final call runs", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await React.act(async () => view.current!.finish());
  const final = calls.at(-1)!;
  assert.equal(final.partial, false);
  await unmount();
  await React.act(async () => final.reply.resolve(ok("late words")));
  assert.deepEqual(view.texts, []);
});

void it("sends no final call when the host unmounts before the stop event", async () => {
  FakeRecorder.lazyStop = true;
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await React.act(async () => view.current!.finish());
  await unmount();
  await React.act(async () => recorders[0]!.fireStop());
  assert.equal(calls.filter((call) => !call.partial).length, 0);
  assert.deepEqual(view.texts, []);
});

/** A recorder the browser refuses to build, with or without a mime type. */
class BrokenRecorder {
  static isTypeSupported() {
    return true;
  }
  constructor() {
    throw new Error("NotSupportedError");
  }
}

/** Hold the window's timeouts by hand, so a test fires the error dismiss itself. */
function fakeTimeouts() {
  const pending = new Map<number, () => void>();
  let next = 1;
  const { setTimeout: set, clearTimeout: clear } = dom.window;
  dom.window.setTimeout = ((fn: () => void) => {
    pending.set(next, fn);
    return next++;
  }) as typeof dom.window.setTimeout;
  dom.window.clearTimeout = ((id?: number) => {
    if (id) pending.delete(id);
  }) as typeof dom.window.clearTimeout;
  return {
    pending,
    restore() {
      dom.window.setTimeout = set;
      dom.window.clearTimeout = clear;
    },
  };
}

void it("shows an error and releases the mic when the recorder cannot be built", async () => {
  Object.assign(globalThis, { MediaRecorder: BrokenRecorder });
  try {
    const { view, unmount } = await mount();
    const stream = await startTake(view);
    const state = view.current!.state;
    const built = recorders.length;
    await unmount();
    assert.deepEqual(state, { kind: "err", message: msg("agent.composer.mic_unavailable") });
    assert.equal(built, 0);
    assert.equal(stream.track.stopped, true);
  } finally {
    Object.assign(globalThis, { MediaRecorder: FakeRecorder });
  }
});

void it("a second failure restarts the error's dismiss timer", async () => {
  const timers = fakeTimeouts();
  Object.assign(globalThis, { MediaRecorder: BrokenRecorder });
  try {
    const { view, unmount } = await mount();
    await startTake(view);
    const [first] = timers.pending.keys();
    await startTake(view);
    const waiting = [...timers.pending.keys()];
    await React.act(async () => timers.pending.get(waiting[0]!)!());
    const kind = view.current!.state.kind;
    await unmount();
    assert.equal(waiting.length, 1);
    assert.notEqual(waiting[0], first);
    assert.equal(kind, "idle");
  } finally {
    timers.restore();
    Object.assign(globalThis, { MediaRecorder: FakeRecorder });
  }
});

void it("unmount clears the error's dismiss timer", async () => {
  const timers = fakeTimeouts();
  Object.assign(globalThis, { MediaRecorder: BrokenRecorder });
  try {
    const { view, unmount } = await mount();
    await startTake(view);
    const before = timers.pending.size;
    await unmount();
    assert.equal(before, 1);
    assert.equal(timers.pending.size, 0);
  } finally {
    timers.restore();
    Object.assign(globalThis, { MediaRecorder: FakeRecorder });
  }
});

/** Mount a composer in the page, so focus moves as it does in a browser. */
async function mountComposer() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <LocaleProvider initialLocale="en">
        <UserPrefsProvider>
          <Composer value="" onChange={() => {}} onSubmit={() => {}} />
        </UserPrefsProvider>
      </LocaleProvider>,
    ),
  );
  const button = (key: MessageKey) =>
    container.querySelector<HTMLButtonElement>(`button[aria-label="${msg(key)}"]`);
  const record = async () => {
    await React.act(async () => button("agent.composer.record")!.click());
    await React.act(async () => micPrompts.at(-1)!.resolve(new FakeStream()));
  };
  const frame = () => React.act(() => new Promise<void>((r) => setTimeout(r, 50)));
  return {
    container,
    button,
    record,
    frame,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

void it("Cancel puts focus back in the textarea", async () => {
  const composer = await mountComposer();
  await composer.record();
  const cancel = composer.button("agent.composer.record_cancel")!;
  cancel.focus();
  await React.act(async () => cancel.click());
  await composer.frame();
  const textarea = composer.container.querySelector("textarea");
  const focused = document.activeElement === textarea;
  await composer.unmount();
  assert.ok(textarea);
  assert.equal(focused, true);
});

void it("focus returns to the textarea when a failed take's error closes", async () => {
  const timers = fakeTimeouts();
  try {
    const composer = await mountComposer();
    await composer.record();
    recorders[0]!.speak();
    const finish = composer.button("agent.composer.record_finish")!;
    finish.focus();
    await React.act(async () => finish.click());
    // Finish turns into the disabled mic. A browser then moves focus to the
    // body; jsdom keeps it on the button, so the test does that step.
    const disabled = composer.button("agent.composer.record")!.disabled;
    const elsewhere = document.createElement("input");
    document.body.append(elsewhere);
    elsewhere.focus();
    elsewhere.remove();
    const dropped = document.activeElement === document.body;
    await React.act(async () => calls.at(-1)!.reply.resolve(refusal(502, "transcription.failed")));
    await React.act(async () => [...timers.pending.values()].forEach((fire) => fire()));
    await composer.frame();
    const textarea = composer.container.querySelector("textarea");
    const focused = document.activeElement === textarea;
    await composer.unmount();
    assert.equal(disabled, true);
    assert.equal(dropped, true);
    assert.ok(textarea);
    assert.equal(focused, true);
  } finally {
    timers.restore();
  }
});

void it("shows the recording strip and the live words in the composer", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <LocaleProvider initialLocale="en">
        <UserPrefsProvider>
          <Composer value="" onChange={() => {}} onSubmit={() => {}} />
        </UserPrefsProvider>
      </LocaleProvider>,
    ),
  );
  const mic = container.querySelector(`button[aria-label="${msg("agent.composer.record")}"]`);
  assert.ok(mic, container.innerHTML);
  await React.act(async () => (mic as HTMLButtonElement).click());
  // A second click during the mic prompt must not start a second take.
  assert.equal((mic as HTMLButtonElement).disabled, true);
  await React.act(async () => micPrompts.at(-1)!.resolve(new FakeStream()));
  recorders[0]!.speak();
  await tickTo(3000);
  const words = "please summarize the failing tests in the last run";
  await React.act(async () => calls[0]!.reply.resolve(ok(words)));

  assert.equal(container.querySelector("textarea"), null);
  const cancel = container.querySelector(
    `button[aria-label="${msg("agent.composer.record_cancel")}"]`,
  );
  assert.ok(cancel, container.innerHTML);
  // The voice strip is the cancel button's row.
  const strip = cancel.parentElement!;
  assert.ok(strip.querySelector("canvas"), container.innerHTML);
  // Screen readers hear the state from an always-mounted status outside the strip.
  assert.equal(strip.closest('[role="status"]'), null);
  const announced = () =>
    [...container.querySelectorAll('p[role="status"]')].map((p) => p.textContent).join("|");
  assert.match(announced(), new RegExp(msg("agent.composer.recording")));
  const live = strip.querySelector('span[dir="auto"]');
  assert.equal(live?.textContent, words);
  // The fade must follow the words' own direction, not the page's.
  assert.match(live!.className, /\[&:dir\(rtl\)\]:\[mask-image:/);
  assert.doesNotMatch(live!.className, /(^|\s)rtl:\[mask/);
  // The timer changes each second, so screen readers must not hear it in the live region.
  const timer = strip.querySelector('span[dir="ltr"]');
  assert.equal(timer?.textContent, "0:03");
  assert.equal(timer?.getAttribute("aria-hidden"), "true");

  const finish = container.querySelector(
    `button[aria-label="${msg("agent.composer.record_finish")}"]`,
  );
  assert.ok(finish, container.innerHTML);
  await React.act(async () => (finish as HTMLButtonElement).click());
  assert.equal(calls.at(-1)!.partial, false);
  assert.ok(strip.textContent?.includes(msg("agent.composer.transcribing")), strip.innerHTML);
  assert.match(announced(), new RegExp(msg("agent.composer.transcribing")));
  assert.equal(
    container.querySelector(`button[aria-label="${msg("agent.composer.record_cancel")}"]`),
    null,
  );
  const settled = strip.querySelector('span[dir="auto"] > span');
  assert.match(settled!.className, /(^|\s)text-muted-foreground(\s|$)/);
  await React.act(async () => root.unmount());
  container.remove();
});

// WebKit can start the context suspended. Its analyser then reads flat silence,
// which the gate would take as fresh silence while the user speaks.
for (const [state, reported] of [
  ["suspended", false],
  ["running", true],
] as const) {
  void it(`the wave ${reported ? "reports" : "does not report"} the level of a ${state} audio context`, async () => {
    class FakeAudio {
      state = state;
      resume = () => new Promise<void>(() => undefined);
      close = async () => undefined;
      createAnalyser = () => ({
        fftSize: 0,
        // A loud frame: every sample far from the 128 midpoint.
        getByteTimeDomainData: (frame: Uint8Array) => frame.fill(255),
      });
      createMediaStreamSource = () => ({ connect: () => undefined });
    }
    const proto = dom.window.HTMLCanvasElement.prototype;
    const getContext = proto.getContext;
    // jsdom has no canvas, so a 2D context that ignores every draw call stands in.
    const pen = new Proxy({}, { get: () => () => undefined, set: () => true });
    proto.getContext = (() => pen) as never;
    Object.assign(dom.window, { AudioContext: FakeAudio });
    const levels: number[] = [];
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await React.act(async () =>
        root.render(
          <DictationWave stream={new FakeStream() as never} onLevel={(l) => levels.push(l)} />,
        ),
      );
      await React.act(async () => root.unmount());
    } finally {
      proto.getContext = getContext;
      Object.assign(dom.window, { AudioContext: undefined });
    }
    if (reported) assert.ok(levels.length > 0 && levels.every((l) => l > 0), `${levels}`);
    else assert.deepEqual(levels, []);
  });
}
