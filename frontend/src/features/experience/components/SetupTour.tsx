"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";

import { msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { ArrowLeft, ArrowRight, X } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";

import { anchorSelector, arrowStep, type SetupStop } from "../lib/setup-tour";
import { SpotlightMask } from "./SpotlightMask";
import { TOUCH_TAP_SQUARE } from "./touch";

const CARD_W = 360;
const WIDE_CARD_W = 720;
const GAP = 16;
const EDGE = 12;
/** How long a stop waits for its anchor to mount before it centers the card instead. */
const ANCHOR_WAIT_MS = 1500;

/*
 * Over the header (z 30), the agent pill (z 40) and the Settings modal (z 50),
 * which a stop opens to frame one of its settings, so the dim covers it all.
 */
const MASK_Z = 51;
const CARD_Z = 52;

type Placement = { top: number; left: number; width?: number };

function visible(el: Element): boolean {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && window.getComputedStyle(el).visibility !== "hidden";
}

function findAnchor(anchor: string): Element | null {
  return Array.from(document.querySelectorAll(anchorSelector(anchor))).find(visible) ?? null;
}

/**
 * The part of the anchor its scrolling ancestors show. A Settings tab taller
 * than the modal would otherwise frame the page below the modal too.
 */
function shownRect(el: Element): DOMRect {
  const r = el.getBoundingClientRect();
  let top = r.top;
  let bottom = r.bottom;
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (!/(auto|scroll|hidden)/.test(window.getComputedStyle(p).overflowY)) continue;
    const box = p.getBoundingClientRect();
    top = Math.max(top, box.top);
    bottom = Math.min(bottom, box.bottom);
  }
  return new DOMRect(r.x, top, r.width, Math.max(0, bottom - top));
}

function center(cardH: number): Placement {
  const w = Math.min(CARD_W, window.innerWidth - EDGE * 2);
  return {
    top: Math.max(EDGE, (window.innerHeight - cardH) / 2),
    left: (window.innerWidth - w) / 2,
  };
}

/** A wide card over the anchor, centred on it, so a row of choices fits across it. */
function placeAbove(rect: DOMRect, cardH: number): Placement {
  const vw = window.innerWidth;
  const width = Math.min(WIDE_CARD_W, rect.width, vw - EDGE * 2);
  const left = rect.left + rect.width / 2 - width / 2;
  return {
    top: Math.max(EDGE, Math.min(rect.top - cardH - GAP, window.innerHeight - cardH - EDGE)),
    left: Math.max(EDGE, Math.min(left, vw - width - EDGE)),
    width,
  };
}

/**
 * Beside the anchor on the side with the most room, kept on screen; a target
 * too big to sit beside (the dashboard's table) gets the card in the screen's
 * bottom-end corner, so the part that changes stays in view. In Settings that
 * corner holds the rows' buttons, so the card takes the start corner, over
 * the tab rail.
 */
function place(rect: DOMRect, cardH: number, rtl: boolean, inSettings: boolean): Placement {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = Math.min(CARD_W, vw - EDGE * 2);
  const sides = [
    { side: "bottom", fits: vh - rect.bottom >= cardH + GAP + EDGE, space: vh - rect.bottom },
    { side: "top", fits: rect.top >= cardH + GAP + EDGE, space: rect.top },
    { side: "right", fits: vw - rect.right >= w + GAP + EDGE, space: vw - rect.right },
    { side: "left", fits: rect.left >= w + GAP + EDGE, space: rect.left },
  ].filter((s) => s.fits);
  if (sides.length === 0) {
    const onLeft = inSettings !== rtl;
    return {
      top: Math.max(EDGE, vh - cardH - EDGE * 2),
      left: onLeft ? EDGE * 2 : vw - w - EDGE * 2,
    };
  }
  const side = sides.sort((a, b) => b.space - a.space)[0]!.side;
  let top: number;
  let left: number;
  if (side === "bottom" || side === "top") {
    top = side === "bottom" ? rect.bottom + GAP : rect.top - cardH - GAP;
    left = rect.left + rect.width / 2 - w / 2;
  } else {
    top = rect.top + rect.height / 2 - cardH / 2;
    left = side === "right" ? rect.right + GAP : rect.left - w - GAP;
  }
  return {
    top: Math.max(EDGE, Math.min(top, vh - cardH - EDGE)),
    left: Math.max(EDGE, Math.min(left, vw - w - EDGE)),
  };
}

/**
 * One stop of the setup over the real app: the spotlight frames the part it
 * is about, and the card beside it explains it or asks its setting. The app
 * behind stays visible, so an answer that changes it shows right away.
 */
export function SetupTour({
  stop,
  index,
  total,
  children,
  onBack,
  onNext,
  onSkip,
}: {
  stop: SetupStop;
  index: number;
  total: number;
  /** The stop's question, when it asks one. */
  children?: React.ReactNode;
  onBack: () => void;
  onNext: () => void;
  onSkip: () => void;
}) {
  const reduceMotion = useReducedMotion() ?? false;
  const cardRef = React.useRef<HTMLDivElement>(null);
  const [rect, setRect] = React.useState<DOMRect | null>(null);
  const [placement, setPlacement] = React.useState<Placement | null>(null);

  React.useEffect(() => {
    let raf = 0;
    let last = "";
    let scrolled = false;
    const started = performance.now();
    setRect(null);
    setPlacement(null);
    // Polled rather than observed: the anchor can mount late (the sidebar's
    // run list loads after the page) and move as the level reshapes the page.
    const tick = (now: number) => {
      const el = findAnchor(stop.anchor);
      const cardH = cardRef.current?.offsetHeight ?? 280;
      const view = `${cardH}|${window.innerWidth}|${window.innerHeight}`;
      if (el) {
        // A setting inside Settings can sit below its tab's fold; bring it in
        // once, and the polling follows it while it scrolls.
        if (!scrolled && stop.settings) {
          scrolled = true;
          el.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
        }
        const r = stop.settings ? shownRect(el) : el.getBoundingClientRect();
        const key = `${r.x}|${r.y}|${r.width}|${r.height}|${view}`;
        if (key !== last) {
          last = key;
          setRect(r);
          setPlacement(
            stop.above
              ? placeAbove(r, cardH)
              : place(r, cardH, getActiveDir() === "rtl", !!stop.settings),
          );
        }
      } else if (now - started > ANCHOR_WAIT_MS && last !== `center|${view}`) {
        last = `center|${view}`;
        setRect(null);
        setPlacement(center(cardH));
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [stop.anchor, stop.settings, stop.above, reduceMotion]);

  // Menus and tooltips opened from the framed Settings controls rise over the dim (globals.css).
  React.useEffect(() => {
    document.documentElement.setAttribute("data-setup-tour", "");
    return () => document.documentElement.removeAttribute("data-setup-tour");
  }, []);

  const onArrow = React.useEffectEvent((event: KeyboardEvent) => {
    const step = arrowStep(event, getActiveDir() === "rtl");
    if (step === 0) return;
    event.preventDefault();
    if (step === 1) onNext();
    else onBack();
  });
  React.useEffect(() => {
    const listener = (event: KeyboardEvent) => onArrow(event);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  // Focus moves to a stop's card once, when it first shows, never on a later move.
  const placed = placement !== null;
  React.useEffect(() => {
    if (placed) cardRef.current?.focus({ preventScroll: true });
  }, [placed, stop.id]);

  const rtl = getActiveDir() === "rtl";
  const BackArrow = rtl ? ArrowRight : ArrowLeft;
  const NextArrow = rtl ? ArrowLeft : ArrowRight;

  return createPortal(
    <>
      {/* A Settings stop leaves its framed controls usable; elsewhere the page stays still. */}
      <div
        className={cn("fixed inset-0", stop.settings && rect && "pointer-events-none")}
        style={{ zIndex: MASK_Z }}
      >
        <SpotlightMask targetRect={rect} interactive={!!stop.settings} />
      </div>
      <AnimatePresence mode="wait">
        <motion.div
          key={stop.id}
          ref={cardRef}
          role="dialog"
          aria-labelledby={`setup-stop-${stop.id}`}
          tabIndex={-1}
          dir={getActiveDir()}
          className={cn(
            "fixed flex max-h-[calc(100dvh-24px)] w-[min(calc(100vw-24px),360px)] flex-col gap-3 overflow-y-auto overscroll-contain rounded-2xl border border-border bg-background p-4 text-foreground shadow-[var(--shadow-float)] outline-none sm:p-5",
            !placement && "invisible",
          )}
          style={{
            top: placement?.top ?? 0,
            left: placement?.left ?? 0,
            width: placement?.width,
            zIndex: CARD_Z,
          }}
          initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduceMotion ? 0.12 : 0.2, ease: [0.22, 1, 0.36, 1] }}
        >
          <div className="flex gap-1" aria-hidden>
            {Array.from({ length: total }, (_, i) => (
              <span
                key={i}
                className={cn(
                  "h-1 flex-1 rounded-full transition-colors duration-300",
                  i <= index ? "bg-foreground" : "bg-border",
                )}
              />
            ))}
          </div>
          <div className="flex flex-col gap-1">
            <h2
              id={`setup-stop-${stop.id}`}
              className="text-base font-semibold leading-snug text-balance"
            >
              {msg(stop.title)}
            </h2>
            {stop.body && (
              <p className="text-sm leading-relaxed text-muted-foreground">{msg(stop.body)}</p>
            )}
          </div>
          {children}
          <div className="flex items-center gap-2 pt-1">
            <Button
              variant="ghost"
              size="icon"
              className={cn(TOUCH_TAP_SQUARE, "-ms-2 text-muted-foreground hover:text-foreground")}
              autoTooltip={false}
              aria-label={msg("experience.intake.skip")}
              onClick={onSkip}
            >
              <X className="size-4" aria-hidden />
            </Button>
            <div className="ms-auto flex items-center gap-2">
              <Button
                variant="outline"
                size="icon"
                className={TOUCH_TAP_SQUARE}
                autoTooltip={false}
                aria-label={msg("experience.intake.back")}
                onClick={onBack}
              >
                <BackArrow className="size-4" aria-hidden />
              </Button>
              <Button
                size="icon"
                className={TOUCH_TAP_SQUARE}
                autoTooltip={false}
                aria-label={msg("experience.intake.next")}
                onClick={onNext}
              >
                <NextArrow className="size-4" aria-hidden />
              </Button>
            </div>
          </div>
        </motion.div>
      </AnimatePresence>
    </>,
    document.body,
  );
}
