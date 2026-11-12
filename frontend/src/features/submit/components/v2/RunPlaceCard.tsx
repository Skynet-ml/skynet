"use client";

import { Desktop, Globe } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { useWizardV2ShellOptional } from "../../hooks/use-unified-wizard";

const PLACES = [
  {
    source: "hosted",
    icon: Globe,
    label: "submit.v2.run_place.remote",
    desc: "submit.v2.run_place.remote_desc",
  },
  {
    source: "local",
    icon: Desktop,
    label: "submit.v2.run_place.local",
    desc: "submit.v2.run_place.local_desc",
  },
] as const;

/**
 * The first question of a linked Mac's setup (SKY-340): Remote or Local. The
 * models, engines and prices of every later step follow this choice. An
 * unlinked Mac and the web app have only one place, so they show nothing.
 */
export function RunPlaceCard() {
  const shell = useWizardV2ShellOptional();
  if (!shell || !(shell.hostedChoice || shell.runOn === "hosted")) return null;
  const { runOn, setRunOn, submitting } = shell;
  return (
    <div className="flex flex-col gap-1.5">
      <span id="run-place-label" className="text-sm font-medium">
        {msg("submit.v2.run_place.title")}
      </span>
      <div
        role="radiogroup"
        aria-labelledby="run-place-label"
        className="grid grid-cols-1 gap-2 sm:grid-cols-2"
      >
        {PLACES.map(({ source, icon: Icon, label, desc }) => {
          const chosen = runOn === source;
          return (
            <button
              key={source}
              type="button"
              role="radio"
              aria-checked={chosen}
              disabled={submitting}
              onClick={() => setRunOn(source)}
              className={cn(
                "flex items-start gap-3 rounded-xl border p-3 text-start transition-colors disabled:opacity-60",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                chosen
                  ? "border-primary/60 bg-primary/5 shadow-sm"
                  : "border-border bg-card hover:border-foreground/20 hover:bg-muted/40",
              )}
            >
              <span
                className={cn(
                  "flex size-8 shrink-0 items-center justify-center rounded-lg",
                  chosen ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
                )}
              >
                <Icon className="size-4" aria-hidden />
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-sm font-medium">{msg(label)}</span>
                <span className="text-xs text-muted-foreground">{msg(desc)}</span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
