/*
 * node --test cannot load .tsx, so RepoTargetCard.render.test.ts runs this
 * file through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

import type { RepoDraft } from "../../lib/wizard-v2";

// The repository picker's skeleton imports a stylesheet, which Node cannot load.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.endsWith(".css")) return { shortCircuit: true, url: "data:text/javascript," };
    return nextResolve(specifier, context);
  },
});

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
  // The commit select renders its closed list into a fragment.
  DocumentFragment: dom.window.DocumentFragment,
  Event: dom.window.Event,
  MouseEvent: dom.window.MouseEvent,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  sessionStorage: dom.window.sessionStorage,
  localStorage: dom.window.localStorage,
  customElements: dom.window.customElements,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
// The provider icons read a media query on import; jsdom has none.
const matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
Object.assign(globalThis, { matchMedia });
Object.assign(dom.window, { matchMedia });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

// GitHub is linked, but its branch and commit lists fail, so the branch is a typed field.
globalThis.fetch = (async (input: RequestInfo | URL) =>
  String(input).endsWith("/connectors")
    ? new Response(
        JSON.stringify({ connectors: [{ provider: "github", connected: true, status: "ok" }] }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      )
    : new Response("{}", { status: 500 })) as typeof fetch;

const { createRoot } = await import("react-dom/client");
const { RepoTargetCard } = await import("./RepoTargetCard");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");

after(() => dom.window.close());

const repo: RepoDraft = {
  provider: "github",
  repository: "owner/repo",
  branch: "",
  commit: "",
  editablePaths: [],
  setupCommand: "",
};

/** Type into an input as a user does. */
function type(input: HTMLInputElement, text: string) {
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  setter.call(input, text);
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

/** Leave an input as a user does. */
function leave(input: HTMLInputElement) {
  input.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
}

/** Render the card for this draft and wait for the connectors to load. */
async function mount(
  draft: RepoDraft,
  onChange: (next: RepoDraft) => void = () => {},
  objective = "",
  localFolders = false,
  issue: string | null = null,
) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      // The app mounts this provider once at the root; the secrets header needs it.
      <TooltipProvider>
        <RepoTargetCard
          repo={draft}
          onChange={onChange}
          issue={issue}
          guided={false}
          seed={{ objective, scorerCode: "", cases: [] }}
          localFolders={localFolders}
        />
      </TooltipProvider>,
    );
  });
  const unmount = async () => {
    await React.act(async () => root.unmount());
    container.remove();
  };
  return { container, unmount };
}

void test("a typed branch reaches the draft only on blur, and only when it changed", async () => {
  const sent: RepoDraft[] = [];
  const { container, unmount } = await mount(repo, (next) => sent.push(next));
  try {
    const input = container.querySelector<HTMLInputElement>("#v2-repo-branch");
    assert.ok(input, "no field #v2-repo-branch");
    // Each patch of another branch drops the draft files.
    await React.act(async () => type(input, "d"));
    await React.act(async () => type(input, "de"));
    assert.deepEqual(sent, []);
    await React.act(async () => type(input, ""));
    await React.act(async () => leave(input));
    assert.deepEqual(sent, []);
    await React.act(async () => type(input, "dev"));
    await React.act(async () => leave(input));
    assert.deepEqual(sent, [{ ...repo, branch: "dev" }]);
    assert.ok(container.querySelector("#v2-repo-commit"), "no field #v2-repo-commit");
    assert.ok(container.querySelector('[aria-controls="v2-repo-secrets-panel"]'));
  } finally {
    await unmount();
  }
});

void test("branch and commit wait until a repository is picked", async () => {
  const { container, unmount } = await mount({ ...repo, repository: "" });
  try {
    assert.equal(container.querySelector("#v2-repo-branch"), null);
    assert.equal(container.querySelector("#v2-repo-commit"), null);
  } finally {
    await unmount();
  }
});

void test("an empty draft picks the source, then the host, with nothing preselected", async () => {
  const empty: RepoDraft = { ...repo, provider: undefined, repository: "" };
  const sent: RepoDraft[] = [];
  const { container, unmount } = await mount(empty, (next) => sent.push(next));
  try {
    assert.equal(container.querySelector('[role="radio"]'), null);
    const cards = [...container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]")];
    assert.equal(cards.length, 2);
    await React.act(async () => cards[0]!.click());
    // Both source cards stay after the pick, so the user can switch back.
    assert.equal(container.querySelectorAll("button[aria-pressed]").length, 2);
    assert.equal(cards[0]!.getAttribute("aria-pressed"), "true");
    const hosts = [...container.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
    assert.equal(hosts.length, 3);
    assert.ok(hosts.every((h) => h.getAttribute("aria-checked") === "false"));
    // GitHub is linked, so the pick sets the host and starts no sign-in.
    await React.act(async () => hosts[0]!.click());
    assert.deepEqual(sent, [
      { ...empty, provider: "github", branch: "", commit: "", createdBySkynet: false },
    ]);
    assert.equal(hosts[0]!.getAttribute("aria-checked"), "true");
  } finally {
    await unmount();
  }
});

void test("Create new offers every host, starts no sign-in, and signs in to an unlinked pick", async () => {
  const linkedFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return String(input).endsWith("/connectors")
      ? new Response(JSON.stringify({ connectors: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      : new Response("{}", { status: 500 });
  }) as typeof fetch;
  const empty: RepoDraft = { ...repo, provider: undefined, repository: "" };
  const { container, unmount } = await mount(empty);
  try {
    const cards = [...container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]")];
    await React.act(async () => cards[1]!.click());
    assert.equal(cards[1]!.getAttribute("aria-pressed"), "true");
    assert.deepEqual(
      calls.filter((url) => !url.endsWith("/connectors")),
      [],
    );
    const hosts = [...container.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
    assert.equal(hosts.length, 3);
    assert.ok(hosts.every((h) => h.getAttribute("aria-checked") === "false"));
    assert.deepEqual(
      hosts.map((h) => h.disabled),
      [false, false, false],
    );
    assert.equal(container.querySelector("#v2-repo-new-name"), null);
    // GitLab is not linked, so picking it starts its sign-in.
    await React.act(async () => hosts[1]!.click());
    assert.ok(calls.some((url) => url.endsWith("/connectors/gitlab/oauth/start")));
  } finally {
    await unmount();
    globalThis.fetch = linkedFetch;
  }
});

void test("Create new reopens on the host the user signed in to", async () => {
  const linkedFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) =>
    new Response(
      JSON.stringify(
        String(input).endsWith("/connectors")
          ? { connectors: [] }
          : { authorize_url: "about:blank" },
      ),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )) as typeof fetch;
  const empty: RepoDraft = { ...repo, provider: undefined, repository: "" };
  try {
    const first = await mount(empty);
    try {
      const cards = [
        ...first.container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]"),
      ];
      await React.act(async () => cards[1]!.click());
      const hosts = [...first.container.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
      await React.act(async () => hosts[1]!.click());
    } finally {
      await first.unmount();
    }
    const back = await mount(empty);
    try {
      assert.deepEqual(
        [...back.container.querySelectorAll("button[aria-pressed]")].map((c) =>
          c.getAttribute("aria-pressed"),
        ),
        ["false", "true"],
      );
      const hosts = [...back.container.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
      assert.deepEqual(
        hosts.map((h) => h.getAttribute("aria-checked")),
        ["false", "true", "false"],
      );
      assert.equal(dom.window.sessionStorage.getItem("skynet.submit.repo-new-host.v1"), null);
    } finally {
      await back.unmount();
    }
  } finally {
    globalThis.fetch = linkedFetch;
    dom.window.sessionStorage.clear();
  }
});

for (const host of ["gitlab", "bitbucket"] as const) {
  void test(`a linked ${host} account creates the new repository on ${host}`, async () => {
    const linkedFetch = globalThis.fetch;
    const posts: Array<{ url: string; body: unknown }> = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/connectors"))
        return new Response(
          JSON.stringify({ connectors: [{ provider: host, connected: true, status: "ok" }] }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      if (init?.method === "POST") {
        posts.push({ url, body: JSON.parse(String(init.body)) });
        return new Response(
          JSON.stringify({
            full_name: "alice/app",
            default_branch: "main",
            commit: "a".repeat(40),
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { status: 500 });
    }) as typeof fetch;
    const empty: RepoDraft = { ...repo, provider: undefined, repository: "" };
    const sent: RepoDraft[] = [];
    const { container, unmount } = await mount(empty, (next) => sent.push(next), "Sort fast.");
    try {
      const cards = [...container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]")];
      await React.act(async () => cards[1]!.click());
      const hosts = [...container.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
      await React.act(async () => hosts[host === "gitlab" ? 1 : 2]!.click());
      const name = container.querySelector<HTMLInputElement>("#v2-repo-new-name");
      assert.ok(name, "no field #v2-repo-new-name");
      await React.act(async () => type(name, "app"));
      const create = [...container.querySelectorAll<HTMLButtonElement>("button")].find((b) =>
        b.textContent?.includes("Create repository"),
      );
      assert.ok(create && !create.disabled, "the Create button is not enabled");
      await React.act(async () => create.click());
      assert.equal(posts.length, 1);
      assert.ok(posts[0]!.url.endsWith(`/connectors/${host}/repos`), posts[0]!.url);
      assert.deepEqual(posts[0]!.body, {
        name: "app",
        private: true,
        objective: "Sort fast.",
        scorer_code: null,
        cases: null,
      });
      // The run pushes candidate branches only to GitHub, so this is not marked as created by Skynet.
      assert.deepEqual(sent.at(-1), {
        ...empty,
        provider: host,
        repository: "alice/app",
        branch: "main",
        commit: "",
        createdBySkynet: false,
      });
    } finally {
      await unmount();
      globalThis.fetch = linkedFetch;
    }
  });
}

void test("after a pick both source cards stay and there is no back button", async () => {
  const empty: RepoDraft = { ...repo, provider: undefined, repository: "" };
  const { container, unmount } = await mount(empty);
  try {
    const cards = () => [...container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]")];
    await React.act(async () => cards()[1]!.click());
    assert.equal(cards().length, 2);
    assert.deepEqual(
      cards().map((c) => c.getAttribute("aria-pressed")),
      ["false", "true"],
    );
    assert.ok(container.querySelector('[role="radio"]'), "no host picker");
    assert.ok(
      ![...container.querySelectorAll("button")].some((b) => b.textContent === "Back"),
      "a back button is still shown",
    );
    await React.act(async () => cards()[0]!.click());
    assert.deepEqual(
      cards().map((c) => c.getAttribute("aria-pressed")),
      ["true", "false"],
    );
  } finally {
    await unmount();
  }
});

// SKY-993: the "Pick a folder" source in the desktop app.
type Bridge = {
  pickFolder?: () => Promise<string | null>;
  createFolder?: (name: string) => Promise<string | null>;
};
const win = dom.window as unknown as { __APP_ENV__?: unknown; skynetDesktop?: Bridge };

/** Run the body as the desktop app with this folder bridge, then restore the browser. */
async function asDesktop(bridge: Bridge | undefined, localMode: boolean, body: () => Promise<void>) {
  win.__APP_ENV__ = localMode ? { localMode: true } : undefined;
  win.skynetDesktop = bridge;
  try {
    await body();
  } finally {
    delete win.__APP_ENV__;
    delete win.skynetDesktop;
  }
}

/** The card with its draft kept in state, as the wizard keeps it. */
function Controlled({ start, sent }: { start: RepoDraft; sent: RepoDraft[] }) {
  const [draft, setDraft] = React.useState(start);
  return (
    <TooltipProvider>
      <RepoTargetCard
        repo={draft}
        onChange={(next) => {
          sent.push(next);
          setDraft(next);
        }}
        issue={null}
        guided={false}
        seed={{ objective: "", scorerCode: "", cases: [] }}
        localFolders
      />
    </TooltipProvider>
  );
}

async function mountControlled(start: RepoDraft, sent: RepoDraft[]) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(<Controlled start={start} sent={sent} />));
  return {
    container,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

const sourceCards = (container: HTMLElement) => [
  ...container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]"),
];
const buttonWithText = (container: HTMLElement, text: string) =>
  [...container.querySelectorAll<HTMLButtonElement>("button")].find((b) =>
    b.textContent?.includes(text),
  );

void test("the folder source shows only in the desktop app, for a run on this computer", async () => {
  const bridge: Bridge = { pickFolder: async () => null };
  for (const [hasBridge, localMode, localFolders, expected] of [
    [false, false, true, 2],
    [true, false, true, 2],
    [false, true, true, 2],
    [true, true, false, 2],
    [true, true, true, 3],
  ] as const) {
    await asDesktop(hasBridge ? bridge : undefined, localMode, async () => {
      const { container, unmount } = await mount(repo, () => {}, "", localFolders);
      try {
        assert.equal(
          sourceCards(container).length,
          expected,
          `bridge=${hasBridge} localMode=${localMode} localFolders=${localFolders}`,
        );
      } finally {
        await unmount();
      }
    });
  }
});

void test("Choose folder sets the picked path, drops the git fields and shows the path", async () => {
  const picks: string[] = [];
  const calls: string[] = [];
  const linkedFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return linkedFetch(input);
  }) as typeof fetch;
  const bridge: Bridge = {
    pickFolder: async () => {
      picks.push("pick");
      return "/Users/me/app";
    },
  };
  await asDesktop(bridge, true, async () => {
    const sent: RepoDraft[] = [];
    const { container, unmount } = await mountControlled(
      { ...repo, branch: "dev", commit: "a".repeat(40), createdBySkynet: true },
      sent,
    );
    try {
      await React.act(async () => sourceCards(container)[2]!.click());
      const choose = container.querySelector<HTMLButtonElement>(
        'button[aria-describedby="v2-repo-folder-path"]',
      );
      assert.ok(choose, "no Choose folder button");
      calls.length = 0;
      await React.act(async () => choose.click());
      assert.deepEqual(picks, ["pick"]);
      const last = sent.at(-1)!;
      assert.equal(last.localPath, "/Users/me/app");
      assert.equal("provider" in last, false);
      assert.equal(last.repository, "");
      assert.equal(last.branch, "");
      assert.equal(last.commit, "");
      assert.equal(last.createdBySkynet, false);
      const path = container.querySelector("#v2-repo-folder-path");
      assert.equal(path?.getAttribute("aria-live"), "polite");
      assert.ok(path?.textContent?.includes("/Users/me/app"));
      // A folder source reads no branch, commit or file list from a repository host.
      // It reads only the names in the folder's .env (SKY-994).
      assert.deepEqual(
        calls.filter((url) => !url.endsWith("/connectors") && !url.includes("/secrets/local-env?")),
        [],
      );
    } finally {
      await unmount();
      globalThis.fetch = linkedFetch;
    }
  });
});

void test("New folder calls createFolder with the typed name and uses its path", async () => {
  const names: string[] = [];
  const bridge: Bridge = {
    pickFolder: async () => null,
    createFolder: async (name) => {
      names.push(name);
      return `/Users/me/Documents/Skynet/${name}`;
    },
  };
  await asDesktop(bridge, true, async () => {
    const sent: RepoDraft[] = [];
    const { container, unmount } = await mountControlled({ ...repo, repository: "" }, sent);
    try {
      await React.act(async () => sourceCards(container)[2]!.click());
      const input = container.querySelector<HTMLInputElement>("#v2-repo-folder-name");
      assert.ok(input, "no field #v2-repo-folder-name");
      await React.act(async () => type(input, " my-app "));
      await React.act(async () => buttonWithText(container, "Create folder")!.click());
      assert.deepEqual(names, ["my-app"]);
      assert.equal(sent.at(-1)!.localPath, "/Users/me/Documents/Skynet/my-app");
      assert.equal(input.value, "");
    } finally {
      await unmount();
    }
  });
});

void test("a cancelled folder pick changes nothing", async () => {
  const bridge: Bridge = { pickFolder: async () => null, createFolder: async () => null };
  await asDesktop(bridge, true, async () => {
    const sent: RepoDraft[] = [];
    const { container, unmount } = await mountControlled({ ...repo, localPath: "/Users/me/old" }, sent);
    try {
      // A folder draft opens on the folder source.
      assert.equal(sourceCards(container)[2]!.getAttribute("aria-pressed"), "true");
      const choose = container.querySelector<HTMLButtonElement>(
        'button[aria-describedby="v2-repo-folder-path"]',
      )!;
      await React.act(async () => choose.click());
      assert.deepEqual(sent, []);
      assert.ok(container.querySelector("#v2-repo-folder-path")?.textContent?.includes("/Users/me/old"));
    } finally {
      await unmount();
    }
  });
});

void test("a look at the folder card keeps the git repository and its branch", async () => {
  const bridge: Bridge = { pickFolder: async () => null };
  await asDesktop(bridge, true, async () => {
    const sent: RepoDraft[] = [];
    const { container, unmount } = await mountControlled({ ...repo, branch: "dev" }, sent);
    try {
      await React.act(async () => sourceCards(container)[2]!.click());
      assert.equal(sourceCards(container)[2]!.getAttribute("aria-pressed"), "true");
      assert.ok(container.querySelector("#v2-repo-folder-path"), "no folder panel");
      await React.act(async () => sourceCards(container)[0]!.click());
      assert.equal(sourceCards(container)[0]!.getAttribute("aria-pressed"), "true");
      assert.deepEqual(sent, []);
    } finally {
      await unmount();
    }
  });
});

void test("a folder draft without the folder card asks for a repository instead", async () => {
  const { container, unmount } = await mount(
    { ...repo, localPath: "" },
    () => {},
    "",
    false,
    "submit.v2.repo.issue.local_path",
  );
  try {
    assert.equal(sourceCards(container).length, 2);
    const status = container.querySelector('[role="status"]');
    assert.ok(status?.textContent?.includes("Choose a repository"), status?.textContent ?? "no status");
  } finally {
    await unmount();
  }
});

void test("a new folder hides the last folder's .env preview while its read waits or fails (SKY-994)", async () => {
  for (const second of ["pending", "rejected"] as const) {
    const bridge: Bridge = { pickFolder: async () => "/Users/me/b" };
    const linkedFetch = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (!url.includes("/secrets/local-env?")) return linkedFetch(input);
      const path = new URL(url, "https://api.example.test").searchParams.get("path");
      if (path === "/Users/me/b") {
        if (second === "rejected") throw new TypeError("network down");
        return new Promise<Response>(() => {});
      }
      return new Response(
        JSON.stringify({
          status: "found",
          entries: [{ name: "NEW_KEY", clash: false }],
          skipped: [{ line: 4, name: "MY-KEY", reason: "invalid_name" }],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }) as typeof fetch;
    await asDesktop(bridge, true, async () => {
      const sent: RepoDraft[] = [];
      const { container, unmount } = await mountControlled({ ...repo, localPath: "/Users/me/a" }, sent);
      try {
        await React.act(async () => {
          await new Promise((resolve) => setTimeout(resolve, 0));
        });
        assert.ok((container.textContent ?? "").includes("Skipped .env lines"), "no preview for folder A");
        const choose = container.querySelector<HTMLButtonElement>('button[aria-describedby="v2-repo-folder-path"]');
        assert.ok(choose, "no Choose folder button");
        await React.act(async () => {
          choose.click();
          await new Promise((resolve) => setTimeout(resolve, 0));
        });
        assert.equal(sent.at(-1)!.localPath, "/Users/me/b");
        assert.ok(!(container.textContent ?? "").includes("Skipped .env lines"), `${second}: folder A preview stays`);
      } finally {
        await unmount();
        globalThis.fetch = linkedFetch;
      }
    });
  }
});

void test("the folder card keeps a run-start .env clash pick when the preview finds no file (SKY-994)", async () => {
  const bridge: Bridge = { pickFolder: async () => null };
  const linkedFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (!url.includes("/secrets/local-env?")) return linkedFetch(input);
    return new Response(JSON.stringify({ status: "missing", entries: [], skipped: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  await asDesktop(bridge, true, async () => {
    const sent: RepoDraft[] = [];
    const { container, unmount } = await mountControlled(
      { ...repo, localPath: "/Users/me/app", envClashes: ["DB_URL"] },
      sent,
    );
    try {
      await React.act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      assert.ok(!sent.some((draft) => (draft.envClashes ?? []).length === 0), "the preview dropped the clash");
      const text = container.textContent ?? "";
      assert.ok(text.includes("DB_URL"));
      assert.ok(buttonWithText(container, "Keep saved"), "no Keep saved choice");
      const use = buttonWithText(container, "Use .env");
      assert.ok(use, "no Use .env choice");
      assert.ok(text.includes("Pick Keep saved or Use .env for: DB_URL."), "no undecided notice");
      await React.act(async () => use.click());
      assert.deepEqual(sent.at(-1)!.envChoices, { DB_URL: "use" });
    } finally {
      await unmount();
      globalThis.fetch = linkedFetch;
    }
  });
});

void test("the folder card asks to pick a folder again when the app did not pick it (SKY-1007)", async () => {
  const bridge: Bridge = { pickFolder: async () => null };
  const linkedFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (!url.includes("/secrets/local-env?")) return linkedFetch(input);
    return new Response(JSON.stringify({ status: "not_picked", entries: [], skipped: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  await asDesktop(bridge, true, async () => {
    const { container, unmount } = await mountControlled({ ...repo, localPath: "/Users/me/app" }, []);
    try {
      await React.act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      assert.ok((container.textContent ?? "").includes("Pick the folder again."), "no pick-again notice");
    } finally {
      await unmount();
      globalThis.fetch = linkedFetch;
    }
  });
});

void test("the folder card lists .env clashes and a pick goes into the draft (SKY-994)", async () => {
  const bridge: Bridge = { pickFolder: async () => null };
  const linkedFetch = globalThis.fetch;
  const previews: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (!url.includes("/secrets/local-env?")) return linkedFetch(input);
    previews.push(new URL(url, "https://api.example.test").searchParams.get("path") ?? "");
    return new Response(
      JSON.stringify({
        status: "found",
        entries: [
          { name: "DB_URL", clash: true },
          { name: "NEW_KEY", clash: false },
        ],
        skipped: [{ line: 4, name: "MY-KEY", reason: "invalid_name" }],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }) as typeof fetch;
  await asDesktop(bridge, true, async () => {
    const sent: RepoDraft[] = [];
    const { container, unmount } = await mountControlled({ ...repo, localPath: "/Users/me/app" }, sent);
    try {
      await React.act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      assert.deepEqual(previews, ["/Users/me/app"]);
      assert.deepEqual(sent.at(-1)!.envClashes, ["DB_URL"]);
      assert.deepEqual(sent.at(-1)!.envChoices, {});
      const text = container.textContent ?? "";
      assert.ok(text.includes("DB_URL"));
      assert.ok(text.includes("4 (MY-KEY)"));
      const use = buttonWithText(container, "Use .env");
      assert.ok(use, "no Use .env choice");
      assert.ok(buttonWithText(container, "Keep saved"), "no Keep saved choice");
      assert.ok(text.includes("Pick Keep saved or Use .env for: DB_URL."), "no undecided notice");
      await React.act(async () => use.click());
      assert.deepEqual(sent.at(-1)!.envChoices, { DB_URL: "use" });
      assert.ok(!(container.textContent ?? "").includes("Pick Keep saved or Use .env for:"));
      // The same preview again changes nothing, so the draft does not loop.
      const count = sent.length;
      await React.act(async () => {
        dom.window.dispatchEvent(new dom.window.Event("saved-secrets-changed"));
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      assert.equal(previews.length, 2);
      assert.equal(sent.length, count);
    } finally {
      await unmount();
      globalThis.fetch = linkedFetch;
    }
  });
});
