"use client";

import { Desktop, Globe } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { useWizardV2ShellOptional } from "../../hooks/use-unified-wizard";
import { ChoiceIconTile } from "./ChoiceIconTile";

const PLACES = [
  {
    source: "hosted",
    icon: Globe,
    gesture: "spin",
    label: "submit.v2.run_place.remote",
    desc: "submit.v2.run_place.remote_desc",
  },
  {
    source: "local",
    icon: Desktop,
    gesture: "boot",
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
        {PLACES.map(({ source, icon: Icon, gesture, label, desc }) => {
          const chosen = runOn === source;
          return (
            // Styled and animated like the repository source cards
            // (SourceCard in RepoTargetCard.tsx), so the two choices match.
            <button
              key={source}
              type="button"
              role="radio"
              aria-checked={chosen}
              disabled={submitting}
              onClick={() => setRunOn(source)}
              className={cn(
                "group relative isolate flex min-h-20 flex-1 cursor-pointer items-center gap-4 overflow-hidden rounded-xl border bg-card px-5 py-4 text-start outline-none",
                "transition-[translate,scale,border-color,box-shadow,background-color] duration-300 ease-out motion-reduce:transition-none",
                "hover:-translate-y-0.5 hover:shadow-[0_10px_30px_-12px] hover:shadow-primary-fill/40 active:translate-y-0 active:scale-[0.99] motion-reduce:hover:translate-y-0",
                "focus-visible:ring-[3px] focus-visible:ring-ring/50",
                "disabled:pointer-events-none disabled:opacity-60",
                chosen
                  ? "border-primary-fill bg-primary-fill/5 shadow-[0_0_0_1px] shadow-primary-fill/40"
                  : "border-border hover:border-primary-fill/50",
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "pointer-events-none absolute -top-16 -start-16 -z-10 size-48 rounded-full bg-primary-fill/20 blur-3xl transition-opacity duration-500",
                  chosen ? "opacity-70" : "opacity-0 group-hover:opacity-100",
                )}
              />
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-y-0 -start-1/2 -z-10 w-1/3 -skew-x-12 bg-gradient-to-r from-transparent via-foreground/[0.06] to-transparent opacity-0 transition-[inset-inline-start,opacity] duration-700 ease-out group-hover:start-[120%] group-hover:opacity-100 motion-reduce:hidden"
              />
              <ChoiceIconTile selected={chosen} gesture={gesture}>
                <Icon
                  className={cn(
                    "size-5 transition-transform duration-300 ease-out motion-safe:group-hover:scale-110 motion-reduce:transition-none",
                    source === "hosted" && "motion-safe:group-hover:-rotate-12",
                  )}
                  aria-hidden="true"
                />
              </ChoiceIconTile>
              <span className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="text-sm font-semibold text-foreground">{msg(label)}</span>
                <span className="text-xs leading-relaxed text-muted-foreground">{msg(desc)}</span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
