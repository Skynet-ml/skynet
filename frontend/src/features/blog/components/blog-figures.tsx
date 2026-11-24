"use client";

/**
 * The behavior of the live blog figures (.bf): marks that arrive when a
 * figure scrolls into view, the chart tooltips, the ARC-AGI-3 score or cost
 * toggle and harness legend, and the focus of the six levers.
 *
 * The figure HTML is static markup from content/<slug>.figures.json, so the
 * behavior attaches to it here, after hydration, through the DOM.
 */

import * as React from "react";

/** Set the ARC-AGI-3 view to the choice of this toggle button. */
function pick(button: HTMLButtonElement) {
  const group = button.parentElement;
  const view = button.closest<HTMLElement>(".arcv");
  if (!group || !view) return;
  const buttons = Array.from(group.querySelectorAll("button"));
  view.dataset.m = button.dataset.m;
  group.style.setProperty("--i", String(buttons.indexOf(button)));
  for (const b of buttons) {
    b.setAttribute("aria-checked", String(b === button));
    b.tabIndex = b === button ? 0 : -1;
  }
}

/** Pin the focus of the levers figure on lever `f`, or clear it when `f` is empty. */
function focusLever(levers: HTMLElement, f: string) {
  if (f) levers.dataset.f = f;
  else delete levers.dataset.f;
  for (const li of levers.querySelectorAll<HTMLElement>(".lev li")) {
    li.setAttribute("aria-pressed", String(li.dataset.l === f));
  }
}

/** Place a fixed tooltip next to the pointer, inside the window. */
function placeAtPointer(tip: HTMLElement, e: PointerEvent) {
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  let x = e.clientX + 14;
  let y = e.clientY + 16;
  if (x + w > innerWidth - 8) x = Math.max(8, e.clientX - w - 14);
  if (y + h > innerHeight - 8) y = Math.max(8, e.clientY - h - 12);
  tip.style.transform = `translate(${x}px,${y}px)`;
}

/** Place the caret tooltip above its trigger, or below when the top has no room. */
function placeAbove(tip: HTMLElement, el: Element) {
  const r = el.getBoundingClientRect();
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  const center = r.left + r.width / 2;
  let y = r.top - 10 - h;
  const below = y < 8;
  if (below) y = r.bottom + 10;
  const x = Math.max(8, Math.min(innerWidth - w - 8, center - w / 2));
  tip.style.transform = `translate(${x}px,${y}px)`;
  tip.style.setProperty("--ox", `${center - x}px`);
  tip.classList.toggle("b", below);
}

/** Attach the figure behavior to every .bf on the page, and render the two shared tooltips. */
export function BlogFigureBehavior() {
  const tipRef = React.useRef<HTMLDivElement>(null);
  const atipRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    const tip = tipRef.current;
    const atip = atipRef.current;
    if (!tip || !atip) return;
    const figures = Array.from(document.querySelectorAll<HTMLElement>(".bf"));

    // A figure in view at load keeps its final state, so nothing on screen blinks out.
    // A figure starts when 60% of it shows, or when it fills 60% of the window (a tall figure).
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.intersectionRatio >= 0.6 || e.intersectionRect.height >= innerHeight * 0.6) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }
      },
      { threshold: [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1] },
    );
    for (const fig of figures) {
      const card = fig.querySelector(".card");
      if (!card) continue;
      const r = card.getBoundingClientRect();
      if (r.top < innerHeight && r.bottom > 0) {
        card.classList.add("in");
        continue;
      }
      fig.classList.add("motion");
      io.observe(card);
    }

    let current: Element | null = null;
    const hideTip = () => {
      current = null;
      tip.classList.remove("on");
    };
    const onPointer = (e: PointerEvent) => {
      const el = (e.target as Element | null)?.closest?.(".bf [data-tt]");
      if (!el) return hideTip();
      if (el !== current) {
        current = el;
        tip.textContent = el.getAttribute("data-tt");
      }
      placeAtPointer(tip, e);
      tip.classList.add("on");
    };
    const showAtip = (el: Element) => {
      atip.firstElementChild!.textContent = el.getAttribute("data-tip");
      placeAbove(atip, el);
      atip.classList.add("on");
    };
    const hideAtip = () => atip.classList.remove("on");
    const onScroll = () => {
      hideTip();
      hideAtip();
    };

    const onClick = (e: MouseEvent) => {
      const target = e.target as Element;
      const seg = target.closest<HTMLButtonElement>(".bf .arcv .seg button");
      if (seg) return pick(seg);
      const legend = target.closest<HTMLButtonElement>(".bf .arcv .legend button");
      if (legend) {
        const view = legend.closest<HTMLElement>(".arcv")!;
        const h = view.dataset.h === legend.dataset.h ? "" : (legend.dataset.h ?? "");
        if (h) view.dataset.h = h;
        else delete view.dataset.h;
        for (const b of view.querySelectorAll<HTMLElement>(".legend button")) {
          b.setAttribute("aria-pressed", String(b.dataset.h === h));
        }
        return;
      }
      const li = target.closest<HTMLElement>(".bf .lev li");
      const levers = li?.closest<HTMLElement>(".levers");
      if (li && levers) focusLever(levers, levers.dataset.f === li.dataset.l ? "" : (li.dataset.l ?? ""));
    };
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as Element;
      const li = target.closest?.<HTMLElement>(".bf .lev li");
      const levers = li?.closest<HTMLElement>(".levers");
      if (li && levers) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          focusLever(levers, levers.dataset.f === li.dataset.l ? "" : (li.dataset.l ?? ""));
        } else if (e.key === "Escape") focusLever(levers, "");
        return;
      }
      const button = target.closest?.<HTMLButtonElement>(".bf .arcv .seg button");
      const group = button?.parentElement;
      if (!button || !group) return;
      const buttons = Array.from(group.querySelectorAll("button"));
      const i = buttons.indexOf(button);
      const n = buttons.length;
      const j = { ArrowRight: (i + 1) % n, ArrowDown: (i + 1) % n, ArrowLeft: (i + n - 1) % n, ArrowUp: (i + n - 1) % n, Home: 0, End: n - 1 }[e.key];
      if (j === undefined) return;
      e.preventDefault();
      pick(buttons[j]!);
      buttons[j]!.focus();
    };

    const onOver = (e: PointerEvent) => {
      const el = (e.target as Element).closest?.(".bf [data-tip]");
      if (el && e.pointerType === "mouse") showAtip(el);
    };
    const onOut = (e: PointerEvent) => {
      if ((e.target as Element).closest?.(".bf [data-tip]")) hideAtip();
    };
    const onFocusIn = (e: FocusEvent) => {
      const el = (e.target as Element).closest?.(".bf [data-tip]");
      if (el?.matches(":focus-visible")) showAtip(el);
    };

    document.addEventListener("pointermove", onPointer);
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("pointerdown", hideAtip);
    document.addEventListener("pointerover", onOver);
    document.addEventListener("pointerout", onOut);
    document.addEventListener("focusin", onFocusIn);
    document.addEventListener("focusout", hideAtip);
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKey);
    document.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      io.disconnect();
      document.removeEventListener("pointermove", onPointer);
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("pointerdown", hideAtip);
      document.removeEventListener("pointerover", onOver);
      document.removeEventListener("pointerout", onOut);
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("focusout", hideAtip);
      document.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("scroll", onScroll);
    };
  }, []);

  return (
    <>
      <div ref={tipRef} className="bf-tip" role="tooltip" aria-hidden="true" />
      <div ref={atipRef} className="bf-atip" aria-hidden="true">
        <span />
        <svg width="12" height="7" viewBox="0 0 12 7">
          <path d="M0 0 L6 6 L12 0" />
        </svg>
      </div>
      {/* The arrowhead that the figure lines share. */}
      <svg width="0" height="0" className="absolute" aria-hidden="true">
        <defs>
          <marker id="ah" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M2 1.5L8.5 5L2 8.5" fill="none" stroke="context-stroke" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </marker>
        </defs>
      </svg>
    </>
  );
}
