"use client";

import * as React from "react";
import { motion, useReducedMotion } from "motion/react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";

import { RAIL_MIN_ITEMS, currentRange, markProgress } from "./message-rail";

/** One user message on the rail; `index` is its place in the chat's messages. */
export interface RailItem {
  index: number;
  /** The message's first line; an edit changes it, so it keys the rail's order. */
  label: string;
}

// Codex's mark: a 26 px line that rests at 6 px and springs to full length.
const LINE = 26;
const REST = 6 / LINE;
const SPRING =
  "linear(0,.398 10%,.682 20%,.843 30%,.925 40%,.972 50%,1.004 60%,1.008 70%,1.003 80%,1)";

/**
 * Codex's user message rail: a column of short marks at the thread's start edge, one for
 * each user message. A mark grows on hover with its neighbors and jumps to its turn on
 * click; dragging along the rail scrubs through the turns.
 */
export function UserMessageRail({
  items,
  scrollBox,
}: {
  items: readonly RailItem[];
  /** The thread's scroll box. */
  scrollBox: HTMLElement | null;
}) {
  const reduceMotion = useReducedMotion();
  const listRef = React.useRef<HTMLDivElement>(null);
  const [pointed, setPointed] = React.useState<number | null>(null);
  const [scrubbing, setScrubbing] = React.useState(false);
  const scrub = React.useRef<{ last: number; moved: boolean } | null>(null);
  const swallowClick = React.useRef(false);
  // A streamed token makes new items with the same user messages. The key keeps `order`,
  // and so the observer, until a user message changes.
  const orderKey = JSON.stringify(items.map((item) => [item.index, item.label]));
  const order = React.useMemo(
    () => (JSON.parse(orderKey) as Array<[number, string]>).map(([index]) => index),
    [orderKey],
  );
  const [current, setCurrent] = React.useState<ReadonlySet<number>>(() => new Set(order.slice(-1)));
  const shown = items.length >= RAIL_MIN_ITEMS;
  const count = new Intl.NumberFormat(getActiveIntlLocale());

  // The marks of the turns on screen are current; 16 px off the top keeps a turn that only
  // shows its last line from counting.
  React.useEffect(() => {
    const root = scrollBox;
    if (!root || !shown) return;
    const visible = new Set<number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.target.isConnected) continue;
          const index = Number((entry.target as HTMLElement).dataset.userMessage);
          if (entry.isIntersecting) visible.add(index);
          else visible.delete(index);
        }
        setCurrent(currentRange(order, visible));
      },
      { root, rootMargin: "-16px 0px 0px 0px" },
    );
    // An edit unmounts the later turns, and a cancel mounts them again as new nodes with
    // the same order, so the observer follows the thread's nodes, not only `order`.
    const observed = new Set<HTMLElement>();
    const sync = () => {
      for (const el of observed) {
        if (el.isConnected) continue;
        observer.unobserve(el);
        observed.delete(el);
        visible.delete(Number(el.dataset.userMessage));
      }
      root.querySelectorAll<HTMLElement>("[data-user-message]").forEach((el) => {
        if (observed.has(el)) return;
        observed.add(el);
        observer.observe(el);
      });
    };
    sync();
    const mutations = new MutationObserver(sync);
    mutations.observe(root, { childList: true, subtree: true });
    return () => {
      mutations.disconnect();
      observer.disconnect();
    };
  }, [scrollBox, order, shown]);

  // A long rail scrolls itself to keep the first current mark in view.
  React.useEffect(() => {
    const list = listRef.current;
    if (!list || scrubbing) return;
    const mark = list.querySelector<HTMLElement>('[aria-current="true"]');
    if (!mark) return;
    if (mark.offsetTop < list.scrollTop) list.scrollTop = mark.offsetTop;
    else if (mark.offsetTop + mark.offsetHeight > list.scrollTop + list.clientHeight)
      list.scrollTop = mark.offsetTop + mark.offsetHeight - list.clientHeight;
  }, [current, scrubbing]);

  if (!shown) return null;

  const jump = (index: number, smooth: boolean) => {
    const root = scrollBox;
    const turn = root?.querySelector<HTMLElement>(`[data-user-message="${index}"]`);
    if (!root || !turn) return;
    // The thread scrolls by itself, so the page around it stays put; 16 px matches its padding.
    const top =
      root.scrollTop + turn.getBoundingClientRect().top - root.getBoundingClientRect().top - 16;
    root.scrollTo({ top, behavior: smooth && !reduceMotion ? "smooth" : "instant" });
    if (!smooth) return;
    // The bubble glows, so the eye finds the turn it jumped to.
    const glow = "0 0 0 3px color-mix(in srgb, var(--color-primary) 45%, transparent)";
    turn
      .querySelector<HTMLElement>("[data-user-message-bubble]")
      ?.animate(
        [
          { boxShadow: glow, offset: 0 },
          { boxShadow: glow, offset: 0.35 },
          { boxShadow: "0 0 0 3px transparent" },
        ],
        { duration: reduceMotion ? 0 : 1400, easing: "cubic-bezier(0.23, 1, 0.32, 1)" },
      );
  };

  // The mark under the pointer, read at the rail's middle so a drag that drifts sideways still scrubs.
  const markAt = (clientY: number) => {
    const list = listRef.current;
    if (!list) return null;
    const box = list.getBoundingClientRect();
    const y = Math.min(Math.max(clientY, box.top + 1), box.bottom - 1);
    const mark = document
      .elementFromPoint(box.left + box.width / 2, y)
      ?.closest<HTMLElement>("[data-rail-position]");
    return mark ? Number(mark.dataset.railPosition) : null;
  };

  const endScrub = () => {
    if (scrub.current?.moved) {
      // The click that ends a drag must not jump again, smoothly, to the same turn.
      swallowClick.current = true;
      setTimeout(() => (swallowClick.current = false), 0);
    }
    scrub.current = null;
    setScrubbing(false);
  };

  return (
    <motion.nav
      aria-label={msg("shared.agent.rail_label")}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: reduceMotion ? 0 : 0.15 }}
      className="absolute start-1 top-1/2 z-20 -translate-y-1/2"
    >
      <div
        ref={listRef}
        data-scrubbing={scrubbing || undefined}
        className="relative flex max-h-[min(70vh,40rem)] flex-col overflow-y-auto overscroll-contain py-1 [scrollbar-width:none]"
        onPointerDown={(e) => {
          const at = e.button === 0 ? markAt(e.clientY) : null;
          if (at != null) scrub.current = { last: at, moved: false };
        }}
        onPointerMove={(e) => {
          const s = scrub.current;
          if (!s) return;
          // A release outside the rail before the capture starts sends no pointerup here.
          if (e.buttons === 0) return endScrub();
          const at = markAt(e.clientY);
          if (at == null || at === s.last) return;
          if (!s.moved) e.currentTarget.setPointerCapture(e.pointerId);
          scrub.current = { last: at, moved: true };
          setScrubbing(true);
          setPointed(at);
          jump(items[at]!.index, false);
        }}
        onPointerUp={endScrub}
        // Once the capture starts, the pointer does not leave; before it, a leave ends the press.
        onPointerLeave={endScrub}
        onPointerCancel={endScrub}
        onLostPointerCapture={endScrub}
      >
        {items.map((item, position) => {
          const isCurrent = current.has(item.index);
          const isPointed = pointed === position;
          const scale = REST + (1 - REST) * markProgress(position, pointed);
          const motionStyle = {
            transition: scrubbing || reduceMotion ? "none" : `all 0.16s ${SPRING}`,
          };
          return (
            <button
              key={item.index}
              type="button"
              data-rail-position={position}
              aria-current={isCurrent || undefined}
              aria-label={formatMsg("shared.agent.rail_jump", {
                position: count.format(position + 1),
              })}
              onPointerEnter={() => !scrub.current && setPointed(position)}
              onPointerLeave={() =>
                !scrub.current && setPointed((p) => (p === position ? null : p))
              }
              onFocus={(e) => e.currentTarget.matches(":focus-visible") && setPointed(position)}
              onBlur={() => setPointed((p) => (p === position ? null : p))}
              onClick={() => !swallowClick.current && jump(item.index, true)}
              className="flex h-2.5 w-9 shrink-0 cursor-pointer items-center outline-none"
            >
              <span
                aria-hidden
                className={cn(
                  "relative h-0.5 w-[30px] transition-[color,opacity] duration-150",
                  isPointed
                    ? "text-foreground opacity-100"
                    : isCurrent && pointed == null
                      ? "text-foreground opacity-60"
                      : "text-muted-foreground opacity-40",
                )}
              >
                <span
                  className="absolute inset-y-0 start-0 bg-current ltr:origin-left rtl:origin-right"
                  style={{ width: LINE, transform: `scaleX(${scale})`, ...motionStyle }}
                />
              </span>
            </button>
          );
        })}
      </div>
    </motion.nav>
  );
}
