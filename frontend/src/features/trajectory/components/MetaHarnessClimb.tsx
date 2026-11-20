"use client";

import { motion, useReducedMotion } from "motion/react";
import {
  ArrowCounterClockwise,
  ArrowsIn,
  ArrowsOut,
  Crosshair,
  Minus,
  Plus,
} from "@/shared/ui/icons";
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { createPortal } from "react-dom";
import { formatBlackboxScore } from "@/shared/lib";
import { formatMsg, msg } from "@/shared/lib/messages";
import { TERMS } from "@/shared/lib/terms";
import { cn } from "@/shared/lib/utils";
import {
  CanvasControlButton,
  CanvasControlDivider,
  CanvasControlGroup,
} from "@/shared/ui/canvas-control-button";
import {
  CLIMB_LAYOUT,
  layoutClimb,
  type ClimbLayout,
  type ClimbModel,
  type ClimbPoint,
} from "../lib/meta-harness";
import { usePinchZoom } from "../lib/pinch-zoom";
import { displayCandidateId } from "../lib/types";

// The palette mirrors TrajectoryTree so a climb and a tree read as one family.
const EDGE_STROKE = "color-mix(in srgb, var(--muted-foreground) 42%, transparent)";
const NODE_CORE_FILL = "var(--card)";
const NODE_CORE_STROKE = "color-mix(in srgb, var(--foreground) 16%, transparent)";
const NODE_CORE_STROKE_HOVER = "color-mix(in srgb, var(--foreground) 42%, transparent)";
const NODE_CORE_STROKE_SELECTED = "var(--foreground)";
const IMPROVED_FILL = "var(--tree-pass)";
const REGRESSED_FILL = "var(--tree-fail)";
const SCORE_TRACK_STROKE = "color-mix(in srgb, var(--muted-foreground) 14%, transparent)";
const PENDING_STROKE = "color-mix(in srgb, var(--muted-foreground) 60%, transparent)";
const PENDING_PROGRESS = "var(--primary)";
const WINNER_INDICATOR = "var(--tree-winner)";
const WINNER_HALO = "var(--tree-winner-halo)";
const WINNER_FILL = "var(--tree-winner-fill)";
const WINNER_BADGE_FILL = "var(--tree-winner)";
const WINNER_BADGE_INK = "var(--tree-winner-badge-ink)";
const LABEL_HALO = "color-mix(in srgb, var(--background) 90%, transparent)";
const GRID_LINE_COLOR = "var(--tree-grid)";
const AXIS_INK = "color-mix(in srgb, var(--foreground) 55%, transparent)";
const SURFACE_GRADIENT = "radial-gradient(ellipse at 50% 0%, var(--muted), var(--background) 70%)";
// Room under the plot for the legend that floats over the chart's bottom edge.
// A coarse pointer (any iPad, even with a trackpad attached) grows every legend
// toggle to a 44px target, so the legend needs more room there or it covers
// the axis labels.
const LEGEND_ROOM_PX = 48;
const LEGEND_ROOM_COARSE_PX = 68;
const COARSE_POINTER_QUERY = "(any-pointer: coarse)";
// Inline the chart is as tall as the climb's natural layout plus that room;
// maximized, the climb stretches to the screen instead.
const PLOT_HEIGHT_PX = CLIMB_LAYOUT.padTop + CLIMB_LAYOUT.plotHeight + CLIMB_LAYOUT.padBottom;
/** The inline chart's height, for skeletons that stand in for it. */
export const CLIMB_CHART_HEIGHT_PX = {
  fine: PLOT_HEIGHT_PX + LEGEND_ROOM_PX,
  coarse: PLOT_HEIGHT_PX + LEGEND_ROOM_COARSE_PX,
} as const;

function subscribeCoarsePointer(onChange: () => void): () => void {
  const query = window.matchMedia(COARSE_POINTER_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function useLegendRoom(): number {
  const coarse = useSyncExternalStore(
    subscribeCoarsePointer,
    () => window.matchMedia(COARSE_POINTER_QUERY).matches,
    () => false,
  );
  return coarse ? LEGEND_ROOM_COARSE_PX : LEGEND_ROOM_PX;
}

// The map controls mirror TrajectoryTree, except that zooming out stops where
// the whole climb is in view rather than at a fixed floor.
const ZOOM_MAX = 6;
const ZOOM_WHEEL_FACTOR = 0.0015;
const ZOOM_BUTTON_IN = 1.25;
const ZOOM_BUTTON_OUT = 0.8;
const DRAG_THRESHOLD_PX = 4;

const RING_R = CLIMB_LAYOUT.nodeRadius - CLIMB_LAYOUT.ringThickness / 2;
const INNER_R = CLIMB_LAYOUT.nodeRadius - CLIMB_LAYOUT.ringThickness;

interface LayerVisibility {
  improved: boolean;
  regressed: boolean;
  lineage: boolean;
  winner: boolean;
}

interface View {
  k: number;
  tx: number;
  ty: number;
}

interface PanState {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  startTx: number;
  startTy: number;
  moved: boolean;
}

export interface MetaHarnessClimbProps {
  model: ClimbModel;
  live: boolean;
  selectedId: string | null;
  newestId: string | null;
  onSelect: (id: string) => void;
}

// At rest the climb sits at its natural size against the start edge, shrunk
// only when it would not fit; the viewer zooms in and pans from there. ``plot``
// is the canvas above the legend's room.
function fitView(plot: { w: number; h: number }, layoutW: number, layoutH: number): View {
  if (plot.w < 2 || plot.h < 2 || layoutW <= 0 || layoutH <= 0) {
    return { k: 1, tx: 0, ty: 0 };
  }
  const k = Math.min(1, plot.w / layoutW, plot.h / layoutH);
  return { k, tx: 0, ty: Math.max(0, (plot.h - layoutH * k) / 2) };
}

// The resting frame is the zoom floor: the climb never shrinks below the size
// at which all of it is in view, and at that size the frame is fully decided.
// Above it, a side that overflows the viewport pans within its overflow while
// a side that fits stays where the resting frame puts it, so bare canvas never
// opens up beside the drawing.
function clampView(
  view: View,
  plot: { w: number; h: number },
  layoutW: number,
  layoutH: number,
): View {
  const rest = fitView(plot, layoutW, layoutH);
  if (view.k <= rest.k) return rest;
  const k = Math.min(ZOOM_MAX, view.k);
  const slackX = plot.w - layoutW * k;
  const slackY = plot.h - layoutH * k;
  return {
    k,
    tx: slackX >= 0 ? 0 : Math.max(slackX, Math.min(0, view.tx)),
    ty: slackY >= 0 ? slackY / 2 : Math.max(slackY, Math.min(0, view.ty)),
  };
}

// Whether the legend has this version's kind of outcome switched on.
function versionShown(point: ClimbPoint, layers: LayerVisibility): boolean {
  return point.version.improved ? layers.improved : layers.regressed;
}

function edgePath(from: { x: number; y: number }, to: { x: number; y: number }): string {
  const midX = (from.x + to.x) / 2;
  return `M ${from.x} ${from.y} C ${midX} ${from.y}, ${midX} ${to.y}, ${to.x} ${to.y}`;
}

function ringFraction(score: number, domain: ClimbLayout["domain"]): number {
  const t = domain.unit ? score : (score - domain.min) / (domain.max - domain.min);
  return Math.max(0, Math.min(1, t));
}

// A stroked circle normalised to one unit of path length: the dash offset
// rotates the arc to start at 12 o'clock (a circle's path begins at 3 o'clock).
function arc(cx: number, cy: number, r: number, width: number, color: string, fraction: number) {
  if (fraction <= 0) return null;
  const full = fraction >= 1;
  return (
    <circle
      cx={cx}
      cy={cy}
      r={r}
      fill="none"
      stroke={color}
      strokeWidth={width}
      pathLength={1}
      strokeDasharray={full ? undefined : `${fraction} ${1 - fraction}`}
      strokeDashoffset={full ? undefined : -0.75}
    />
  );
}

/**
 * The meta-harness run as a climb: versions left to right in the order they
 * were scored, height by score, the best so far drawn as a staircase and each
 * version tied to the version it was rewritten from. The chart pans and zooms
 * like the candidate tree, and every legend entry filters what it shows.
 */
export function MetaHarnessClimb({
  model,
  live,
  selectedId,
  newestId,
  onSelect,
}: MetaHarnessClimbProps) {
  const reduceMotion = useReducedMotion() === true;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [view, setView] = useState<View>({ k: 1, tx: 0, ty: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [isMaximized, setIsMaximized] = useState(false);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [layers, setLayers] = useState<LayerVisibility>({
    improved: true,
    regressed: true,
    lineage: true,
    winner: true,
  });
  const panStateRef = useRef<PanState | null>(null);
  // Auto-fit keeps framing the climb as versions stream in until the viewer
  // pans or zooms; reset and the maximize toggle release the lock.
  const userInteractedRef = useRef(false);
  const toggleLayer = useCallback((key: keyof LayerVisibility) => {
    setLayers((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // Seed the size synchronously so the first paint after a portal move has
    // the new container's dimensions rather than one stale frame.
    const rect = el.getBoundingClientRect();
    setSize({ w: rect.width, h: rect.height });
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setSize({ w: entry.contentRect.width, h: entry.contentRect.height });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
    // The maximize toggle moves the container through a portal, so the
    // observer has to re-attach to the new host element.
  }, [isMaximized]);

  useEffect(() => {
    userInteractedRef.current = false;
  }, [isMaximized]);

  useEffect(() => {
    if (!isMaximized) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsMaximized(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [isMaximized]);

  const legendRoom = useLegendRoom();
  const plot = useMemo(() => ({ w: size.w, h: size.h - legendRoom }), [size, legendRoom]);
  const layout = useMemo(
    () =>
      layoutClimb(model, {
        availableWidth: plot.w,
        availableHeight: isMaximized ? plot.h : undefined,
        showPending: live,
      }),
    [model, plot, isMaximized, live],
  );
  const bestPoint = useMemo(
    () => layout.points.find((point) => point.id === model.bestId) ?? null,
    [layout, model.bestId],
  );

  useEffect(() => {
    if (size.w < 2 || size.h < 2) return;
    setView((v) =>
      userInteractedRef.current
        ? clampView(v, plot, layout.width, layout.height)
        : fitView(plot, layout.width, layout.height),
    );
  }, [size, plot, layout.width, layout.height]);

  const zoomAt = useCallback(
    (cx: number, cy: number, factor: number) => {
      setView((v) => {
        const rest = fitView(plot, layout.width, layout.height);
        const nextK = Math.max(rest.k, Math.min(ZOOM_MAX, v.k * factor));
        if (nextK === v.k) return v;
        // Zooming all the way out lands on the resting frame, which then keeps
        // following new versions as if the view had been reset.
        if (nextK === rest.k) {
          userInteractedRef.current = false;
          return rest;
        }
        userInteractedRef.current = true;
        const wx = (cx - v.tx) / v.k;
        const wy = (cy - v.ty) / v.k;
        return clampView(
          { k: nextK, tx: cx - wx * nextK, ty: cy - wy * nextK },
          plot,
          layout.width,
          layout.height,
        );
      });
    },
    [plot, layout.width, layout.height],
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = container.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, Math.exp(-e.deltaY * ZOOM_WHEEL_FACTOR));
    };
    container.addEventListener("wheel", onWheel, { passive: false });
    return () => container.removeEventListener("wheel", onWheel);
    // Same portal caveat as the ResizeObserver: the listener follows the host.
  }, [zoomAt, isMaximized]);

  const { start: pinchStart, move: pinchMove, end: pinchEnd } = usePinchZoom(containerRef, zoomAt);

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const target = e.target as Element | null;
      if (target?.closest("[data-trajectory-controls]")) return;
      if (pinchStart(e)) {
        if (panStateRef.current?.moved) setIsDragging(false);
        panStateRef.current = null;
        return;
      }
      // Capturing here would steal the click from the node under the pointer,
      // so capture waits until the pointer actually starts panning.
      panStateRef.current = {
        pointerId: e.pointerId,
        startClientX: e.clientX,
        startClientY: e.clientY,
        startTx: view.tx,
        startTy: view.ty,
        moved: false,
      };
    },
    [view.tx, view.ty, pinchStart],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (pinchMove(e)) return;
      const ps = panStateRef.current;
      if (!ps || ps.pointerId !== e.pointerId) return;
      const dx = e.clientX - ps.startClientX;
      const dy = e.clientY - ps.startClientY;
      if (!ps.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD_PX) {
        ps.moved = true;
        userInteractedRef.current = true;
        setIsDragging(true);
        (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
      }
      if (ps.moved) {
        setView((v) =>
          clampView(
            { k: v.k, tx: ps.startTx + dx, ty: ps.startTy + dy },
            plot,
            layout.width,
            layout.height,
          ),
        );
      }
    },
    [plot, layout.width, layout.height, pinchMove],
  );

  const handlePointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      pinchEnd(e);
      const ps = panStateRef.current;
      if (!ps || ps.pointerId !== e.pointerId) return;
      panStateRef.current = null;
      if (ps.moved) {
        (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
        setIsDragging(false);
      }
    },
    [pinchEnd],
  );

  // A release that ends a pan must not also select the node it lands on.
  const handleSelect = useCallback(
    (id: string) => {
      if (panStateRef.current?.moved) return;
      onSelect(id);
    },
    [onSelect],
  );

  const zoomFromCenter = useCallback(
    (factor: number) => zoomAt(size.w / 2, size.h / 2, factor),
    [size.w, size.h, zoomAt],
  );
  const restView = useMemo(
    () => fitView(plot, layout.width, layout.height),
    [plot, layout.width, layout.height],
  );
  const resetView = useCallback(() => {
    userInteractedRef.current = false;
    setView(restView);
  }, [restView]);
  const isTransformed = view.k !== restView.k || view.tx !== restView.tx || view.ty !== restView.ty;
  const atZoomFloor = view.k <= restView.k;

  const transform = `translate(${view.tx}, ${view.ty}) scale(${view.k})`;
  // Gridlines run past the last version to the edge of whatever is in view.
  const gridEnd = Math.max(layout.width, (size.w - view.tx) / view.k);

  const body = (
    <div
      ref={containerRef}
      className={
        isMaximized
          ? "fixed inset-0 z-50 h-dvh w-screen overflow-hidden border-0"
          : "relative w-full overflow-hidden rounded-xl border border-border/60"
      }
      style={
        isMaximized
          ? { background: SURFACE_GRADIENT }
          : { height: PLOT_HEIGHT_PX + legendRoom, background: SURFACE_GRADIENT }
      }
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
    >
      <svg
        width="100%"
        height="100%"
        viewBox={`0 0 ${Math.max(size.w, 1)} ${Math.max(size.h, 1)}`}
        preserveAspectRatio="xMidYMid meet"
        role="group"
        aria-label={msg("meta_harness.a11y.chart_label")}
        className="block select-none"
        style={{
          direction: "ltr",
          cursor: isDragging ? "grabbing" : "grab",
          touchAction: "none",
        }}
      >
        <g transform={transform}>
          <g aria-hidden="true">
            {layout.ticks.map((tick) => (
              <g key={tick.value}>
                <line
                  x1={CLIMB_LAYOUT.padStart - CLIMB_LAYOUT.nodeRadius - 6}
                  x2={gridEnd}
                  y1={tick.y}
                  y2={tick.y}
                  stroke={GRID_LINE_COLOR}
                  strokeWidth={1}
                />
                <text
                  x={CLIMB_LAYOUT.padStart - CLIMB_LAYOUT.nodeRadius - 10}
                  y={tick.y + 3}
                  textAnchor="end"
                  fontSize={10}
                  fill={AXIS_INK}
                  style={{ fontVariantNumeric: "tabular-nums" }}
                >
                  {formatBlackboxScore(tick.value)}
                </text>
              </g>
            ))}
            <text
              x={CLIMB_LAYOUT.padStart - CLIMB_LAYOUT.nodeRadius - 6}
              y={CLIMB_LAYOUT.padTop - 18}
              fontSize={10}
              fontWeight={600}
              fill={AXIS_INK}
              style={{ letterSpacing: "0.08em", textTransform: "uppercase" }}
            >
              {msg("meta_harness.axis.score")}
            </text>
            <text
              x={CLIMB_LAYOUT.padStart - CLIMB_LAYOUT.nodeRadius - 6}
              y={layout.height - 10}
              fontSize={10}
              fontWeight={600}
              fill={AXIS_INK}
              style={{ letterSpacing: "0.08em", textTransform: "uppercase" }}
            >
              {msg("meta_harness.axis.version")} →
            </text>
          </g>
          <ClimbContent
            layout={layout}
            model={model}
            bestPoint={bestPoint}
            layers={layers}
            selectedId={selectedId}
            hoveredId={hoveredId}
            newestId={newestId}
            reduceMotion={reduceMotion}
            onSelect={handleSelect}
            onHover={setHoveredId}
          />
        </g>
      </svg>

      <CanvasControlGroup data-trajectory-controls className="absolute end-3 top-3 z-10">
        <CanvasControlButton
          icon={Plus}
          label={msg("trajectory.controls.zoom_in")}
          onClick={() => zoomFromCenter(ZOOM_BUTTON_IN)}
        />
        <CanvasControlButton
          icon={Minus}
          label={msg("trajectory.controls.zoom_out")}
          onClick={() => zoomFromCenter(ZOOM_BUTTON_OUT)}
          disabled={atZoomFloor}
        />
        <CanvasControlButton
          icon={isTransformed ? ArrowCounterClockwise : Crosshair}
          label={msg("trajectory.controls.zoom_reset")}
          onClick={resetView}
        />
        <CanvasControlDivider />
        <CanvasControlButton
          icon={isMaximized ? ArrowsIn : ArrowsOut}
          label={
            isMaximized
              ? msg("trajectory.controls.fullscreen_exit")
              : msg("trajectory.controls.fullscreen_enter")
          }
          onClick={() => setIsMaximized((prev) => !prev)}
        />
      </CanvasControlGroup>

      <div className="pointer-events-none absolute inset-x-0 bottom-0 z-10 flex justify-center px-3 pb-3">
        <div className="pointer-events-auto flex max-w-full flex-wrap items-center justify-center gap-x-3 gap-y-1.5 rounded-full border border-border/50 bg-background/80 px-3 py-1 text-[0.6875rem] text-muted-foreground/80 backdrop-blur">
          <LegendToggle
            pressed={layers.improved}
            onToggle={() => toggleLayer("improved")}
            swatch={<RingSwatch color={IMPROVED_FILL} />}
            label={msg("meta_harness.legend.improved")}
          />
          <LegendToggle
            pressed={layers.regressed}
            onToggle={() => toggleLayer("regressed")}
            swatch={<RingSwatch color={REGRESSED_FILL} />}
            label={msg("meta_harness.legend.regressed")}
          />
          <LegendDivider />
          <LegendToggle
            pressed={layers.lineage}
            onToggle={() => toggleLayer("lineage")}
            swatch={
              <svg className="h-2.5 w-4" viewBox="0 0 16 10" aria-hidden="true">
                <path
                  d="M 1 8 C 8 8, 8 2, 15 2"
                  fill="none"
                  stroke={EDGE_STROKE}
                  strokeWidth="1.6"
                />
              </svg>
            }
            label={msg("meta_harness.legend.lineage")}
          />
          <LegendDivider />
          <LegendToggle
            pressed={layers.winner}
            onToggle={() => toggleLayer("winner")}
            swatch={
              <span
                className="inline-block h-2.5 w-4 rounded-[2px]"
                style={{ background: WINNER_BADGE_FILL }}
              />
            }
            label={TERMS.winningCandidate}
          />
        </div>
      </div>
    </div>
  );

  if (isMaximized && typeof document !== "undefined") {
    return (
      <>
        <div
          aria-hidden
          className="w-full rounded-xl border border-border/60 opacity-40"
          style={{ height: PLOT_HEIGHT_PX + legendRoom, background: SURFACE_GRADIENT }}
        />
        {createPortal(body, document.body)}
      </>
    );
  }
  return body;
}

interface ClimbContentProps {
  layout: ClimbLayout;
  model: ClimbModel;
  bestPoint: ClimbPoint | null;
  layers: LayerVisibility;
  selectedId: string | null;
  hoveredId: string | null;
  newestId: string | null;
  reduceMotion: boolean;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
}

const ClimbContent = memo(function ClimbContent({
  layout,
  model,
  bestPoint,
  layers,
  selectedId,
  hoveredId,
  newestId,
  reduceMotion,
  onSelect,
  onHover,
}: ClimbContentProps) {
  const R = CLIMB_LAYOUT.nodeRadius;
  const pending = layout.pending;
  const pendingDone = model.pending?.scores.size ?? 0;
  const pendingTotal = model.pending?.total ?? 0;
  // The version being scored answers to its trial number, like the finished
  // ones answer to their candidate id, so it selects the same way.
  const pendingId = model.pending === null ? null : String(model.pending.index);
  const pendingSelected = pendingId !== null && pendingId === selectedId;
  const pendingHovered = pendingId !== null && pendingId === hoveredId;
  const activate = (id: string) => (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(id);
    }
  };

  return (
    <>
      <LayerFade show={layers.lineage} reduceMotion={reduceMotion}>
        {layout.edges.map((edge) => (
          <LayerFade
            key={`edge-${edge.to.id}`}
            show={versionShown(edge.from, layers) && versionShown(edge.to, layers)}
            reduceMotion={reduceMotion}
          >
            <motion.path
              d={edgePath(edge.from, edge.to)}
              fill="none"
              stroke={EDGE_STROKE}
              strokeWidth={1.4}
              initial={reduceMotion ? false : { pathLength: 0, opacity: 0 }}
              animate={reduceMotion ? undefined : { pathLength: 1, opacity: 1 }}
              transition={reduceMotion ? undefined : { duration: 0.5, ease: "easeOut" }}
            />
          </LayerFade>
        ))}
        {pending !== null && bestPoint !== null ? (
          <LayerFade show={versionShown(bestPoint, layers)} reduceMotion={reduceMotion}>
            <path
              d={edgePath(bestPoint, pending)}
              fill="none"
              stroke={EDGE_STROKE}
              strokeWidth={1.2}
              strokeDasharray="3 3"
            />
          </LayerFade>
        ) : null}
      </LayerFade>

      <g>
        {layout.points.map((point) => {
          const { version } = point;
          const isSelected = point.id === selectedId;
          const isHovered = point.id === hoveredId;
          const isNewest = point.id === newestId;
          const isWinner = point.id === model.bestId;
          const winnerShown = isWinner && layers.winner;
          // The seed is the baseline the climb starts from, not a version the
          // harness wrote, so it keeps its ring but no disc or id label.
          const isSeed = version.candidate.parent_id === null;
          const shown = versionShown(point, layers);
          const coreStroke = isSelected
            ? NODE_CORE_STROKE_SELECTED
            : isHovered
              ? NODE_CORE_STROKE_HOVER
              : NODE_CORE_STROKE;
          return (
            <LayerFade key={point.id} show={shown} reduceMotion={reduceMotion}>
              <motion.g
                role="button"
                aria-label={formatMsg("meta_harness.a11y.node_label", {
                  id: displayCandidateId(point.id),
                  score: formatBlackboxScore(version.score),
                })}
                aria-pressed={isSelected}
                tabIndex={shown ? 0 : -1}
                onMouseEnter={() => onHover(point.id)}
                onMouseLeave={() => onHover(null)}
                onClick={() => onSelect(point.id)}
                onKeyDown={activate(point.id)}
                initial={reduceMotion ? false : { scale: isNewest ? 0 : 0.7, opacity: 0 }}
                animate={reduceMotion ? undefined : { scale: 1, opacity: 1 }}
                transition={reduceMotion ? undefined : { duration: 0.45, ease: [0.2, 0.8, 0.2, 1] }}
                style={{ cursor: "pointer", transformOrigin: `${point.x}px ${point.y}px` }}
              >
                {isNewest && !reduceMotion ? (
                  <motion.circle
                    cx={point.x}
                    cy={point.y}
                    r={R}
                    fill="none"
                    stroke={WINNER_INDICATOR}
                    strokeWidth="1.6"
                    initial={{ r: R, opacity: 0.6 }}
                    animate={{ r: R * 2.2, opacity: 0 }}
                    transition={{ duration: 1.2, ease: "easeOut", repeat: 2 }}
                  />
                ) : null}
                {isSelected ? (
                  <circle
                    cx={point.x}
                    cy={point.y}
                    r={R + 5}
                    fill="none"
                    stroke={NODE_CORE_STROKE_SELECTED}
                    strokeWidth={1.5}
                    strokeOpacity={0.7}
                  />
                ) : null}
                {isWinner ? (
                  <LayerFade show={layers.winner} reduceMotion={reduceMotion}>
                    <circle
                      cx={point.x}
                      cy={point.y}
                      r={R + 11}
                      fill="none"
                      stroke={WINNER_HALO}
                      strokeWidth={3}
                    />
                    {reduceMotion ? null : (
                      <motion.circle
                        cx={point.x}
                        cy={point.y}
                        r={R + 4}
                        fill="none"
                        stroke={WINNER_INDICATOR}
                        strokeWidth={1.4}
                        initial={{ opacity: 0.5, r: R + 4 }}
                        animate={{ opacity: [0.5, 0.12, 0.5], r: [R + 4, R + 10, R + 4] }}
                        transition={{ duration: 3.2, ease: "easeInOut", repeat: Infinity }}
                      />
                    )}
                    <circle
                      cx={point.x}
                      cy={point.y}
                      r={R + 4}
                      fill="none"
                      stroke={WINNER_INDICATOR}
                      strokeWidth={2.6}
                    />
                  </LayerFade>
                ) : null}
                <circle
                  cx={point.x}
                  cy={point.y}
                  r={RING_R}
                  fill="none"
                  stroke={SCORE_TRACK_STROKE}
                  strokeWidth={CLIMB_LAYOUT.ringThickness}
                />
                {arc(
                  point.x,
                  point.y,
                  RING_R,
                  CLIMB_LAYOUT.ringThickness,
                  version.improved ? IMPROVED_FILL : REGRESSED_FILL,
                  ringFraction(version.score, layout.domain),
                )}
                {isSeed ? null : (
                  <>
                    <circle
                      cx={point.x}
                      cy={point.y}
                      r={INNER_R}
                      fill={winnerShown ? WINNER_FILL : NODE_CORE_FILL}
                      stroke={coreStroke}
                      strokeWidth={isSelected ? 1.4 : 0.8}
                      style={{
                        transition: "fill 250ms ease, stroke 120ms ease, stroke-width 120ms ease",
                      }}
                    />
                    <text
                      x={point.x}
                      y={point.y + 4.5}
                      textAnchor="middle"
                      fontFamily="var(--font-mono, monospace)"
                      fontSize="13"
                      fontWeight={700}
                      fill={winnerShown ? "var(--tree-winner-number)" : "var(--foreground)"}
                      pointerEvents="none"
                    >
                      {displayCandidateId(point.id)}
                    </text>
                  </>
                )}
                {isWinner ? (
                  <LayerFade show={layers.winner} reduceMotion={reduceMotion}>
                    <WinnerBadge x={point.x} y={point.y + R + 4} />
                  </LayerFade>
                ) : null}
                <motion.g
                  initial={false}
                  animate={{ y: isWinner && !layers.winner ? -20 : 0 }}
                  transition={reduceMotion ? { duration: 0 } : { duration: 0.25, ease: "easeOut" }}
                >
                  <text
                    x={point.x}
                    y={point.y + R + (isWinner ? 34 : 14)}
                    textAnchor="middle"
                    fontFamily="var(--font-mono, monospace)"
                    fontSize="10.5"
                    fontWeight={600}
                    fill="color-mix(in srgb, var(--foreground) 72%, transparent)"
                    stroke={LABEL_HALO}
                    strokeWidth={2.5}
                    strokeLinejoin="round"
                    pointerEvents="none"
                    style={{ fontVariantNumeric: "tabular-nums", paintOrder: "stroke" }}
                  >
                    {formatBlackboxScore(version.score)}
                  </text>
                </motion.g>
              </motion.g>
            </LayerFade>
          );
        })}

        {pending !== null && model.pending !== null && pendingId !== null ? (
          <motion.g
            key={`pending-${model.pending.index}`}
            role="button"
            aria-label={formatMsg("meta_harness.a11y.pending_label", {
              id: displayCandidateId(String(model.pending.index)),
              done: pendingDone,
              total: pendingTotal,
            })}
            aria-pressed={pendingSelected}
            tabIndex={0}
            onMouseEnter={() => onHover(pendingId)}
            onMouseLeave={() => onHover(null)}
            onClick={() => onSelect(pendingId)}
            onKeyDown={activate(pendingId)}
            initial={reduceMotion ? false : { opacity: 0, y: pending.y }}
            animate={{ opacity: 1, y: pending.y }}
            transition={reduceMotion ? { duration: 0 } : { duration: 0.6, ease: "easeOut" }}
            style={{ cursor: "pointer" }}
          >
            {pendingSelected ? (
              <circle
                cx={pending.x}
                cy={0}
                r={R + 9}
                fill="none"
                stroke={NODE_CORE_STROKE_SELECTED}
                strokeWidth={1.5}
                strokeOpacity={0.7}
              />
            ) : null}
            {reduceMotion ? (
              <circle
                cx={pending.x}
                cy={0}
                r={R + 4}
                fill="none"
                stroke={PENDING_STROKE}
                strokeWidth={1.2}
                strokeDasharray="4 4"
              />
            ) : (
              <motion.circle
                cx={pending.x}
                cy={0}
                r={R + 4}
                fill="none"
                stroke={PENDING_STROKE}
                strokeWidth={1.2}
                pathLength={1}
                strokeDasharray="0.08 0.06"
                animate={{ strokeDashoffset: [0, -1] }}
                transition={{ duration: 6, ease: "linear", repeat: Infinity }}
              />
            )}
            <circle
              cx={pending.x}
              cy={0}
              r={RING_R}
              fill="none"
              stroke={SCORE_TRACK_STROKE}
              strokeWidth={CLIMB_LAYOUT.ringThickness}
            />
            {arc(
              pending.x,
              0,
              RING_R,
              CLIMB_LAYOUT.ringThickness,
              PENDING_PROGRESS,
              pendingTotal > 0 ? pendingDone / pendingTotal : 0,
            )}
            <circle
              cx={pending.x}
              cy={0}
              r={INNER_R}
              fill={NODE_CORE_FILL}
              stroke={
                pendingSelected
                  ? NODE_CORE_STROKE_SELECTED
                  : pendingHovered
                    ? NODE_CORE_STROKE_HOVER
                    : NODE_CORE_STROKE
              }
              strokeWidth={pendingSelected ? 1.4 : 0.8}
            />
            <text
              x={pending.x}
              y={4}
              textAnchor="middle"
              fontFamily="var(--font-mono, monospace)"
              fontSize="10.5"
              fontWeight={700}
              fill="var(--foreground)"
              pointerEvents="none"
              style={{ fontVariantNumeric: "tabular-nums" }}
            >
              {pendingDone}/{pendingTotal}
            </text>
            <text
              x={pending.x}
              y={R + 14}
              textAnchor="middle"
              fontFamily="var(--font-mono, monospace)"
              fontSize="10.5"
              fontWeight={600}
              fill="color-mix(in srgb, var(--foreground) 72%, transparent)"
              stroke={LABEL_HALO}
              strokeWidth={2.5}
              strokeLinejoin="round"
              pointerEvents="none"
              style={{ paintOrder: "stroke" }}
            >
              {displayCandidateId(String(model.pending.index))}
            </text>
          </motion.g>
        ) : null}
      </g>
    </>
  );
});

function LayerFade({
  show,
  reduceMotion,
  children,
}: {
  show: boolean;
  reduceMotion: boolean;
  children: React.ReactNode;
}) {
  return (
    <motion.g
      initial={false}
      animate={{ opacity: show ? 1 : 0 }}
      transition={reduceMotion ? { duration: 0 } : { duration: 0.25, ease: "easeOut" }}
      style={{ pointerEvents: show ? "auto" : "none" }}
    >
      {children}
    </motion.g>
  );
}

function WinnerBadge({ x, y }: { x: number; y: number }) {
  const label = msg("trajectory.node.winning_label");
  // SVG text can't auto-size its background rect, so estimate the label's
  // width from its glyph count (~5.6px per glyph at 9.5px bold Heebo).
  const w = Math.round(label.length * 5.6) + 14;
  const h = 16;
  const top = y + 4;
  return (
    <g pointerEvents="none">
      <rect x={x - w / 2} y={top} width={w} height={h} rx={3} ry={3} fill={WINNER_BADGE_FILL} />
      <text
        x={x}
        y={top + h / 2 + 3.4}
        textAnchor="middle"
        fontFamily='"Heebo", "Assistant", system-ui, sans-serif'
        fontSize="9.5"
        fontWeight={700}
        letterSpacing="0.4"
        fill={WINNER_BADGE_INK}
      >
        {label}
      </text>
    </g>
  );
}

function RingSwatch({ color }: { color: string }) {
  return (
    <svg className="size-2.5" viewBox="0 0 10 10" aria-hidden="true">
      <circle cx="5" cy="5" r="3.5" fill="none" stroke={SCORE_TRACK_STROKE} strokeWidth="3" />
      <circle
        cx="5"
        cy="5"
        r="3.5"
        fill="none"
        stroke={color}
        strokeWidth="3"
        pathLength={1}
        strokeDasharray="0.65 0.35"
        strokeDashoffset={-0.75}
      />
    </svg>
  );
}

function LegendToggle({
  swatch,
  label,
  pressed,
  onToggle,
}: {
  swatch: React.ReactNode;
  label: string;
  pressed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onToggle}
      className={cn(
        "inline-flex cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-md px-1.5 py-0.5 transition-[opacity,color,background-color] duration-150 hover:bg-accent/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
        !pressed && "opacity-40",
      )}
    >
      {swatch}
      <span>{label}</span>
    </button>
  );
}

function LegendDivider() {
  return <span aria-hidden="true" className="inline-block h-3 w-px bg-border/70" />;
}
