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
  /** Hand the hook one chunk of recorded audio. */
  speak(data = "voice") {
    this.ondataavailable?.({ data: new Blob([data]) });
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
  audio: Blob;
  signal?: AbortSignal | null;
  reply: Deferred<Response>;
}
let calls: Call[] = [];
globalThis.fetch = async (_url, init) => {
  const body = init?.body as FormData;
  const reply = deferred<Response>();
  calls.push({
    audio: body.get("audio") as Blob,
    signal: init?.signal,
    reply,
  });
  return reply.promise;
};

const ok = (text: string) =>
  new Response(JSON.stringify({ text, provider: "test" }), {
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

/** Move the clock on and run one tick of the hook's timer. */
async function tickTo(ms: number) {
  now += ms;
  await React.act(async () => ticks.forEach((tick) => tick()));
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

void it("records with no request, then sends the whole take once and hands back its text", async () => {
  const { view, unmount } = await mount();
  const stream = await startTake(view);
  recorders[0]!.speak("one ");
  await tickTo(5000);
  recorders[0]!.speak("two");
  await tickTo(5000);
  const whileRecording = calls.length;
  const seconds = view.current!.seconds;
  await React.act(async () => view.current!.finish());
  const kind = view.current!.state.kind;
  const sent = calls.length;
  const audio = await calls[0]!.audio.text();
  await React.act(async () => calls[0]!.reply.resolve(ok("  hello there ")));
  const after = view.current!.state.kind;
  const texts = [...view.texts];
  await unmount();
  assert.equal(whileRecording, 0);
  assert.equal(seconds, 10);
  assert.equal(kind, "busy");
  assert.equal(sent, 1);
  assert.equal(audio, "one two");
  assert.equal(stream.track.stopped, true);
  assert.equal(after, "idle");
  assert.deepEqual(texts, ["hello there"]);
});

void it("cancel sends nothing and releases the mic", async () => {
  const { view, unmount } = await mount();
  const stream = await startTake(view);
  recorders[0]!.speak();
  await React.act(async () => view.current!.cancel());
  const kind = view.current!.state.kind;
  await unmount();
  assert.equal(calls.length, 0);
  assert.equal(kind, "idle");
  assert.equal(stream.track.stopped, true);
});

void it("sends the take when the browser stops the recorder by itself", async () => {
  const { view, unmount } = await mount();
  const stream = await startTake(view);
  recorders[0]!.speak();
  // The mic goes away: the browser stops the recorder with no call to finish().
  await React.act(async () => {
    recorders[0]!.state = "inactive";
    recorders[0]!.fireStop();
  });
  const kind = view.current!.state.kind;
  const sent = calls.length;
  await tickTo(10_000);
  const afterTick = calls.length;
  await React.act(async () => calls[0]!.reply.resolve(ok("kept words")));
  const texts = [...view.texts];
  await unmount();
  assert.equal(kind, "busy");
  assert.equal(sent, 1);
  assert.equal(afterTick, 1);
  assert.equal(stream.track.stopped, true);
  assert.deepEqual(texts, ["kept words"]);
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


void it("unmount aborts the final request", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await React.act(async () => view.current!.finish());
  const final = calls.at(-1)!;
  await unmount();
  assert.equal(final.signal?.aborted, true);
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


for (const [name, reply] of [
  ["fails", () => refusal(502, "transcription.failed")],
  ["returns no words", () => ok("  ")],
] as const) {
  void it(`shows an error and inserts nothing when the final call ${name}`, async () => {
    const { view, unmount } = await mount();
    await startTake(view);
    recorders[0]!.speak();
    await React.act(async () => view.current!.finish());
    await React.act(async () => calls.at(-1)!.reply.resolve(reply()));
    const state = view.current!.state;
    await unmount();
    assert.deepEqual(state, { kind: "err", message: msg("agent.composer.transcribe_failed") });
    assert.deepEqual(view.texts, []);
  });
}

void it("drops the final text when the host unmounts while the final call runs", async () => {
  const { view, unmount } = await mount();
  await startTake(view);
  recorders[0]!.speak();
  await React.act(async () => view.current!.finish());
  const final = calls.at(-1)!;
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
  assert.equal(calls.length, 0);
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

void it("shows the recording strip in the composer", async () => {
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
  // No preview request goes out while the user records.
  assert.equal(calls.length, 0);
  // The timer changes each second, so screen readers must not hear it in the live region.
  const timer = strip.querySelector('span[dir="ltr"]');
  assert.equal(timer?.textContent, "0:03");
  assert.equal(timer?.getAttribute("aria-hidden"), "true");

  const finish = container.querySelector(
    `button[aria-label="${msg("agent.composer.record_finish")}"]`,
  );
  assert.ok(finish, container.innerHTML);
  await React.act(async () => (finish as HTMLButtonElement).click());
  assert.equal(calls.length, 1);
  assert.ok(strip.textContent?.includes(msg("agent.composer.transcribing")), strip.innerHTML);
  assert.match(announced(), new RegExp(msg("agent.composer.transcribing")));
  assert.equal(
    container.querySelector(`button[aria-label="${msg("agent.composer.record_cancel")}"]`),
    null,
  );
  assert.match(strip.querySelector("canvas")!.className, /(^|\s)text-muted-foreground(\s|$)/);
  await React.act(async () => root.unmount());
  container.remove();
});


/** A 2D context that records the line the wave draws. */
function fakePen() {
  const pen = {
    strokeStyle: "",
    globalAlpha: 1,
    lineWidth: 0,
    lineJoin: "",
    strokes: [] as Array<{ alpha: number; width: number; ys: number[] }>,
    ys: [] as number[],
    clearRect() {},
    setTransform() {},
    beginPath() {
      pen.ys = [];
    },
    moveTo(_x: number, y: number) {
      pen.ys.push(y);
    },
    lineTo(_x: number, y: number) {
      pen.ys.push(y);
    },
    stroke() {
      pen.strokes.push({ alpha: pen.globalAlpha, width: pen.lineWidth, ys: pen.ys });
    },
  };
  return pen;
}

/** Render the wave with a fake pen, a 100 by 24 canvas and a loud mic. */
async function drawWave(props: { stream: unknown; processing?: boolean }, frames = 1) {
  class FakeAudio {
    state = "running";
    resume = async () => undefined;
    close = async () => undefined;
    createAnalyser = () => ({
      fftSize: 0,
      // A loud frame: every sample far from the 128 midpoint.
      getByteTimeDomainData: (frame: Uint8Array) => frame.fill(255),
    });
    createMediaStreamSource = () => ({ connect: () => undefined });
  }
  const proto = dom.window.HTMLCanvasElement.prototype;
  const { getContext, getBoundingClientRect } = proto;
  const pen = fakePen();
  proto.getContext = (() => pen) as never;
  proto.getBoundingClientRect = (() => ({ width: 100, height: 24 })) as never;
  const queued: FrameRequestCallback[] = [];
  const raf = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = (fn) => queued.push(fn);
  Object.assign(dom.window, { AudioContext: FakeAudio });
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await React.act(async () =>
      root.render(<DictationWave stream={props.stream as never} processing={props.processing} />),
    );
    for (let i = 1; i < frames; i++) queued.shift()?.(0);
    await React.act(async () => root.unmount());
  } finally {
    proto.getContext = getContext;
    proto.getBoundingClientRect = getBoundingClientRect;
    globalThis.requestAnimationFrame = raf;
    Object.assign(dom.window, { AudioContext: undefined });
  }
  return pen.strokes;
}

void it("the wave draws one thin line that moves with the mic level, flat at both ends", async () => {
  const strokes = await drawWave({ stream: new FakeStream() }, 6);
  const last = strokes.at(-1)!;
  assert.equal(strokes.length, 6);
  assert.equal(last.width, 1.5);
  assert.equal(last.alpha, 0.7);
  // One point every 2 px across 100 px.
  assert.equal(last.ys.length, 51);
  assert.equal(last.ys[0], 12);
  assert.ok(Math.abs(last.ys.at(-1)! - 12) < 1e-9);
  assert.ok(last.ys.some((y) => Math.abs(y - 12) > 1), `${last.ys}`);
});

void it("while processing, the wave draws a flat, dim line", async () => {
  const strokes = await drawWave({ stream: null, processing: true });
  assert.equal(strokes.length, 1);
  assert.ok(Math.abs(strokes[0]!.alpha - 0.35 * 0.7) < 1e-9);
  assert.ok(strokes[0]!.ys.every((y) => y === 12));
});
