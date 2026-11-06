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
    <LocalCliUsageView report={null} loading={false} failed={false} onRefresh={() => {}} {...props} />,
  );

void describe("LocalCliUsageView", () => {
  void it("shows a percent bar for each window, session first, and the Skynet totals", () => {
    const html = render({ report });
    assert.equal(html.match(/role="progressbar"/g)?.length, 2);
    assert.ok(html.indexOf("localUsage.window.5h") < html.indexOf("localUsage.window.7d"));
    assert.match(html, /localUsage\.used/);
    assert.match(html, /localUsage\.resets/);
    assert.match(html, /localUsage\.skynet\.5h/);
    assert.match(html, /localUsage\.skynet\.7d/);
    assert.match(html, /localUsage\.capped/);
    assert.match(html, /localUsage\.billingNote/);
  });

  void it("toggles between Claude Code and Codex, with Claude Code first", () => {
    const html = render({ report });
    assert.match(html, /role="radiogroup"/);
    assert.ok(html.indexOf("subscription.cli.claude") < html.indexOf("subscription.cli.codex"));
    const codex = render({ report, defaultCli: "codex" });
    assert.doesNotMatch(codex, /role="progressbar"/);
    assert.match(codex, /localUsage\.unavailable/);
  });

  void it("asks the user to reconnect an expired sign-in", () => {
    const html = render({ report: withQuota("claude", { status: "reconnect", windows: [] }) });
    assert.match(html, /localUsage\.reconnect/);
    assert.doesNotMatch(html, /role="progressbar"/);
  });

  void it("says so when the CLI reports no window", () => {
    assert.match(render({ report: withQuota("claude", { status: "ok", windows: [] }) }), /localUsage\.noWindows/);
  });

  void it("tells the user in every UI catalog that billing does not change", () => {
    const ui = new URL("../../../../../i18n/locales/ui/", import.meta.url);
    const text = (locale: string) =>
      (JSON.parse(readFileSync(new URL(`${locale}.json`, ui), "utf8")) as Record<string, string>)[
        "localUsage.billingNote"
      ] ?? "";
    assert.match(text("en"), /does not change your web credits/);
    // he.json is the base catalog, so the key must be translated there too.
    assert.ok(text("he"));
    assert.notEqual(text("he"), text("en"));
  });

  void it("shows loading before the first report", () => {
    assert.match(render({ loading: true }), /localUsage\.loading/);
  });

  void it("shows an error row when the load fails", () => {
    assert.match(render({ failed: true }), /localUsage\.loadFailed/);
  });
});
