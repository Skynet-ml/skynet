"use client";

import { animate, motion, useReducedMotion } from "framer-motion";
import {
  ArrowCounterClockwise,
  ArrowsIn,
  ArrowsOut,
  Crosshair,
  Minus,
  Plus,
} from "@/shared/ui/icons";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  collapseGhosts,
  interpolateLayout,
  TRAJECTORY_LAYOUT,
  type LayoutResult,
} from "../lib/layout";
import { usePinchZoom } from "../lib/pinch-zoom";
import { displayCandidateId, type RejectedNode, type TrajectoryNode } from "../lib/types";
import { formatMsg, msg } from "@/shared/lib/messages";
import { TERMS } from "@/shared/lib/terms";
import { cn } from "@/shared/lib/utils";
import {
  CanvasControlButton,
  CanvasControlDivider,
  CanvasControlGroup,
} from "@/shared/ui/canvas-control-button";

const EDGE_STROKE = "color-mix(in srgb, var(--muted-foreground) 42%, transparent)";
const EDGE_STROKE_MERGE = "color-mix(in srgb, var(--muted-foreground) 30%, transparent)";
const EDGE_STROKE_GHOST = "color-mix(in srgb, var(--muted-foreground) 28%, transparent)";
const NODE_CORE_FILL = "var(--card)";
const NODE_CORE_STROKE = "color-mix(in srgb, var(--foreground) 16%, transparent)";
const NODE_CORE_STROKE_HOVER = "color-mix(in srgb, var(--foreground) 42%, transparent)";
const NODE_CORE_STROKE_SELECTED = "var(--foreground)";
const DONUT_PASS_FILL = "var(--tree-pass)";
const DONUT_RING_THICKNESS = 8;
// Track under the black-box score arc — same brown as the lineage edges.
const SCORE_TRACK_STROKE = "color-mix(in srgb, var(--muted-foreground) 14%, transparent)";
const GHOST_FILL = "var(--tree-ghost-fill)";
const GHOST_STROKE = "color-mix(in srgb, var(--muted-foreground) 50%, transparent)";
const GHOST_STROKE_HOVER = "color-mix(in srgb, var(--muted-foreground) 85%, transparent)";
const WINNER_INDICATOR = "var(--tree-winner)";
const WINNER_HALO = "var(--tree-winner-halo)";
const WINNER_FILL = "var(--tree-winner-fill)";
const WINNER_BADGE_FILL = "var(--tree-winner)";
const WINNER_BADGE_INK = "var(--tree-winner-badge-ink)";
// Painted behind score labels (paint-order: stroke) so they stay legible
// where a lineage edge or grid line passes underneath.
const LABEL_HALO = "color-mix(in srgb, var(--background) 90%, transparent)";

const ZOOM_MIN = 0.4;
const ZOOM_MAX = 6;
const ZOOM_WHEEL_FACTOR = 0.0015;
const ZOOM_BUTTON_IN = 1.25;
const ZOOM_BUTTON_OUT = 0.8;
const DRAG_THRESHOLD_PX = 4;
const FIT_PADDING_PX = 32;
const CONTAINER_HEIGHT_PX = 560;
// 44px grid step, 48px padding from edges before the axes start. Kept local
// rather than shared to avoid cross-feature coupling for two tiny constants.
const GRID_STEP = 44;
const GRID_LINE_COLOR = "var(--tree-grid)";
// Direct color values — using `hsl(var(--muted))` is invalid because the
// CSS variables hold hex colors, not h s l components, so the gradient gets
// silently dropped and the maximized overlay becomes transparent.
const SURFACE_GRADIENT =
  "radial-gradient(circle at 50% 42%, var(--muted) 0%, var(--background) 58%)";
interface View {
  k: number;
  tx: number;
  ty: number;
}

// Which visual layers of the tree are currently shown; every legend entry
// doubles as the toggle for its layer.
interface LayerVisibility {
  score: boolean;
  winner: boolean;
  rejected: boolean;
}

export interface TrajectoryTreeProps {
  layout: LayoutResult;
  // The same tree laid out without its rejected proposals. Hiding them swaps
  // to this layout so their slots close up instead of leaving holes.
  layoutWithoutRejected?: LayoutResult;
  selectedId: string | null;
  newestId: string | null;
  onSelectCandidate: (id: string) => void;
  onSelectRejected: (rejectionId: string) => void;
}

function clampScale(k: number): number {
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, k));
}

// The score ring is a stroked circle: `pathLength` normalizes the
// circumference to `total` units so each segment spans exactly its fraction,
// and the dash offset rotates the arc to start at 12 o'clock (a circle's
// path begins at 3 o'clock). A full ring is just an undashed stroke — no
// closed-arc special case needed.
const RING_R = TRAJECTORY_LAYOUT.nodeRadius - DONUT_RING_THICKNESS / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_R;
const RING_SEG_GAP_PX = 1.2;

function ringSegment(
  node: TrajectoryNode,
  key: string,
  fill: string,
  startUnit: number,
  lengthUnits: number,
  total: number,
): React.ReactNode {
  const gapUnits = (RING_SEG_GAP_PX / RING_CIRCUMFERENCE) * total;
  const full = lengthUnits >= total;
  const dashLen = Math.max(0.05, lengthUnits - gapUnits);
  return (
    <circle
      key={key}
      cx={node.x}
      cy={node.y}
      r={RING_R}
      fill="none"
      stroke={fill}
      strokeWidth={DONUT_RING_THICKNESS}
      pathLength={total}
      strokeDasharray={full ? undefined : `${dashLen} ${total - dashLen}`}
      strokeDashoffset={full ? undefined : -(0.75 * total + startUnit)}
    />
  );
}

// Filter layers stay mounted and cross-fade on legend toggles, so switching
// a layer reads as a dissolve rather than a pop. Pointer events are cut
// while hidden so an invisible layer can't swallow clicks.
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
      aria-hidden={!show}
    >
      {children}
    </motion.g>
  );
}

// A scorer has no pass/fail verdict, so the ring is one arc on a
// faint track that fills to the candidate's aggregate score.
function renderScoreRing(
  node: TrajectoryNode,
  fraction: number,
  show: boolean,
  reduceMotion: boolean,
): React.ReactNode {
  const clamped = Math.max(0, Math.min(1, fraction));
  return (
    <LayerFade show={show} reduceMotion={reduceMotion}>
      <circle
        cx={node.x}
        cy={node.y}
        r={RING_R}
        fill="none"
        stroke={SCORE_TRACK_STROKE}
        strokeWidth={DONUT_RING_THICKNESS}
      />
      {clamped > 0 ? ringSegment(node, "score", DONUT_PASS_FILL, 0, clamped, 1) : null}
    </LayerFade>
  );
}

function fitView(size: { w: number; h: number }, layoutW: number, layoutH: number): View {
  if (size.w < 2 || size.h < 2 || layoutW <= 0 || layoutH <= 0) {
    return { k: 1, tx: 0, ty: 0 };
  }
  const padded = FIT_PADDING_PX * 2;
  const k = clampScale(Math.min((size.w - padded) / layoutW, (size.h - padded) / layoutH));
  const tx = (size.w - layoutW * k) / 2;
  const ty = (size.h - layoutH * k) / 2;
  return { k, tx, ty };
}

const MORPH_TRANSITION = { duration: 0.45, ease: [0.2, 0.8, 0.2, 1] } as const;

// Toggling rejected proposals swaps layouts; tween between them so the tree
// slides open and shut instead of jumping. Other layout changes (streamed
// candidates) apply directly, since new nodes already animate in.
function useLayoutMorph(
  target: LayoutResult,
  morphKey: boolean,
  reduceMotion: boolean,
): { layout: LayoutResult; progress: number } {
  const [frame, setFrame] = useState<{
    target: LayoutResult;
    layout: LayoutResult;
    progress: number;
  } | null>(null);
  const shownRef = useRef(target);
  const keyRef = useRef(morphKey);
  // A layout effect so the first tween frame replaces the new target before
  // paint; a passive effect would flash the end state for one frame.
  useLayoutEffect(() => {
    const from = shownRef.current;
    const keyChanged = keyRef.current !== morphKey;
    keyRef.current = morphKey;
    if (!keyChanged || reduceMotion) {
      shownRef.current = target;
      return;
    }
    setFrame({ target, layout: from, progress: 0 });
    const controls = animate(0, 1, {
      ...MORPH_TRANSITION,
      onUpdate: (t) => {
        const layout = interpolateLayout(from, target, t);
        shownRef.current = layout;
        setFrame({ target, layout, progress: t });
      },
      onComplete: () => setFrame(null),
    });
    return () => controls.stop();
  }, [target, morphKey, reduceMotion]);
  return frame !== null && frame.target === target
    ? { layout: frame.layout, progress: frame.progress }
    : { layout: target, progress: 1 };
}

export function TrajectoryTree({
  layout: fullLayout,
  layoutWithoutRejected,
  selectedId,
  newestId,
  onSelectCandidate,
  onSelectRejected,
}: TrajectoryTreeProps) {
  const reduceMotion = useReducedMotion();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [view, setView] = useState<View>({ k: 1, tx: 0, ty: 0 });
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isMaximized, setIsMaximized] = useState(false);
  const [layers, setLayers] = useState<LayerVisibility>({
    score: true,
    winner: true,
    rejected: true,
  });
  const toggleLayer = useCallback((key: keyof LayerVisibility) => {
    setLayers((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);
  const panStateRef = useRef<null | {
    pointerId: number;
    startClientX: number;
    startClientY: number;
    startTx: number;
    startTy: number;
    moved: boolean;
  }>(null);
  // Auto-fit keeps re-centering as new candidates stream in until the user
  // actively pans, wheel-zooms, or button-zooms — once they take control we
  // freeze their framing. Reset / maximize-toggle release the lock.
  const userInteractedRef = useRef(false);

  const targetLayout = useMemo(
    () =>
      layers.rejected || layoutWithoutRejected === undefined
        ? fullLayout
        : collapseGhosts(fullLayout, layoutWithoutRejected),
    [layers.rejected, fullLayout, layoutWithoutRejected],
  );
  const { layout, progress: morphProgress } = useLayoutMorph(
    targetLayout,
    layers.rejected,
    !!reduceMotion,
  );
  // Morph progress already covered by the recentering glide, or null when no
  // glide is running.
  const recenterFromRef = useRef<number | null>(null);
  const { nodes, ghosts, edges, width, height } = layout;
  const idIndex = useMemo(() => {
    const m = new Map<string, TrajectoryNode>();
    for (const n of nodes) m.set(n.candidate_id, n);
    return m;
  }, [nodes]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // Seed size synchronously so the SVG viewBox is correct on the first paint
    // after a portal move (ResizeObserver's initial dispatch is async and would
    // otherwise paint one frame with the previous container's dimensions).
    const r = el.getBoundingClientRect();
    setSize({ w: r.width, h: r.height });
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const { width: w, height: h } = e.contentRect;
        setSize({ w, h });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
    // Re-attach when the maximize toggle moves the container via portal: React
    // unmounts/remounts the host div, so the previous observer is stale.
  }, [isMaximized]);

  // Toggling rejected proposals reshapes the whole tree, so it recenters even
  // after the user has panned. Declared before the fit effect so both run in
  // the same commit when motion is reduced.
  useEffect(() => {
    if (!userInteractedRef.current) return;
    userInteractedRef.current = false;
    recenterFromRef.current = 0;
  }, [layers.rejected]);

  useEffect(() => {
    if (size.w < 2 || size.h < 2) return;
    if (userInteractedRef.current) return;
    const fit = fitView(size, width, height);
    const from = recenterFromRef.current;
    if (from === null || from >= 1) {
      setView(fit);
      return;
    }
    // Close the same share of the remaining gap as the morph just covered,
    // so the view lands on the fit exactly when the tree settles.
    const step = (morphProgress - from) / (1 - from);
    recenterFromRef.current = morphProgress >= 1 ? null : morphProgress;
    setView((v) => ({
      k: v.k + (fit.k - v.k) * step,
      tx: v.tx + (fit.tx - v.tx) * step,
      ty: v.ty + (fit.ty - v.ty) * step,
    }));
  }, [size, width, height, morphProgress]);

  // Toggling maximize resizes the container; release the interaction lock so
  // the new viewport gets a fresh fit.
  useEffect(() => {
    userInteractedRef.current = false;
  }, [isMaximized]);

  // Lock page scroll while the maximized overlay is open so the user cannot
  // scroll the underlying content behind the fixed surface, and bind ESC to
  // exit so the overlay behaves like a normal modal.
  useEffect(() => {
    if (!isMaximized) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setIsMaximized(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKey);
    };
  }, [isMaximized]);

  const zoomAt = useCallback((cx: number, cy: number, factor: number) => {
    setView((v) => {
      const nextK = clampScale(v.k * factor);
      if (nextK === v.k) return v;
      userInteractedRef.current = true;
      const wx = (cx - v.tx) / v.k;
      const wy = (cy - v.ty) / v.k;
      return { k: nextK, tx: cx - wx * nextK, ty: cy - wy * nextK };
    });
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = container.getBoundingClientRect();
      const factor = Math.exp(-e.deltaY * ZOOM_WHEEL_FACTOR);
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, factor);
    };
    container.addEventListener("wheel", onWheel, { passive: false });
    return () => container.removeEventListener("wheel", onWheel);
    // Re-attach when maximize portals the container: the old element is
    // detached and a new one mounts in the portal target, leaving the
    // previous listener orphaned (same reason the ResizeObserver re-binds).
  }, [zoomAt, isMaximized]);

  const { start: pinchStart, move: pinchMove, end: pinchEnd } = usePinchZoom(containerRef, zoomAt);

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      // Don't hijack pointers that land on the floating zoom controls.
      const target = e.target as Element | null;
      if (target?.closest("[data-trajectory-controls]")) return;
      if (pinchStart(e)) {
        if (panStateRef.current?.moved) setIsDragging(false);
        panStateRef.current = null;
        return;
      }
      // Capturing on pointerdown would steal the synthesized click from child
      // nodes — defer until the user actually starts panning (see pointermove).
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
        setView((v) => ({ k: v.k, tx: ps.startTx + dx, ty: ps.startTy + dy }));
      }
    },
    [pinchMove],
  );

  const handlePointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      pinchEnd(e);
      const ps = panStateRef.current;
      if (ps && ps.pointerId === e.pointerId) {
        panStateRef.current = null;
        if (ps.moved) {
          (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
          setIsDragging(false);
        }
      }
    },
    [pinchEnd],
  );

  const handleNodeClick = useCallback(
    (id: string, e: React.MouseEvent) => {
      // Suppress clicks that close a pan gesture — drag-then-release should
      // not also select a node it happened to release on.
      if (panStateRef.current?.moved) return;
      e.stopPropagation();
      onSelectCandidate(id);
    },
    [onSelectCandidate],
  );

  const handleGhostClick = useCallback(
    (rejectionId: string, e: React.MouseEvent) => {
      if (panStateRef.current?.moved) return;
      e.stopPropagation();
      onSelectRejected(rejectionId);
    },
    [onSelectRejected],
  );

  const zoomFromCenter = useCallback(
    (factor: number) => zoomAt(size.w / 2, size.h / 2, factor),
    [size.w, size.h, zoomAt],
  );
  const resetView = useCallback(() => {
    userInteractedRef.current = false;
    setView(fitView(size, width, height));
  }, [size, width, height]);
  const isTransformed = useMemo(() => {
    const baseline = fitView(size, width, height);
    return view.k !== baseline.k || view.tx !== baseline.tx || view.ty !== baseline.ty;
  }, [view, size, width, height]);

  if (nodes.length === 0) return null;

  const transform = `translate(${view.tx}, ${view.ty}) scale(${view.k})`;

  const gridScreenStep = GRID_STEP * view.k;

  const treeBody = (
    <div
      ref={containerRef}
      className={
        isMaximized
          ? "fixed inset-0 z-50 w-screen h-dvh overflow-hidden border-0"
          : "relative w-full overflow-hidden rounded-xl border border-border/60"
      }
      style={
        isMaximized
          ? { background: SURFACE_GRADIENT }
          : { height: CONTAINER_HEIGHT_PX, background: SURFACE_GRADIENT }
      }
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      role="tree"
      aria-label={msg("trajectory.a11y.tree_label")}
    >
      <svg
        width="100%"
        height="100%"
        viewBox={`0 0 ${Math.max(size.w, 1)} ${Math.max(size.h, 1)}`}
        preserveAspectRatio="xMidYMid meet"
        style={{
          display: "block",
          direction: "ltr",
          cursor: isDragging ? "grabbing" : "grab",
          touchAction: "none",
        }}
      >
        {gridScreenStep > 2 ? (
          <>
            <defs>
              <pattern
                id="trajectory-grid"
                x={view.tx}
                y={view.ty}
                width={gridScreenStep}
                height={gridScreenStep}
                patternUnits="userSpaceOnUse"
              >
                <path
                  d={`M ${gridScreenStep} 0 L 0 0 L 0 ${gridScreenStep}`}
                  fill="none"
                  stroke={GRID_LINE_COLOR}
                  strokeWidth={1}
                />
              </pattern>
            </defs>
            <rect width={size.w} height={size.h} fill="url(#trajectory-grid)" />
          </>
        ) : null}
        <g transform={transform}>
          <TreeContent
            nodes={nodes}
            ghosts={ghosts}
            edges={edges}
            idIndex={idIndex}
            selectedId={selectedId}
            hoveredId={hoveredId}
            newestId={newestId}
            showScore={layers.score}
            showWinner={layers.winner}
            showRejected={layers.rejected}
            reduceMotion={!!reduceMotion}
            onNodeClick={handleNodeClick}
            onGhostClick={handleGhostClick}
            onHover={setHoveredId}
          />
        </g>
      </svg>

      <CanvasControlGroup
        data-trajectory-controls
        className={
          isMaximized ? "absolute top-4 start-4 z-20 shadow-[var(--shadow-float)]" : "absolute top-3 end-3 z-20"
        }
      >
        <CanvasControlButton
          icon={Plus}
          label={msg("trajectory.controls.zoom_in")}
          onClick={() => zoomFromCenter(ZOOM_BUTTON_IN)}
        />
        <CanvasControlButton
          icon={Minus}
          label={msg("trajectory.controls.zoom_out")}
          onClick={() => zoomFromCenter(ZOOM_BUTTON_OUT)}
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
          onClick={() => setIsMaximized((v) => !v)}
        />
      </CanvasControlGroup>

      <div className="pointer-events-none absolute inset-x-0 bottom-3 z-10 flex justify-center">
        <div className="pointer-events-auto inline-flex flex-wrap items-center gap-x-1 gap-y-1 rounded-lg border border-border/70 bg-background/85 px-2 py-1 text-[11px] font-medium text-muted-foreground/90 backdrop-blur-sm">
          <LegendToggle
            pressed={layers.score}
            onToggle={() => toggleLayer("score")}
            swatch={<ScoreSwatch />}
            label={msg("trajectory.blackbox.legend.score")}
          />
          <LegendDivider />
          <LegendToggle
            pressed={layers.winner}
            onToggle={() => toggleLayer("winner")}
            swatch={
              <span
                className="inline-flex items-center justify-center rounded-sm px-1.5 py-0.5 text-[8px] font-semibold leading-none tracking-wider rtl:tracking-normal"
                style={{
                  background: WINNER_BADGE_FILL,
                  color: WINNER_BADGE_INK,
                }}
              >
                {msg("trajectory.node.winning_label")}
              </span>
            }
            label={TERMS.winningCandidate}
          />
          {fullLayout.ghosts.length > 0 ? (
            <>
              <LegendDivider />
              <LegendToggle
                pressed={layers.rejected}
                onToggle={() => toggleLayer("rejected")}
                swatch={
                  <span
                    className="inline-block size-2.5 rounded-full"
                    style={{
                      background: GHOST_FILL,
                      border: `1px dashed ${GHOST_STROKE}`,
                    }}
                  />
                }
                label={msg("trajectory.ghost.legend")}
              />
            </>
          ) : null}
        </div>
      </div>
    </div>
  );

  if (isMaximized && typeof document !== "undefined") {
    return (
      <>
        {/* Placeholder keeps the panel's vertical rhythm intact while the
            tree is portaled into a viewport-spanning overlay above. */}
        <div
          aria-hidden="true"
          className="w-full rounded-xl border border-border/60 opacity-40"
          style={{ height: CONTAINER_HEIGHT_PX, background: SURFACE_GRADIENT }}
        />
        {createPortal(treeBody, document.body)}
      </>
    );
  }
  return treeBody;
}

interface TreeContentProps {
  nodes: TrajectoryNode[];
  ghosts: RejectedNode[];
  edges: LayoutResult["edges"];
  idIndex: Map<string, TrajectoryNode>;
  selectedId: string | null;
  hoveredId: string | null;
  newestId: string | null;
  showScore: boolean;
  showWinner: boolean;
  showRejected: boolean;
  reduceMotion: boolean;
  onNodeClick: (id: string, e: React.MouseEvent) => void;
  onGhostClick: (rejectionId: string, e: React.MouseEvent) => void;
  onHover: (id: string | null) => void;
}

// The edge/ghost/node geometry is expressed in layout coordinates and never
// depends on the live pan/zoom transform (that lives on the parent <g>). Pulling
// it into a memoized child means a pan or zoom — which fires setView on every
// pointermove/wheel tick — re-renders only the lightweight outer <g>, not the
// hundreds of SVG primitives below it.
const TreeContent = memo(function TreeContent({
  nodes,
  ghosts,
  edges,
  idIndex,
  selectedId,
  hoveredId,
  newestId,
  showScore,
  showWinner,
  showRejected,
  reduceMotion,
  onNodeClick,
  onGhostClick,
  onHover,
}: TreeContentProps) {
  const [hoveredGhostId, setHoveredGhostId] = useState<string | null>(null);
  // A 0–1 scorer fills the arc with the raw score; anything on a larger
  // scale is measured against the tree's best candidate instead.
  const scoreScale = useMemo(() => Math.max(1, ...nodes.map((n) => n.score)), [nodes]);
  return (
    <>
      <g>
        {edges.map((edge, i) => {
          const from = idIndex.get(edge.from);
          const to = idIndex.get(edge.to);
          if (from === undefined || to === undefined) return null;
          // Orthogonal elbow routing: drop from the parent, run flat at the
          // midpoint row, drop into the child. Siblings overlap on the shared
          // vertical stub, which reads as one strict connector bus.
          const midY = (from.y + to.y) / 2;
          const d =
            from.x === to.x
              ? `M ${from.x} ${from.y} L ${to.x} ${to.y}`
              : `M ${from.x} ${from.y} L ${from.x} ${midY} L ${to.x} ${midY} L ${to.x} ${to.y}`;
          return (
            <motion.path
              key={`${edge.from}-${edge.to}-${i}`}
              d={d}
              fill="none"
              stroke={edge.isMerge ? EDGE_STROKE_MERGE : EDGE_STROKE}
              strokeWidth={edge.isMerge ? 1.2 : 1.6}
              strokeDasharray={edge.isMerge ? "4 4" : undefined}
              initial={reduceMotion ? false : { pathLength: 0, opacity: 0 }}
              animate={reduceMotion ? undefined : { pathLength: 1, opacity: 1 }}
              transition={reduceMotion ? undefined : { duration: 0.45, ease: [0.2, 0.8, 0.2, 1] }}
            />
          );
        })}
      </g>

      <LayerFade show={showRejected} reduceMotion={reduceMotion}>
        <g>
          {ghosts.map((ghost) => {
            const parent = idIndex.get(ghost.parent_id);
            if (parent === undefined) return null;
            // Same elbow as a lineage edge, dashed: a branch that was tried
            // and dropped.
            const midY = (parent.y + ghost.y) / 2;
            const d =
              parent.x === ghost.x
                ? `M ${parent.x} ${parent.y} L ${ghost.x} ${ghost.y}`
                : `M ${parent.x} ${parent.y} L ${parent.x} ${midY} L ${ghost.x} ${midY} L ${ghost.x} ${ghost.y}`;
            return (
              <path
                key={`ghost-edge-${ghost.rejection_id}`}
                d={d}
                fill="none"
                stroke={EDGE_STROKE_GHOST}
                strokeWidth={1.2}
                strokeDasharray="4 4"
              />
            );
          })}
        </g>
      </LayerFade>

      {/* Each ghost drives its own opacity from the toggle rather than
          inheriting a group fade: with only mount and hover animating it,
          some ghosts stayed invisible after re-showing until hovered. Keep
          `opacity` out of `style`: an undefined style key makes motion drop
          the value on every morph frame, freezing the fade midway. */}
      <g>
        {ghosts.map((ghost) => {
          const isHovered = ghost.rejection_id === hoveredGhostId;
          const r = TRAJECTORY_LAYOUT.nodeRadius;
          const mark = r * 0.28;
          const restOpacity = !showRejected ? 0 : isHovered ? 0.95 : 0.7;
          return (
            <motion.g
              key={`ghost-${ghost.rejection_id}`}
              role="treeitem"
              aria-label={formatMsg("trajectory.a11y.ghost_label", {
                parent: displayCandidateId(ghost.parent_id),
                score: ghost.proposal_score.toFixed(2),
              })}
              aria-selected={false}
              tabIndex={-1}
              onMouseEnter={() => setHoveredGhostId(ghost.rejection_id)}
              onMouseLeave={() => setHoveredGhostId(null)}
              onClick={(e) => onGhostClick(ghost.rejection_id, e)}
              aria-hidden={!showRejected}
              initial={reduceMotion ? false : { scale: 0.7, opacity: 0 }}
              animate={{ scale: 1, opacity: restOpacity }}
              transition={
                reduceMotion ? { duration: 0 } : { duration: 0.35, ease: [0.2, 0.8, 0.2, 1] }
              }
              pointerEvents={showRejected ? "auto" : "none"}
              style={{ cursor: "pointer" }}
            >
              <circle
                cx={ghost.x}
                cy={ghost.y}
                r={r - 0.75}
                fill={GHOST_FILL}
                fillOpacity={0.55}
                stroke={isHovered ? GHOST_STROKE_HOVER : GHOST_STROKE}
                strokeWidth={1.5}
                strokeDasharray="5 4"
              />
              <path
                d={`M ${ghost.x - mark} ${ghost.y - mark} L ${ghost.x + mark} ${ghost.y + mark} M ${ghost.x + mark} ${ghost.y - mark} L ${ghost.x - mark} ${ghost.y + mark}`}
                stroke={GHOST_STROKE}
                strokeWidth={1.6}
                strokeLinecap="round"
                pointerEvents="none"
              />
              <text
                x={ghost.x}
                y={ghost.y + r + 14}
                textAnchor="middle"
                fontFamily="var(--font-mono, monospace)"
                fontSize="10.5"
                fontWeight={600}
                fill="color-mix(in srgb, var(--foreground) 60%, transparent)"
                stroke={LABEL_HALO}
                strokeWidth={2.5}
                strokeLinejoin="round"
                pointerEvents="none"
                style={{ fontVariantNumeric: "tabular-nums", paintOrder: "stroke" }}
              >
                {ghost.proposal_score.toFixed(2)}
              </text>
            </motion.g>
          );
        })}
      </g>

      <g>
        {nodes.map((node) => {
          const isSelected = node.candidate_id === selectedId;
          const isHovered = node.candidate_id === hoveredId;
          const isNewest = node.candidate_id === newestId;
          const coreStroke = isSelected
            ? NODE_CORE_STROKE_SELECTED
            : isHovered
              ? NODE_CORE_STROKE_HOVER
              : NODE_CORE_STROKE;
          const innerRadius = TRAJECTORY_LAYOUT.nodeRadius - DONUT_RING_THICKNESS;
          const winnerShown = node.isWinner && showWinner;
          return (
            <motion.g
              key={node.candidate_id}
              role="treeitem"
              aria-label={formatMsg("trajectory.a11y.node_label", {
                id: displayCandidateId(node.candidate_id),
                gen: node.generation,
                score: node.score.toFixed(2),
              })}
              aria-selected={isSelected}
              tabIndex={isSelected ? 0 : -1}
              onMouseEnter={() => onHover(node.candidate_id)}
              onMouseLeave={() => onHover(null)}
              onClick={(e) => onNodeClick(node.candidate_id, e)}
              initial={
                reduceMotion
                  ? false
                  : isNewest
                    ? { scale: 0, opacity: 0 }
                    : { scale: 0.7, opacity: 0 }
              }
              animate={reduceMotion ? undefined : { scale: 1, opacity: 1 }}
              transition={reduceMotion ? undefined : { duration: 0.45, ease: [0.2, 0.8, 0.2, 1] }}
              style={{ cursor: "pointer" }}
            >
              {isNewest && !reduceMotion ? (
                <motion.circle
                  cx={node.x}
                  cy={node.y}
                  r={TRAJECTORY_LAYOUT.nodeRadius}
                  fill="none"
                  stroke={WINNER_INDICATOR}
                  strokeWidth="1.6"
                  initial={{ r: TRAJECTORY_LAYOUT.nodeRadius, opacity: 0.6 }}
                  animate={{ r: TRAJECTORY_LAYOUT.nodeRadius * 2.2, opacity: 0 }}
                  transition={{ duration: 1.2, ease: "easeOut", repeat: 2 }}
                />
              ) : null}
              {isSelected ? (
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={TRAJECTORY_LAYOUT.nodeRadius + 5}
                  fill="none"
                  stroke={NODE_CORE_STROKE_SELECTED}
                  strokeWidth={1.5}
                  strokeOpacity={0.7}
                />
              ) : null}
              {node.isWinner ? (
                <LayerFade show={showWinner} reduceMotion={reduceMotion}>
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={TRAJECTORY_LAYOUT.nodeRadius + 11}
                    fill="none"
                    stroke={WINNER_HALO}
                    strokeWidth={3}
                  />
                  {reduceMotion ? null : (
                    <motion.circle
                      cx={node.x}
                      cy={node.y}
                      r={TRAJECTORY_LAYOUT.nodeRadius + 4}
                      fill="none"
                      stroke={WINNER_INDICATOR}
                      strokeWidth={1.4}
                      initial={{
                        opacity: 0.5,
                        r: TRAJECTORY_LAYOUT.nodeRadius + 4,
                      }}
                      animate={{
                        opacity: [0.5, 0.12, 0.5],
                        r: [
                          TRAJECTORY_LAYOUT.nodeRadius + 4,
                          TRAJECTORY_LAYOUT.nodeRadius + 10,
                          TRAJECTORY_LAYOUT.nodeRadius + 4,
                        ],
                      }}
                      transition={{
                        duration: 3.2,
                        ease: "easeInOut",
                        repeat: Infinity,
                      }}
                    />
                  )}
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={TRAJECTORY_LAYOUT.nodeRadius + 4}
                    fill="none"
                    stroke={WINNER_INDICATOR}
                    strokeWidth={2.6}
                  />
                </LayerFade>
              ) : null}
              {renderScoreRing(node, node.score / scoreScale, showScore, reduceMotion)}
              <circle
                cx={node.x}
                cy={node.y}
                r={innerRadius}
                fill={winnerShown ? WINNER_FILL : NODE_CORE_FILL}
                stroke={coreStroke}
                strokeWidth={isSelected ? 1.4 : 0.8}
                style={{
                  transition: "fill 250ms ease, stroke 120ms ease, stroke-width 120ms ease",
                }}
              />
              <text
                x={node.x}
                y={node.y + 5}
                textAnchor="middle"
                fontFamily="var(--font-mono, monospace)"
                fontSize="14"
                fontWeight={700}
                fill={winnerShown ? "var(--tree-winner-number)" : "var(--foreground)"}
                pointerEvents="none"
              >
                {displayCandidateId(node.candidate_id)}
              </text>
              {node.isWinner ? (
                <LayerFade show={showWinner} reduceMotion={reduceMotion}>
                  <WinnerBadge x={node.x} y={node.y + TRAJECTORY_LAYOUT.nodeRadius + 4} />
                </LayerFade>
              ) : null}
              <motion.g
                initial={false}
                animate={{ y: node.isWinner && !showWinner ? -20 : 0 }}
                transition={reduceMotion ? { duration: 0 } : { duration: 0.25, ease: "easeOut" }}
              >
                <text
                  x={node.x}
                  y={node.y + TRAJECTORY_LAYOUT.nodeRadius + (node.isWinner ? 34 : 14)}
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
                  {node.score.toFixed(2)}
                </text>
              </motion.g>
            </motion.g>
          );
        })}
      </g>
    </>
  );
});

function WinnerBadge({ x, y }: { x: number; y: number }) {
  const label = msg("trajectory.node.winning_label");
  // SVG text can't auto-size its background rect, so estimate the label's
  // width from its glyph count (~5.6px per glyph at 9.5px bold Heebo) and
  // pad each side, so the gold never clips the letters in either locale.
  const w = Math.round(label.length * 5.6) + 14;
  const h = 16;
  const cx = x;
  const top = y + 4;
  return (
    <g pointerEvents="none">
      <rect x={cx - w / 2} y={top} width={w} height={h} rx={3} ry={3} fill={WINNER_BADGE_FILL} />
      <text
        x={cx}
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

function ScoreSwatch() {
  return (
    <svg className="size-2.5" viewBox="0 0 10 10" aria-hidden="true">
      <circle cx="5" cy="5" r="3.5" fill="none" stroke={SCORE_TRACK_STROKE} strokeWidth="3" />
      <circle
        cx="5"
        cy="5"
        r="3.5"
        fill="none"
        stroke={DONUT_PASS_FILL}
        strokeWidth="3"
        pathLength={1}
        strokeDasharray="0.65 0.35"
        strokeDashoffset={-0.75}
      />
    </svg>
  );
}

function LegendDivider() {
  return <span aria-hidden="true" className="inline-block h-3 w-px bg-border/70" />;
}
