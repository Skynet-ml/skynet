import { describe, expect, it, vi } from "vitest";
import { holdQuitUntilStopped, stopServices } from "../src/main/quit";

function quitEvent() {
  return { preventDefault: vi.fn() };
}

describe("holdQuitUntilStopped", () => {
  it("holds the first quit request and starts the shutdown once", () => {
    const shutdown = vi.fn();
    const onBeforeQuit = holdQuitUntilStopped(shutdown);
    const first = quitEvent();
    onBeforeQuit(first);
    expect(first.preventDefault).toHaveBeenCalledTimes(1);
    expect(shutdown).toHaveBeenCalledTimes(1);
  });

  it("also holds a second quit request (a second Cmd+Q) while the shutdown runs", () => {
    const shutdown = vi.fn();
    const onBeforeQuit = holdQuitUntilStopped(shutdown);
    onBeforeQuit(quitEvent());
    const second = quitEvent();
    onBeforeQuit(second);
    const third = quitEvent();
    onBeforeQuit(third);
    expect(second.preventDefault).toHaveBeenCalledTimes(1);
    expect(third.preventDefault).toHaveBeenCalledTimes(1);
    expect(shutdown).toHaveBeenCalledTimes(1);
  });

  it("hides the app once, before the shutdown starts", () => {
    const calls: string[] = [];
    const onBeforeQuit = holdQuitUntilStopped(
      () => calls.push("shutdown"),
      () => calls.push("hide"),
    );
    onBeforeQuit(quitEvent());
    onBeforeQuit(quitEvent());
    expect(calls).toEqual(["hide", "shutdown"]);
  });
});

/** A stop that resolves only when the test calls `done`. */
function pendingStop(name: string, calls: string[]) {
  let done!: () => void;
  const promise = new Promise<void>((r) => (done = r));
  const stop = vi.fn(() => {
    calls.push(`start ${name}`);
    return promise;
  });
  return { stop, done: () => done() };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("stopServices", () => {
  it("stops the web server and the API at the same time", async () => {
    const calls: string[] = [];
    const fe = pendingStop("frontend", calls);
    const api = pendingStop("api", calls);
    const pg = pendingStop("postgres", calls);
    const sb = pendingStop("sandboxes", calls);
    const all = stopServices({ frontend: fe.stop, api: api.stop, postgres: pg.stop, sandboxes: sb.stop });
    await flush();
    // A slow web server must not delay the API stop.
    expect(calls).toEqual(["start frontend", "start api"]);
    fe.done();
    api.done();
    pg.done();
    sb.done();
    await all;
  });

  it("stops Postgres and the sandboxes only after the API is gone", async () => {
    const calls: string[] = [];
    const fe = pendingStop("frontend", calls);
    const api = pendingStop("api", calls);
    const pg = pendingStop("postgres", calls);
    const sb = pendingStop("sandboxes", calls);
    const all = stopServices({ frontend: fe.stop, api: api.stop, postgres: pg.stop, sandboxes: sb.stop });
    fe.done();
    await flush();
    expect(pg.stop).not.toHaveBeenCalled();
    api.done();
    await flush();
    expect(calls.slice(2)).toEqual(["start postgres", "start sandboxes"]);
    pg.done();
    sb.done();
    await all;
  });
});
