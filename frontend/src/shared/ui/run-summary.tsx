"use client";

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { ModelChip } from "@/shared/ui/model-chip";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import type { ModelConfig } from "@/shared/types/api";

const EASE = [0.2, 0.8, 0.2, 1] as const;

/** A saved or proposed run fact, with the same compact label/value layout. */
export function SummaryRow({
  icon,
  label,
  help,
  children,
}: {
  icon?: ReactNode;
  label: ReactNode;
  help?: string;
  children: ReactNode;
}) {
  const labelNode = (
    <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground [&_svg]:size-3.5 [&_svg]:shrink-0">
      {icon}
      {label}
    </span>
  );
  return (
    <div className="flex min-w-0 items-center justify-between gap-3 border-b border-border/40 py-2.5">
      <dt className="min-w-0">{help ? <HelpTip text={help}>{labelNode}</HelpTip> : labelNode}</dt>
      <dd className="max-w-[55%] break-words text-end text-sm font-medium">{children}</dd>
    </div>
  );
}

/** Prose stays readable in full above the next summary fact. */
export function SummaryTextRow({
  icon,
  label,
  children,
}: {
  icon?: ReactNode;
  label: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5 border-b border-border/40 py-2.5">
      <dt className="flex items-center gap-2 text-xs text-muted-foreground [&_svg]:size-3.5 [&_svg]:shrink-0">
        {icon}
        {label}
      </dt>
      <dd className="whitespace-pre-wrap break-words text-sm text-foreground">
        <bdi>{children}</bdi>
      </dd>
    </div>
  );
}

/** Read-only model chip, named once for screen readers. */
export function SummaryModel({
  role,
  config,
  help,
}: {
  role: ReactNode;
  config: ModelConfig | null;
  help?: string;
}) {
  return (
    <div className="space-y-2 rounded-lg">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <span className="font-medium">{help ? <HelpTip text={help}>{role}</HelpTip> : role}</span>
      </div>
      <span className="sr-only">{config?.name || msg("shared.model_chip.not_configured")}</span>
      <div inert>
        <ModelChip
          config={config ?? { name: "" }}
          roleLabel={typeof role === "string" ? role : ""}
          onClick={() => {}}
        />
      </div>
    </div>
  );
}

function useContentHeight() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ height: number; instant: boolean } | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const height = el.offsetHeight;
      setSize((prev) => ({ height, instant: !prev?.height || !height }));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, size] as const;
}

/** The shared summary navigation for submission and saved run configuration. */
export function SummaryTabs<Id extends string>({
  tabs,
  label,
  children,
}: {
  tabs: ReadonlyArray<{ id: Id; label: string; icon: ReactNode }>;
  label: string;
  children: (current: Id) => ReactNode;
}) {
  const [active, setActive] = useState<Id | undefined>(tabs[0]?.id);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const index = Math.max(
    0,
    tabs.findIndex((tab) => tab.id === active),
  );
  const current = tabs[index];
  const baseId = useId();
  const reduceMotion = useReducedMotion();
  const [contentRef, size] = useContentHeight();
  if (!current) return null;
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const rtl = getComputedStyle(event.currentTarget).direction === "rtl";
    const step =
      event.key === "ArrowRight" ? (rtl ? -1 : 1) : event.key === "ArrowLeft" ? (rtl ? 1 : -1) : 0;
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? tabs.length - 1
          : step
            ? (index + step + tabs.length) % tabs.length
            : -1;
    const target = tabs[next];
    if (!target) return;
    event.preventDefault();
    setActive(target.id);
    tabRefs.current[next]?.focus();
  };
  return (
    <>
      <div
        role="tablist"
        aria-label={label}
        className="relative flex shrink-0 gap-0.5 border-b border-border bg-secondary/50 p-1"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute bottom-1 top-1 rounded-lg bg-background shadow-sm transition-[inset-inline-start] duration-200 ease-out motion-reduce:transition-none"
          style={{
            width: `calc((100% - 8px - ${(tabs.length - 1) * 2}px) / ${tabs.length})`,
            insetInlineStart: `calc(4px + ${index} * ((100% - 8px - ${(tabs.length - 1) * 2}px) / ${tabs.length} + 2px))`,
          }}
        />
        {tabs.map((tab, i) => (
          <button
            key={tab.id}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            type="button"
            role="tab"
            id={`${baseId}-tab-${tab.id}`}
            aria-selected={current.id === tab.id}
            aria-controls={`${baseId}-panel`}
            tabIndex={current.id === tab.id ? 0 : -1}
            onClick={() => setActive(tab.id)}
            onKeyDown={onKeyDown}
            className={cn(
              "relative z-10 flex min-h-[44px] min-w-0 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-lg py-2.5 text-xs font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:min-h-0 [&_svg]:size-3.5 [&_svg]:shrink-0",
              current.id === tab.id
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground/80",
            )}
          >
            {tab.icon}
            <span className="sr-only sm:not-sr-only">{tab.label}</span>
          </button>
        ))}
      </div>
      <motion.div
        className="min-h-0 shrink overflow-y-auto"
        initial={false}
        animate={{ height: size?.height ?? "auto" }}
        transition={{ duration: reduceMotion || size?.instant ? 0 : 0.2, ease: EASE }}
      >
        <div ref={contentRef} className="p-4 sm:p-5">
          <AnimatePresence mode="wait" initial={false}>
            <motion.dl
              key={current.id}
              role="tabpanel"
              id={`${baseId}-panel`}
              aria-labelledby={`${baseId}-tab-${current.id}`}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: reduceMotion ? 0 : 0.15, ease: EASE }}
              className="[&>div:last-child]:border-b-0"
            >
              {children(current.id)}
            </motion.dl>
          </AnimatePresence>
        </div>
      </motion.div>
    </>
  );
}
