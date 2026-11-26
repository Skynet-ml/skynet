"use client";

/**
 * The behavior of the live blog figures (.bf): marks that arrive when a
 * figure scrolls into view, the chart tooltips, the ARC-AGI-3 score or cost
 * toggle and harness legend, the focus of the seven levers, the click
 * filters of each card, and the zoom view.
 *
 * The figure HTML is static markup from content/<slug>.figures.json, so the
 * behavior attaches to it here, after hydration, through the DOM.
 */

import * as React from "react";

/** Set the view of the ARC-AGI-3 figure toggle to the choice of this button. */
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

/**
 * Toggle filter `k` of a card to value `v`, then dim every [data-tg] mark that
 * has key `k` with another value. Triggers are [data-fk][data-fv] elements.
 */
function syncFilters(card: Element, state: Record<string, string>) {
  for (const el of card.querySelectorAll("[data-fk]")) {
    const k = el.getAttribute("data-fk") ?? "";
    const on = !!state[k] && state[k] === el.getAttribute("data-fv");
    el.setAttribute("aria-pressed", String(on));
    el.classList.toggle("on", on);
    el.classList.toggle("off", !!state[k] && !on);
  }
  for (const el of card.querySelectorAll("[data-tg]")) {
    const pairs = (el.getAttribute("data-tg") ?? "").split(" ").map((p) => p.split(":"));
    const hide = Object.entries(state).some(
      ([k, v]) =>
        v && pairs.some(([pk]) => pk === k) && !pairs.some(([pk, pv]) => pk === k && pv === v),
    );
    el.classList.toggle("dim", hide);
  }
}

/** Clear the ARC-AGI-3 harness legend choice of a card. */
function clearLegend(card: Element) {
  const view = card.querySelector<HTMLElement>(".arcv");
  if (!view?.dataset.h) return;
  delete view.dataset.h;
  for (const b of view.querySelectorAll(".legend button")) b.setAttribute("aria-pressed", "false");
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

/** Attach the figure behavior to every .bf on the page, and render the shared tooltips and the zoom view. */
export function BlogFigureBehavior() {
  const tipRef = React.useRef<HTMLDivElement>(null);
  const atipRef = React.useRef<HTMLDivElement>(null);
  const zoomRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    const tip = tipRef.current;
    const atip = atipRef.current;
    const modal = zoomRef.current;
    const stage = modal?.firstElementChild;
    if (!tip || !atip || !modal || !stage) return;
    const figures = Array.from(document.querySelectorAll<HTMLElement>(".bf"));

    // A figure in view at load keeps its final state, so nothing on screen blinks out.
    // A figure starts when 35% of it shows, or when it fills 35% of the window (a tall figure).
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.intersectionRatio >= 0.35 || e.intersectionRect.height >= innerHeight * 0.35) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }
      },
      // Steps of 5%, so the callback runs close to the 35% mark.
      { threshold: Array.from({ length: 21 }, (_, i) => i / 20) },
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
        const b = document.createElement("b");
        b.textContent = el.getAttribute("data-tt");
        tip.replaceChildren(b);
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

    const filters = new WeakMap<Element, Record<string, string>>();
    const toggleFilter = (el: Element) => {
      const card = el.closest(".bf .card");
      if (!card) return;
      const state = filters.get(card) ?? {};
      const k = el.getAttribute("data-fk") ?? "";
      const v = el.getAttribute("data-fv") ?? "";
      state[k] = state[k] === v ? "" : v;
      filters.set(card, state);
      syncFilters(card, state);
    };

    // The zoom view moves the live card into the modal, so the SVG ids stay unique and the filters stay as they are.
    // A placeholder of the same height keeps the post from jumping.
    const reduce = matchMedia("(prefers-reduced-motion: reduce)");
    let zoomed: HTMLElement | null = null;
    let hold: HTMLElement | null = null;
    let returnTo: HTMLElement | null = null;
    let closing: ReturnType<typeof setTimeout> | null = null;
    const setZoomButton = (card: Element, open: boolean) => {
      const zb = card.querySelector(".zbtn");
      zb?.setAttribute("aria-label", open ? "Collapse" : "Expand");
      zb?.setAttribute("aria-expanded", String(open));
    };
    const finishClose = () => {
      if (closing) clearTimeout(closing);
      closing = null;
      modal.classList.remove("on", "closing");
      modal.setAttribute("aria-hidden", "true");
      modal.removeAttribute("aria-labelledby");
      if (zoomed) {
        zoomed.classList.remove("zoomed");
        setZoomButton(zoomed, false);
        hold?.replaceWith(zoomed);
      }
      document.body.style.overflow = "";
      returnTo?.focus({ preventScroll: true });
      zoomed = hold = returnTo = null;
    };
    const closeZoom = (instant = false) => {
      if (!zoomed) return;
      if (closing) {
        if (instant) finishClose();
        return;
      }
      hideTip();
      hideAtip();
      if (instant || reduce.matches) return finishClose();
      modal.classList.remove("on");
      modal.classList.add("closing");
      closing = setTimeout(finishClose, 150);
    };
    const openZoom = (card: HTMLElement) => {
      if (closing) finishClose();
      if (zoomed) return;
      hideTip();
      hideAtip();
      card.classList.add("in");
      const cs = getComputedStyle(card);
      hold = document.createElement("div");
      hold.style.height = `${card.offsetHeight}px`;
      hold.style.marginTop = cs.marginTop;
      hold.style.marginBottom = cs.marginBottom;
      card.replaceWith(hold);
      stage.appendChild(card);
      card.classList.add("zoomed");
      setZoomButton(card, true);
      modal.classList.remove("closing");
      modal.setAttribute("aria-hidden", "false");
      const title = card.querySelector(".fh[id]");
      if (title) modal.setAttribute("aria-labelledby", title.id);
      document.body.style.overflow = "hidden";
      zoomed = card;
      returnTo = card.querySelector<HTMLElement>(".zbtn");
      void modal.offsetWidth;
      modal.classList.add("on");
      modal.focus({ preventScroll: true });
    };

    const onClick = (e: MouseEvent) => {
      const target = e.target as Element;
      if (target === modal) return closeZoom();
      const zbtn = target.closest(".bf .card.zoomable .zbtn");
      const zcard = target.closest<HTMLElement>(".bf .card.zoomable");
      if (zbtn && zcard) return zcard === zoomed ? closeZoom() : openZoom(zcard);
      if (zcard === zoomed && zoomed && target.closest("figcaption a")) return closeZoom(true);
      const trigger = target.closest(".bf [data-fk]");
      if (trigger) return toggleFilter(trigger);
      // A phone hides the zoom button, so a tap on the figure opens no zoom view there.
      if (
        zcard &&
        zcard !== zoomed &&
        target.closest(".card-body") &&
        !target.closest("button,a,.lev li,.seg") &&
        zcard.querySelector(".zbtn")?.getClientRects().length
      )
        return openZoom(zcard);
      const seg = target.closest<HTMLButtonElement>(".bf .arcv .seg button");
      if (seg) return pick(seg);
      const legend = target.closest<HTMLButtonElement>(".bf .arcv .legend button[data-h]");
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
      if (li && levers)
        focusLever(levers, levers.dataset.f === li.dataset.l ? "" : (li.dataset.l ?? ""));
    };
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as Element;
      if (e.key === "Escape" && zoomed) {
        e.preventDefault();
        return closeZoom();
      }
      // The dialog is modal, so Tab cycles through its own controls and never reaches the page behind it.
      if (e.key === "Tab" && zoomed) {
        const items = Array.from(
          modal.querySelectorAll<HTMLElement>("button,a[href],[tabindex]"),
        ).filter((el) => el.tabIndex >= 0 && el.getClientRects().length > 0);
        const first = items[0];
        const last = items[items.length - 1];
        if (!first || !last) {
          e.preventDefault();
          return modal.focus();
        }
        const active = document.activeElement;
        if (e.shiftKey && (active === first || active === modal || !modal.contains(active))) {
          e.preventDefault();
          return last.focus();
        }
        if (!e.shiftKey && (active === last || !modal.contains(active))) {
          e.preventDefault();
          return first.focus();
        }
        return;
      }
      const card = target.closest?.(".bf .card");
      if (e.key === "Escape" && card) {
        filters.set(card, {});
        syncFilters(card, {});
        clearLegend(card);
      }
      const trigger = target.closest?.(".bf [data-fk]");
      if (trigger && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        return toggleFilter(trigger);
      }
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
      const j = {
        ArrowRight: (i + 1) % n,
        ArrowDown: (i + 1) % n,
        ArrowLeft: (i + n - 1) % n,
        ArrowUp: (i + n - 1) % n,
        Home: 0,
        End: n - 1,
      }[e.key];
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
    window.addEventListener("resize", hideAtip, { passive: true });
    return () => {
      finishClose();
      io.disconnect();
      window.removeEventListener("resize", hideAtip);
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
      <div
        ref={zoomRef}
        className="bf-zmodal"
        role="dialog"
        aria-modal="true"
        aria-hidden="true"
        tabIndex={-1}
        lang="en"
        dir="ltr"
      >
        <div className="bf bf-zstage" />
      </div>
      <div ref={atipRef} className="bf-atip" aria-hidden="true">
        <span />
        <svg width="12" height="7" viewBox="0 0 12 7">
          <path d="M0 0 L6 6 L12 0" />
        </svg>
      </div>
      {/* The arrowhead that the figure lines share. */}
      <svg width="0" height="0" className="absolute" aria-hidden="true">
        <defs>
          <marker
            id="ah"
            viewBox="0 0 10 10"
            refX="8.5"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path
              d="M2 1.5L8.5 5L2 8.5"
              fill="none"
              stroke="context-stroke"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </marker>
        </defs>
      </svg>
    </>
  );
}
