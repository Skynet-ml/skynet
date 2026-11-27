/*
 * node --test cannot load .tsx, so run it from frontend/ through jiti:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ApiError, type NorthStarMetricsResponse } from "@/shared/lib/api";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { AdminMetricsView, stateFromError } = await import("./AdminMetricsCard");

function mondays(count: number): string[] {
  const newest = Date.UTC(2026, 9, 12);
  return Array.from({ length: count }, (_, i) =>
    new Date(newest - i * 7 * 86_400_000).toISOString().slice(0, 10),
  );
}

function seeded(overrides: Partial<NorthStarMetricsResponse> = {}): NorthStarMetricsResponse {
  const weeks = mondays(8);
  return {
    weeks: 8,
    first_lifts: weeks.map((week, i) => ({
      signup_week: week,
      signups: 20 - i,
      first_lifts: 7 - (i % 3),
    })),
    lifted_users: weeks.map((week, i) => ({ week, new_lifted: 3 + i, returning_lifted: 2 })),
    ...overrides,
  };
}

function bodyRows(html: string): number[] {
  return html
    .split("<tbody")
    .slice(1)
    .map((part) => (part.split("</tbody>")[0] ?? "").split("<tr").length - 1);
}

void describe("AdminMetricsView", () => {
  void it("shows a labelled headline and splits signup and activity weeks", () => {
    const html = renderToStaticMarkup(
      <TooltipProvider>
        <AdminMetricsView state={{ kind: "ready", data: seeded() }} />
      </TooltipProvider>,
    );
    const activated = html.split('data-testid="admin-metrics-activated"')[1] ?? "";
    assert.match(
      activated,
      /settings\.admin\.activity\.activated|Activated this week|הופעלו השבוע/,
    );
    assert.match(activated, />7<\/p>/);
    assert.match(activated, /20/);
    assert.deepEqual(bodyRows(html), [8, 8]);
    assert.equal(html.includes("<h3"), false);
    assert.match(html, /role="heading"/);
    assert.equal(/lift/i.test(html), false);
  });

  void it("drops the explanatory lines in the compact view but keeps both tables", () => {
    const render = (compact: boolean) =>
      renderToStaticMarkup(
        <TooltipProvider>
          <AdminMetricsView state={{ kind: "ready", data: seeded() }} compact={compact} />
        </TooltipProvider>,
      );
    const full = render(false);
    const compact = render(true);
    assert.equal(
      full.split("text-muted-foreground").length > compact.split("text-muted-foreground").length,
      true,
    );
    assert.equal(compact.split('<p class="text-[0.6875rem] text-muted-foreground"').length, 1);
    assert.deepEqual(bodyRows(compact), [8, 8]);
  });

  void it("renders nothing for a non-admin (403)", () => {
    const state = stateFromError(new ApiError("Forbidden", { status: 403 }));
    assert.deepEqual(state, { kind: "forbidden" });
    assert.equal(renderToStaticMarkup(<AdminMetricsView state={state} />), "");
  });

  void it("shows an error row for any other failure", () => {
    const state = stateFromError(new ApiError("boom", { status: 500 }));
    assert.deepEqual(state, { kind: "error" });
    assert.match(renderToStaticMarkup(<AdminMetricsView state={state} />), /role="alert"/);
    assert.deepEqual(stateFromError(new Error("offline")), { kind: "error" });
  });

  void it("shows the empty state when no week has signups", () => {
    const data = seeded();
    data.first_lifts = data.first_lifts.map((row) => ({ ...row, signups: 0, first_lifts: 0 }));
    const html = renderToStaticMarkup(<AdminMetricsView state={{ kind: "ready", data }} />);
    assert.equal(html.includes("<table"), false);
    assert.match(html, /settings\.admin\.activity\.empty|No signups yet|אין נרשמים עדיין/);
  });

  void it("shows a loader while loading", () => {
    assert.match(
      renderToStaticMarkup(<AdminMetricsView state={{ kind: "loading" }} />),
      /role="status"/,
    );
  });
});
