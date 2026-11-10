/*
 * Mounts the menu in jsdom. composer-model-menu.render.test.ts spawns it
 * through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { CatalogModel } from "@/shared/types/api";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  HTMLInputElement: dom.window.HTMLInputElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  customElements: dom.window.customElements,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
const matchMedia = (query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => true,
});
dom.window.matchMedia = matchMedia;
Object.assign(globalThis, { matchMedia });
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");

after(() => dom.window.close());

const CLI: CatalogModel[] = [
  {
    value: "subscription/codex/openai/gpt-5.5",
    label: "GPT-5.5",
    display_name: "GPT-5.5",
    provider: "openai",
    supports_thinking: true,
    supports_vision: true,
    supports_fast: true,
    reasoning_efforts: ["low", "medium", "high"],
    available: true,
  },
  {
    value: "subscription/codex/openai/gpt-5.4-mini",
    label: "GPT-5.4 mini",
    display_name: "GPT-5.4 mini",
    provider: "openai",
    supports_thinking: true,
    supports_vision: true,
    reasoning_efforts: ["low", "medium", "high"],
    available: true,
  },
];

const WEB: CatalogModel[] = CLI.map((model) => ({
  ...model,
  value: model.value.replace("subscription/codex/", ""),
  featured: true,
}));
const SEARCHABLE: CatalogModel = {
  ...WEB[0]!,
  value: "openrouter/anthropic/search-candidate",
  label: "Search candidate",
  display_name: "Search candidate",
  provider: "anthropic",
  featured: false,
};
const UNAVAILABLE: CatalogModel = {
  ...SEARCHABLE,
  value: "openrouter/anthropic/unavailable-candidate",
  label: "Unavailable candidate",
  display_name: "Unavailable candidate",
  available: false,
};
const WEB_ALL = [...WEB, SEARCHABLE, UNAVAILABLE];
globalThis.fetch = async (url) =>
  new Response(
    JSON.stringify(
      String(url).includes("/models/fast-mode")
        ? { supports_fast: String(url).includes("claude-opus-5.5") }
        : { providers: [], models: WEB_ALL },
    ),
    {
      headers: { "Content-Type": "application/json" },
    },
  );
const { ComposerModelMenu } = await import("./composer-model-menu");

interface Calls {
  model: Array<string | null>;
  effort: Array<string | null>;
  fast: boolean[];
}

async function mount(
  effort: string | null = null,
  models: CatalogModel[] | null = CLI,
  allModels?: CatalogModel[],
) {
  const calls: Calls = { model: [], effort: [], fast: [] };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  function ControlledMenu() {
    const [value, setValue] = React.useState<string | null>((models ?? WEB)[0]!.value);
    const [level, setLevel] = React.useState(effort);
    const [fast, setFast] = React.useState(false);
    return (
      <TooltipProvider>
        <ComposerModelMenu
          value={value}
          onChange={(m) => {
            calls.model.push(m);
            setValue(m);
          }}
          effort={level}
          onEffortChange={(e) => {
            calls.effort.push(e);
            setLevel(e);
          }}
          fast={fast}
          onFastChange={(on) => {
            calls.fast.push(on);
            setFast(on);
          }}
          models={models ?? undefined}
          allModels={allModels}
        />
      </TooltipProvider>
    );
  }
  await React.act(async () => {
    root.render(<ControlledMenu />);
  });
  const trigger = container.querySelector<HTMLButtonElement>("button")!;
  await React.act(async () => {
    if (!trigger.disabled) trigger.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
  return {
    calls,
    trigger,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

function byLabel(label: string): HTMLButtonElement | null {
  return document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
}

// Each row's text starts with the provider logo's title.
function menuRows(): string[] {
  return [...document.querySelectorAll('[role="menuitem"]')].map((el) =>
    (el.textContent ?? "").replace(/^OpenAI/, ""),
  );
}

async function click(el: Element) {
  await React.act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
}

void test("a CLI model opens the slider first with a visible model selector", async () => {
  const m = await mount();
  try {
    const slider = document.querySelector('[role="slider"]')!;
    assert.ok(slider);
    assert.deepEqual(menuRows(), []);
    assert.equal((document.querySelector('[role="menu"]') as HTMLElement).style.zIndex, "50");
    const selector = byLabel("Choose a model: GPT-5.5")!;
    assert.equal(selector.getAttribute("aria-expanded"), "false");
    assert.ok(selector.querySelector("svg"));
    assert.equal(selector.querySelector("[dir=ltr]")?.textContent?.trim(), "GPT-5.5");
    assert.ok(selector.compareDocumentPosition(slider) & Node.DOCUMENT_POSITION_FOLLOWING);
    // The level hint stays for screen readers only; no end labels show.
    const hint = document.getElementById(slider.getAttribute("aria-describedby")!)!;
    assert.equal(hint.className, "sr-only");
    assert.equal(hint.textContent, "The model's default level");
    assert.doesNotMatch(document.body.textContent ?? "", /Faster|Smarter/);
  } finally {
    await m.unmount();
  }
});

for (const models of [CLI, null]) {
  void test(`model search filters ${models ? "local" : "web"} rows and supports keyboard selection`, async () => {
    const m = await mount(null, models);
    try {
      await click(byLabel("Choose a model: GPT-5.5")!);
      const input = document.querySelector<HTMLInputElement>('input[aria-label="Search models…"]')!;
      assert.equal(document.activeElement, input);
      const setSearch = async (value: string) => {
        await React.act(async () => {
          Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!.call(input, value);
          input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
        });
      };
      await setSearch("  MiNI  ");
      assert.deepEqual(menuRows(), ["GPT-5.4 mini"]);
      assert.equal(document.activeElement, input);
      await React.act(async () => {
        const result = document.querySelector('[role="menuitem"]')!;
        for (const type of ["pointermove", "pointerout"]) {
          const event = new dom.window.MouseEvent(type, { bubbles: true, relatedTarget: document.body });
          Object.defineProperty(event, "pointerType", { value: "mouse" });
          result.dispatchEvent(event);
        }
      });
      assert.equal(document.activeElement, input);
      await setSearch("missing model");
      assert.deepEqual(menuRows(), []);
      assert.match(document.querySelector('[role="status"]')?.textContent ?? "", /No matching models/);
      await setSearch("openai");
      assert.deepEqual(menuRows(), ["GPT-5.5", "GPT-5.4 mini"]);
      await setSearch("gpt-5.4-mini");
      assert.deepEqual(menuRows(), ["GPT-5.4 mini"]);
      await React.act(async () => {
        input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
      });
      const row = document.querySelector<HTMLElement>('[role="menuitem"]')!;
      assert.equal(document.activeElement, row);
      await React.act(async () => {
        row.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true }));
      });
      assert.equal(document.activeElement, input);
      await React.act(async () => {
        input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
        row.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
      });
      assert.equal(m.calls.model.at(-1), (models ?? WEB)[1]!.value);
      assert.ok(document.querySelector('[role="slider"]'));
      await click(byLabel("Choose a model: GPT-5.4 mini")!);
      assert.equal(document.querySelector<HTMLInputElement>('input')?.value, "");
      assert.equal(menuRows().length, 2);
    } finally {
      await m.unmount();
    }
  });
}

for (const local of [false, true]) {
  void test(`${local ? "explicit engine" : "web"} search includes the full available catalog and restores the shortlist`, async () => {
    const m = await mount(null, local ? WEB : null, local ? WEB_ALL : undefined);
    try {
      await click(byLabel("Choose a model: GPT-5.5")!);
      const input = document.querySelector<HTMLInputElement>('input[aria-label="Search models…"]')!;
      const setSearch = async (value: string) => {
        await React.act(async () => {
          Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!.call(input, value);
          input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
        });
      };
      assert.deepEqual(menuRows(), ["GPT-5.5", "GPT-5.4 mini"]);
      await setSearch("candidate");
      assert.equal(document.querySelectorAll('[role="menuitem"]').length, 1);
      assert.match(document.querySelector('[role="menuitem"]')!.textContent!, /Search candidate/);
      await setSearch("unavailable");
      assert.deepEqual(menuRows(), []);
      await setSearch("   ");
      assert.deepEqual(menuRows(), ["GPT-5.5", "GPT-5.4 mini"]);
      await setSearch("anthropic");
      const row = document.querySelector('[role="menuitem"]')!;
      await click(row);
      assert.equal(m.calls.model.at(-1), SEARCHABLE.value);
      assert.ok(byLabel("Choose a model: Search candidate"));
      await click(byLabel("Choose a model: Search candidate")!);
      assert.equal(document.querySelector<HTMLInputElement>('input')?.value, "");
      assert.equal(document.querySelectorAll('[role="menuitem"]').length, 3);
      const reopened = document.querySelector<HTMLInputElement>('input')!;
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!.call(reopened, "candidate");
        reopened.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      });
      assert.equal(document.querySelectorAll('[role="menuitem"]').length, 1);
    } finally {
      await m.unmount();
    }
  });
}

void test("the web catalog unfolds models above the slider and collapses after selection", async () => {
  const m = await mount("high", null);
  try {
    const slider = document.querySelector('[role="slider"]')!;
    assert.ok(slider);
    assert.equal(slider.getAttribute("aria-valuenow"), "3");
    assert.equal(byLabel("Fast mode"), null);
    await click(byLabel("Choose a model: GPT-5.5")!);
    const rows = [...document.querySelectorAll('[role="menuitem"]')];
    assert.deepEqual(menuRows(), ["GPT-5.5", "GPT-5.4 mini"]);
    assert.ok(rows[0]!.compareDocumentPosition(slider) & Node.DOCUMENT_POSITION_FOLLOWING);
    await click(rows[1]!);
    assert.deepEqual(m.calls.model, [WEB[1]!.value]);
    assert.ok(document.querySelector('[role="slider"]'));
    assert.deepEqual(menuRows(), []);
  } finally {
    await m.unmount();
  }
});

void test("an OpenRouter model uses its catalog effort levels on the slider", async () => {
  const m = await mount(null, [
    {
      ...WEB[0]!,
      value: "openrouter/anthropic/claude-sonnet-4.5",
      reasoning_efforts: ["low", "high"],
    },
  ]);
  try {
    const slider = document.querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuemax"), "2");
    await React.act(async () => {
      slider.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "End", bubbles: true }));
    });
    assert.deepEqual(m.calls.effort, ["high"]);
    assert.equal(byLabel("Fast mode"), null);
  } finally {
    await m.unmount();
  }
});

void test("every model moves from Default straight to its first reasoning tier", async () => {
  for (const [model, maximum, next] of [
    ["openrouter/anthropic/claude-haiku-5.5", "3", "low"],
    ["openrouter/openai/gpt-5.5", "3", "low"],
    ["openrouter/deepseek/deepseek-v4-pro", "3", "low"],
  ]) {
    const m = await mount(null, [
      {
        ...WEB[0]!,
        value: model!,
        reasoning_efforts: ["none", "low", "medium", "high"],
      },
    ]);
    try {
      const slider = document.querySelector('[role="slider"]')!;
      assert.equal(slider.getAttribute("aria-valuemax"), maximum);
      assert.equal(slider.getAttribute("aria-valuenow"), "0");
      await React.act(async () => {
        slider.dispatchEvent(
          new dom.window.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }),
        );
      });
      assert.deepEqual(m.calls.effort, [next]);
    } finally {
      await m.unmount();
    }
  }
});

void test("a saved None tier resets to Default and cannot remain on the chip", async () => {
  const m = await mount("none", [{ ...WEB[0]!, reasoning_efforts: ["none", "low", "high"] }]);
  try {
    assert.deepEqual(m.calls.effort, [null]);
    assert.doesNotMatch(m.trigger.textContent ?? "", /None/);
    const slider = document.querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuenow"), "0");
    assert.equal(slider.getAttribute("aria-valuemax"), "2");
  } finally {
    await m.unmount();
  }
});

void test("search has a placeholder, clear recovery, Escape recovery and direct Enter selection", async () => {
  const m = await mount();
  try {
    await click(byLabel("Choose a model: GPT-5.5")!);
    const input = document.querySelector<HTMLInputElement>('input[aria-label="Search models…"]')!;
    assert.equal(input.placeholder, "Search all models…");
    const setSearch = async (value: string) => {
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!.call(input, value);
        input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      });
    };
    await setSearch("missing");
    await click(byLabel("Clear search")!);
    assert.equal(input.value, "");
    assert.equal(document.activeElement, input);
    assert.equal(menuRows().length, 2);
    await setSearch("mini gpt");
    assert.deepEqual(menuRows(), ["GPT-5.4 mini"]);
    await React.act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    assert.equal(input.value, "");
    assert.equal(document.activeElement, input);
    assert.equal(menuRows().length, 2);
    await setSearch("mini");
    await React.act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, isComposing: true }));
    });
    assert.deepEqual(m.calls.model, []);
    await React.act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    });
    assert.equal(m.calls.model.at(-1), CLI[1]!.value);
    assert.ok(document.querySelector('[role="slider"]'));
    assert.deepEqual(menuRows(), []);
  } finally {
    await m.unmount();
  }
});

for (const [thinking, levels] of [[false, ["low", "high"]], [true, []], [true, ["none"]]] as const) {
  void test(`models without a slider are removed from defaults, shortlist and search (${thinking}/${levels})`, async () => {
    const unsupported = { ...WEB[0]!, supports_thinking: thinking, reasoning_efforts: [...levels] };
    const m = await mount("high", [unsupported, WEB[1]!], [unsupported, ...WEB.slice(1), SEARCHABLE]);
    try {
      assert.deepEqual(m.calls.model, [null]);
      assert.deepEqual(m.calls.effort, [null]);
      assert.match(m.trigger.textContent ?? "", /GPT-5.4 mini/);
      assert.ok(document.querySelector('[role="slider"]'));
      await click(byLabel("Choose a model: GPT-5.4 mini")!);
      assert.deepEqual(menuRows(), ["GPT-5.4 mini"]);
      const input = document.querySelector<HTMLInputElement>('input')!;
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!.call(input, "GPT-5.5");
        input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      });
      assert.deepEqual(menuRows(), []);
      assert.match(document.querySelector('[role="status"]')?.textContent ?? "", /No matching models/);
    } finally {
      await m.unmount();
    }
  });
}

void test("an engine without supported models cannot open an empty slider", async () => {
  const m = await mount(null, [{ ...CLI[0]!, reasoning_efforts: [] }]);
  try {
    assert.equal(m.trigger.disabled, true);
    assert.deepEqual(m.calls.model, [null]);
    assert.equal(document.querySelector('[role="menu"]'), null);
  } finally {
    await m.unmount();
  }
});

void test("model arrows navigate and Escape first folds the list then closes the panel", async () => {
  const m = await mount();
  try {
    await click(byLabel("Choose a model: GPT-5.5")!);
    const row = document.querySelector('[role="menuitem"]')!;
    const slider = document.querySelector('[role="slider"]')!;
    assert.ok(row.compareDocumentPosition(slider) & Node.DOCUMENT_POSITION_FOLLOWING);
    await React.act(async () => {
      (row as HTMLElement).focus();
      row.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
      );
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    assert.equal(document.activeElement?.textContent?.replace(/^OpenAI/, ""), "GPT-5.4 mini");
    await React.act(async () => {
      document.activeElement!.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    assert.ok(document.querySelector('[role="slider"]'));
    assert.deepEqual(menuRows(), []);
    await React.act(async () => {
      document
        .querySelector('[role="slider"]')!
        .dispatchEvent(
          new dom.window.KeyboardEvent("keydown", {
            key: "Escape",
            bubbles: true,
            cancelable: true,
          }),
        );
    });
    assert.equal(document.querySelector('[role="slider"]'), null);
    assert.deepEqual(m.calls.model, []);
  } finally {
    await m.unmount();
  }
});

void test("a model row picks that model", async () => {
  const m = await mount();
  try {
    await click(byLabel("Choose a model: GPT-5.5")!);
    const row = [...document.querySelectorAll('[role="menuitem"]')].find((el) =>
      el.textContent?.endsWith("GPT-5.4 mini"),
    )!;
    await click(row);
    assert.deepEqual(m.calls.model, [CLI[1]!.value]);
    assert.match(document.querySelector(".effort-charge")?.textContent ?? "", /GPT-5.4 mini/);
    // The model control names the new model while its slider remains available.
    assert.ok(document.querySelector('[role="slider"]'));
    assert.deepEqual(menuRows(), []);
    assert.equal(byLabel("Choose a model: GPT-5.4 mini")?.getAttribute("aria-expanded"), "false");
    await React.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    assert.equal(document.activeElement?.getAttribute("role"), "slider");
  } finally {
    await m.unmount();
  }
});

void test("the lightning button toggles fast mode", async () => {
  const m = await mount();
  try {
    const fast = byLabel("Fast mode")!;
    assert.equal(fast.getAttribute("aria-pressed"), "false");
    await click(fast);
    assert.deepEqual(m.calls.fast, [true]);
  } finally {
    await m.unmount();
  }
});

void test("a saved Max on a CLI model whose top is lower shows the level the backend runs", async () => {
  const m = await mount("max");
  try {
    const slider = document.querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuenow"), "3");
    assert.match(slider.getAttribute("aria-valuetext") ?? "", /^High/);
    assert.match(m.trigger.textContent ?? "", /High$/);
    assert.deepEqual(m.calls.effort, []);
  } finally {
    await m.unmount();
  }
  const off = await mount("none");
  try {
    assert.equal(document.querySelector('[role="slider"]')!.getAttribute("aria-valuenow"), "0");
  } finally {
    await off.unmount();
  }
});

void test("OpenRouter priority support enables the fast toggle", async () => {
  const m = await mount(null, [{ ...WEB[0]!, value: "openrouter/anthropic/claude-opus-5.5" }]);
  try {
    const fast = byLabel("Fast mode");
    assert.ok(fast);
    assert.ok(fast.querySelector("svg")?.classList.contains("size-4"));
    await click(fast);
    assert.deepEqual(m.calls.fast, [true]);
  } finally {
    await m.unmount();
  }
});

void test("the right-side reset returns effort to the model default", async () => {
  const m = await mount("high");
  try {
    const reset = byLabel("Reset to the model default");
    assert.ok(reset);
    assert.equal(reset.getAttribute("aria-disabled"), "false");
    assert.ok(byLabel("Fast mode"));
    await click(reset);
    assert.deepEqual(m.calls.effort, [null]);
  } finally {
    await m.unmount();
  }
});

void test("Enter from model search keeps the shared slider", async () => {
  const m = await mount();
  try {
    await click(byLabel("Choose a model: GPT-5.5")!);
    const input = document.querySelector<HTMLInputElement>('input')!;
    await React.act(async () => {
      input.focus();
      Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!.call(input, "mini");
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    });
    await React.act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    });
    assert.deepEqual(m.calls.model, [CLI[1]!.value]);
    assert.ok(document.querySelector('[role="slider"]'));
    assert.deepEqual(menuRows(), []);
  } finally {
    await m.unmount();
  }
});
