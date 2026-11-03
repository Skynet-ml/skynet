import assert from "node:assert/strict";
import { Module } from "node:module";
import path from "node:path";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createJiti } from "jiti";

(globalThis as { React?: typeof React }).React = React;
const jiti = createJiti(import.meta.url, {
  jsx: true,
  alias: { "@/": `${path.join(process.cwd(), "src")}/` },
});

function stub(id: string, exports: Record<string, unknown>) {
  const resolved = jiti.resolve(id);
  const module = new Module(resolved);
  module.exports = exports;
  module.loaded = true;
  jiti.cache[resolved] = module;
}

stub("next/navigation", { useRouter: () => ({ push: () => {} }) });
stub("next-auth/react", { useSession: () => ({ data: null }) });
stub("@/features/agent-panel", { useGeneralistPanelStateOptional: () => null });
stub("@/features/settings", {
  SETTINGS_TABS: {},
  useSettingsModal: () => ({ open: false, openTo: () => {} }),
  visibleSettingsTabs: () => [],
});
stub("@/shared/providers", { useLocale: () => ({ locale: "en" }) });
stub("@/shared/hooks/use-linked-account", {
  useLinkedAccount: () => undefined,
  unlinkHostedAccount: () => Promise.resolve(),
});
stub("@/shared/layout/account-menu", { startHostedSignIn: () => {} });

// Render the palette contents without the dialog's client-only portal.
const inline = ({ children }: React.PropsWithChildren) =>
  React.createElement(React.Fragment, null, children);
stub("@/shared/ui/primitives/dialog", {
  Dialog: inline,
  DialogContent: inline,
  DialogDescription: inline,
  DialogHeader: inline,
  DialogTitle: inline,
});

const { GlobalSearch } = await jiti.import<{ GlobalSearch: React.ComponentType }>(
  "./global-search",
);
const { APP_PAGES } = await jiti.import<{ APP_PAGES: { id: string }[] }>("@/shared/lib/app-pages");

void test("Downloads stays searchable on the web but not in the desktop app", () => {
  const previousMode = process.env.APP_LOCAL_MODE;
  try {
    for (const local of [false, true, false]) {
      process.env.APP_LOCAL_MODE = local ? "1" : "0";
      const html = renderToStaticMarkup(React.createElement(GlobalSearch));
      for (const page of APP_PAGES) {
        assert.equal(
          html.includes(`id="global-search-page-${page.id}"`),
          !(local && page.id === "download"),
          `${local}: ${page.id}`,
        );
      }
    }
  } finally {
    if (previousMode === undefined) delete process.env.APP_LOCAL_MODE;
    else process.env.APP_LOCAL_MODE = previousMode;
  }
});
