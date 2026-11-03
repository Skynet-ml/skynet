/*
 * node --test cannot load .tsx, so this runs through jiti (account-menu.render.test.ts
 * spawns it with aliases that swap in __fixtures__/linked-account-mock.ts and
 * __fixtures__/popover-inline.tsx).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SessionProvider } from "next-auth/react";

import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setServerMessages } from "@/shared/lib/runtime-messages";
import type { LinkedAccount } from "@/shared/hooks/use-linked-account";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { mockAccount } = (await import("@/shared/hooks/use-linked-account")) as unknown as {
  mockAccount: { current: LinkedAccount | undefined };
};
const { lastContentRef } = (await import("@/shared/ui/primitives/popover")) as unknown as {
  lastContentRef: { current: React.ReactNode };
};
const { SettingsModalProvider } = await import("@/features/settings");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { AccountMenu } = await import("./account-menu");

const session = {
  user: { name: "local@skynet.local", email: "local@skynet.local" },
  expires: "2099-01-01T00:00:00.000Z",
};

function render(account: LinkedAccount | undefined): string {
  mockAccount.current = account;
  setServerMessages(buildActiveCatalog("en"), "en");
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="en">
      <SessionProvider session={session}>
        <SettingsModalProvider>
          <AccountMenu />
        </SettingsModalProvider>
      </SessionProvider>
    </LocaleProvider>,
  );
}

function text(node: React.ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  if (React.isValidElement<{ children?: React.ReactNode }>(node)) return text(node.props.children);
  return "";
}

/** Find the menu item whose text is `label` in the last rendered popover content. */
function menuItem(label: string): { onClick: () => void } {
  const queue: React.ReactNode[] = [lastContentRef.current];
  while (queue.length) {
    const node = queue.shift();
    if (Array.isArray(node)) queue.push(...(node as React.ReactNode[]));
    else if (React.isValidElement<{ children?: React.ReactNode; onClick?: () => void }>(node)) {
      if (node.type === "button" && node.props.onClick && text(node) === label)
        return { onClick: node.props.onClick };
      queue.push(node.props.children);
    }
  }
  throw new Error(`no menu item "${label}"`);
}

const ada: LinkedAccount = { linked: true, name: "Ada", email: "ada@example.com", offline: false };

void it("shows Downloads on the web but hides it in the desktop app", () => {
  const previousMode = process.env.APP_LOCAL_MODE;
  try {
    process.env.APP_LOCAL_MODE = "0";
    assert.match(render(ada), /href="\/download"/);
    process.env.APP_LOCAL_MODE = "1";
    assert.doesNotMatch(render(ada), /href="\/download"/);
  } finally {
    if (previousMode === undefined) delete process.env.APP_LOCAL_MODE;
    else process.env.APP_LOCAL_MODE = previousMode;
  }
});

void describe("AccountMenu link states in local mode", () => {
  beforeEach(() => {
    process.env.APP_LOCAL_MODE = "1";
  });
  afterEach(() => {
    delete process.env.APP_LOCAL_MODE;
  });

  void it("shows the linked account's name and email, with one log out", () => {
    const html = render(ada);
    assert.match(html, /Ada/);
    assert.match(html, /ada@example\.com/);
    assert.equal(html.match(/Log out/g)?.length, 1);
    assert.doesNotMatch(html, /Sign out of sync/);
    assert.doesNotMatch(html, /Sign in to sync|Not signed in|skynet\.local/);
  });

  void it("shows the linked account's profile picture", () => {
    const html = render({ ...ada, image: "https://lh3.googleusercontent.com/a/ada" });
    // The trigger and the menu header each show the avatar.
    assert.equal(
      html.match(/<img[^>]+src="https:\/\/lh3\.googleusercontent\.com\/a\/ada"/g)?.length,
      2,
    );
  });

  void it("shows the initials while the linked account has no picture", () => {
    assert.doesNotMatch(render(ada), /<img/);
  });

  void it("falls back to the account label with no name or email", () => {
    const html = render({ linked: true, name: null, email: null, offline: false });
    assert.match(html, /Skynet account/);
    assert.doesNotMatch(html, /skynet\.local/);
  });

  void it("marks the linked account offline", () => {
    const html = render({ ...ada, offline: true });
    assert.match(html, /Ada/);
    assert.match(html, /Linked \(offline\)/);
    assert.doesNotMatch(html, /Not signed in/);
  });

  void it("shows not signed in and sign in to sync when not linked", () => {
    const html = render({ linked: false });
    assert.match(html, /Local mode/);
    assert.match(html, /Not signed in/);
    assert.match(html, /Sign in to sync/);
    assert.doesNotMatch(html, /Log out|skynet\.local/);
  });

  void it("shows a neutral local mode while the link is unknown", () => {
    const html = render(undefined);
    assert.match(html, /Local mode/);
    assert.doesNotMatch(html, /Not signed in|Sign in to sync|Log out|skynet\.local/);
  });
});

void describe("AccountMenu sign in to sync", () => {
  const realFetch = globalThis.fetch;
  let opened: string[] = [];
  let requests: string[] = [];

  beforeEach(() => {
    process.env.APP_LOCAL_MODE = "1";
    opened = [];
    requests = [];
  });
  afterEach(() => {
    delete process.env.APP_LOCAL_MODE;
    delete (globalThis as { window?: unknown }).window;
    globalThis.fetch = realFetch;
  });

  /** Render the not-linked menu, then click "Sign in to sync" with `status` from start. */
  async function clickSignIn(status: number): Promise<void> {
    render({ linked: false });
    const item = menuItem("Sign in to sync");
    // Set after the render: runtime-env reads the server env while `window` is unset.
    (globalThis as { window?: unknown }).window = {
      __APP_ENV__: { apiUrl: "http://api.test", appVersion: "0", localMode: true },
      open: (url: string) => opened.push(url),
    };
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      requests.push(`${init?.method ?? "GET"} ${url}`);
      return new Response(JSON.stringify({ authorize_url: "https://example.com/consent" }), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    item.onClick();
    await new Promise((resolve) => setTimeout(resolve, 20));
  }

  void it("starts the link, then opens the consent page", async () => {
    await clickSignIn(200);
    assert.deepEqual(requests, ["POST http://api.test/local/hosted-link/start"]);
    assert.deepEqual(opened, ["https://example.com/consent"]);
  });

  void it("opens nothing when the start fails", async () => {
    await clickSignIn(400);
    assert.equal(requests.length, 1);
    assert.deepEqual(opened, []);
  });
});

void describe("AccountMenu log out in local mode", () => {
  const realFetch = globalThis.fetch;
  let requests: string[] = [];

  beforeEach(() => {
    process.env.APP_LOCAL_MODE = "1";
    requests = [];
  });
  afterEach(() => {
    delete process.env.APP_LOCAL_MODE;
    delete (globalThis as { window?: unknown }).window;
    globalThis.fetch = realFetch;
  });

  void it("unlinks the hosted account and keeps the local session", async () => {
    render(ada);
    const item = menuItem("Log out");
    (globalThis as { window?: unknown }).window = {
      __APP_ENV__: { apiUrl: "http://api.test", appVersion: "0", localMode: true },
      localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    };
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      requests.push(`${init?.method ?? "GET"} ${url}`);
      return new Response(JSON.stringify({ hosted_revoked: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    item.onClick();
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.deepEqual(requests, ["POST http://api.test/local/hosted-link/unlink"]);
  });
});
