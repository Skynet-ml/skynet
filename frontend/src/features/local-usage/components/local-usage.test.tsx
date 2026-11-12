/*
 * node --test cannot load .tsx, so this runs through jiti (local-usage.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import type { CliUsageReport, CliUsageTotals } from "../lib/cli-usage";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { LocalCliUsageView } = await import("./LocalCliUsagePanel");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");

setClientLocale("en");

const zero: CliUsageTotals = { calls: 0, failed_calls: 0, runs: 0, input_tokens: 0, output_tokens: 0 };

const report: CliUsageReport = {
  generated_at: "2026-11-06T12:00:00+00:00",
  windows: ["5h", "7d"],
  clis: [
    {
      cli: "codex",
      last_used_at: null,
      cap: null,
      windows: { "5h": zero, "7d": zero },
      quota: { status: "unavailable", windows: [] },
    },
    {
      cli: "claude",
      last_used_at: "2026-11-06T11:00:00+00:00",
      cap: { reset_at: "2026-11-06T15:00:00+00:00", message: "limit" },
      windows: {
        "5h": { calls: 3, failed_calls: 1, runs: 2, input_tokens: 1234, output_tokens: 23 },
        "7d": { calls: 4, failed_calls: 1, runs: 2, input_tokens: 51234, output_tokens: 223 },
      },
      quota: {
        status: "ok",
        windows: [
          { window: "7d", used_percent: 22.4, resets_at: "2026-11-12T15:00:00+00:00" },
          { window: "5h", used_percent: 100, resets_at: null },
        ],
      },
    },
  ],
};

const withQuota = (cli: "claude" | "codex", quota: CliUsageReport["clis"][number]["quota"]): CliUsageReport => ({
  ...report,
  clis: report.clis.map((entry) => (entry.cli === cli ? { ...entry, quota } : entry)),
});

const render = (props: Partial<React.ComponentProps<typeof LocalCliUsageView>>) =>
  renderToStaticMarkup(
    <TooltipProvider>
      <LocalCliUsageView report={null} loading={false} failed={false} {...props} />
    </TooltipProvider>,
  );

void describe("LocalCliUsageView", () => {
  void it("shows only a percent bar for each window, session first, with no heading, totals, note or button", () => {
    const html = render({ report });
    assert.equal(html.match(/role="progressbar"/g)?.length, 2);
    assert.ok(html.indexOf("localUsage.window.5h") < html.indexOf("localUsage.window.7d"));
    assert.match(html, /localUsage\.used/);
    assert.match(html, /localUsage\.resets/);
    assert.match(html, /localUsage\.capped/);
    assert.doesNotMatch(html, /localUsage\.(title|intro|skynet|billingNote|refresh)/);
    assert.doesNotMatch(html, /<button(?![^>]*role="radio")/);
  });

  void it("toggles between Claude Code and Codex, with Claude Code first", () => {
    const html = render({ report });
    assert.match(html, /role="radiogroup"/);
    assert.ok(html.indexOf("subscription.cli.claude") < html.indexOf("subscription.cli.codex"));
    const codex = render({ report, defaultCli: "codex" });
    assert.doesNotMatch(codex, /role="progressbar"/);
    assert.match(codex, /localUsage\.unavailable/);
  });

  void it("asks the user to sign in when the backend could not refresh the sign-in", () => {
    const html = render({ report: withQuota("claude", { status: "reconnect", windows: [] }) });
    assert.match(html, /localUsage\.reconnect/);
    assert.doesNotMatch(html, /role="progressbar"/);
  });

  void it("says so when the CLI reports no window", () => {
    assert.match(render({ report: withQuota("claude", { status: "ok", windows: [] }) }), /localUsage\.noWindows/);
  });

  void it("gives the sign-in line without a Refresh step in the UI catalogs", () => {
    const ui = new URL("../../../../../i18n/locales/ui/", import.meta.url);
    const text = (locale: string) =>
      (JSON.parse(readFileSync(new URL(`${locale}.json`, ui), "utf8")) as Record<string, string>)[
        "localUsage.reconnect"
      ] ?? "";
    assert.match(text("en"), /signed out/);
    assert.doesNotMatch(text("en"), /Refresh/);
    // he.json is the base catalog, so the key must be translated there too.
    assert.ok(text("he"));
    assert.notEqual(text("he"), text("en"));
  });

  void it("shows the CLI toggle as icons, with each CLI name for screen readers", () => {
    const html = render({ report });
    assert.match(html, /aria-label="subscription\.cli\.claude"/);
    assert.match(html, /aria-label="subscription\.cli\.codex"/);
    assert.doesNotMatch(html, /<span class="truncate">subscription\.cli/);
  });

  void it("puts the signed-in account behind an info icon, not in the page text", () => {
    const html = render({
      report: withQuota("claude", {
        status: "ok",
        windows: [],
        account: { email: "dev@example.com", plan: "Claude Max" },
      }),
    });
    assert.match(html, /aria-label="localUsage\.accountInfo"/);
    assert.doesNotMatch(html, /dev@example\.com|Claude Max/);
    assert.doesNotMatch(render({ report }), /localUsage\.accountInfo/);
  });

  void it("shows the same model and product sections for both CLIs, and credits only for Codex", () => {
    const credits = { balance: null, resets_available: null, resets_expire_at: null };
    const claude = render({
      report: withQuota("claude", {
        status: "ok",
        windows: [{ window: "7d", used_percent: 100, resets_at: "2026-11-12T15:00:00+00:00" }],
        models: [{ name: "Opus", used_percent: 12, resets_at: null }],
        products: [{ name: "Claude Code", percent: 99 }],
        credits: { ...credits, balance: 3 },
      }),
    });
    assert.doesNotMatch(claude, /localUsage\.limitHit/);
    assert.match(claude, /Opus/);
    assert.match(claude, /localUsage\.products/);
    assert.match(claude, /Claude Code/);
    assert.doesNotMatch(claude, /localUsage\.(extras|creditsLeft|reset\.)/);
    const codex = render({
      defaultCli: "codex",
      report: withQuota("codex", {
        status: "ok",
        windows: [{ window: "7d", used_percent: 40, resets_at: null }],
        credits: { balance: 12.5, resets_available: 2, resets_expire_at: "2026-12-01T00:00:00+00:00" },
      }),
    });
    assert.doesNotMatch(codex, /localUsage\.products/);
    assert.match(codex, /localUsage\.extras/);
    assert.match(codex, /localUsage\.creditsLeft/);
    assert.match(codex, />13</);
    assert.match(codex, /localUsage\.reset\.count/);
    // The note never names an expiry date, even when the CLI reports one.
    assert.match(codex, /localUsage\.reset\.note/);
    // Without a reset action, for example in a test or a read-only view, no button shows.
    assert.doesNotMatch(codex, /localUsage\.reset\.use/);
  });

  void it("offers the Codex reset as an icon button named for screen readers", () => {
    const onCodexReset = async () => "reset" as const;
    const html = render({
      defaultCli: "codex",
      report: withQuota("codex", {
        status: "ok",
        windows: [{ window: "7d", used_percent: 100, resets_at: null }],
        credits: { balance: null, resets_available: 1, resets_expire_at: null },
      }),
      onCodexReset,
    });
    assert.match(html, /data-size="icon-sm"[^>]*aria-label="localUsage\.reset\.use"|aria-label="localUsage\.reset\.use"[^>]*data-size="icon-sm"/);
    assert.match(html, /localUsage\.reset\.note/);
    const none = render({
      defaultCli: "codex",
      report: withQuota("codex", {
        status: "ok",
        windows: [],
        credits: { balance: 4, resets_available: 0, resets_expire_at: null },
      }),
      onCodexReset,
    });
    assert.doesNotMatch(none, /localUsage\.reset\./);
  });

  void it("shows loading before the first report", () => {
    const html = render({ loading: true });
    assert.match(html, /localUsage\.loading/);
    assert.match(html, /agent-shimmer-text/);
  });

  void it("shows an error row when the load fails", () => {
    assert.match(render({ failed: true }), /localUsage\.loadFailed/);
  });
});
