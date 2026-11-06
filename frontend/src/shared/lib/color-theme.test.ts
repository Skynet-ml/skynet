import { test } from "node:test";
import assert from "node:assert/strict";
import {
  COLOR_THEME_COOKIE,
  COLOR_THEME_INIT_SCRIPT,
  COLOR_THEME_STORAGE_KEY,
  readColorTheme,
  writeColorTheme,
} from "./color-theme.ts";

/** A Map-backed Storage: one per origin, so one per port. */
function memoryStorage(initial: Record<string, string> = {}): Storage {
  const store = new Map(Object.entries(initial));
  return {
    get length() {
      return store.size;
    },
    key: (i: number) => [...store.keys()][i] ?? null,
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
}

/** A `document.cookie` stand-in. Cookies ignore the port, so every port shares one jar. */
function cookieJar(initial: Record<string, string> = {}) {
  const jar = new Map(Object.entries(initial));
  return {
    get cookie() {
      return [...jar].map(([k, v]) => `${k}=${v}`).join("; ");
    },
    set cookie(raw: string) {
      const [pair] = raw.split(";");
      const eq = pair.indexOf("=");
      jar.set(pair.slice(0, eq), pair.slice(eq + 1));
    },
  };
}

/** Point the browser globals at one port's localStorage and the shared cookie jar. */
function onPort(local: Storage, cookies: ReturnType<typeof cookieJar>, prefersDark = false) {
  const root = { classList: new Set<string>(), style: { colorScheme: "" } };
  Object.defineProperty(globalThis, "localStorage", { value: local, configurable: true, writable: true });
  Object.assign(globalThis, {
    window: { localStorage: local, matchMedia: () => ({ matches: prefersDark }) },
    document: {
      get cookie() {
        return cookies.cookie;
      },
      set cookie(raw: string) {
        cookies.cookie = raw;
      },
      documentElement: {
        classList: { add: (c: string) => root.classList.add(c) },
        style: root.style,
      },
    },
  });
  return root;
}

/** Run the pre-paint script as the browser would and return whether it painted dark. */
function initPaintsDark(): boolean {
  const root = (globalThis as unknown as { document: { documentElement: { style: { colorScheme: string } } } })
    .document.documentElement;
  new Function(COLOR_THEME_INIT_SCRIPT)();
  return root.style.colorScheme === "dark";
}

test("a theme chosen on one port is read on another port", () => {
  const cookies = cookieJar();
  onPort(memoryStorage(), cookies);
  writeColorTheme("dark");

  onPort(memoryStorage(), cookies);
  assert.equal(readColorTheme(), "dark");
  assert.equal(initPaintsDark(), true);
});

test("System on a new port overrides an older choice left on another port", () => {
  const cookies = cookieJar();
  const portA = memoryStorage();
  onPort(portA, cookies);
  writeColorTheme("dark");

  onPort(memoryStorage(), cookies);
  writeColorTheme("system");

  onPort(portA, cookies);
  assert.equal(portA.getItem(COLOR_THEME_STORAGE_KEY), "dark");
  assert.equal(readColorTheme(), "system");
  assert.equal(initPaintsDark(), false);
});

test("a choice saved before the cookie existed is still read", () => {
  onPort(memoryStorage({ [COLOR_THEME_STORAGE_KEY]: "dark" }), cookieJar());
  assert.equal(readColorTheme(), "dark");
  assert.equal(initPaintsDark(), true);
});

test("the legacy gray value in the cookie reads as dark", () => {
  onPort(memoryStorage(), cookieJar({ [COLOR_THEME_COOKIE]: "gray" }));
  assert.equal(readColorTheme(), "dark");
  assert.equal(initPaintsDark(), true);
});

test("System follows the OS preference", () => {
  onPort(memoryStorage(), cookieJar({ [COLOR_THEME_COOKIE]: "system" }), true);
  assert.equal(readColorTheme(), "system");
  assert.equal(initPaintsDark(), true);
});
