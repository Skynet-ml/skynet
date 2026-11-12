"use client";

import type { CSSProperties } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { CLIMB_CHART_HEIGHT_PX, useTrajectoryViewHint } from "@/features/trajectory";
import { Ghost, Skeleton } from "@/shared/ui/skeleton";
import { Card, CardContent, CardHeader } from "@/shared/ui/primitives/card";
import {
  useRunShapeHint,
  type OverviewBlock,
  type RunShape,
  type StageShape,
} from "../lib/run-kind-hint";
import { PHONE_DETAIL_TABS, requestedDetailTab, shownDetailTab } from "../lib/detail-tabs";
import { detailTabGate, useExperienceLevel } from "@/features/experience";
import { useIsPhone } from "@/shared/hooks/use-device-class";
import { PipelineStagesBone } from "./PipelineStages";
import { UsageTabBone } from "./UsageTab";
import { RunAccessBanner } from "./RunAccessBanner";

/*
 * Mirrors OptimizationDetailView: header card, tab bar, then the run Overview
 * (status line, pipeline, score cards, trajectory).
 *
 * A view rendered before in this tab remembered its tab and stage counts (see
 * run-kind-hint.ts), so a revisit draws exactly those; a cold visit draws the
 * most common shape for the run's kind.
 */

const GENERIC_STAGE: StageShape = { detail: false, foot: "time" };

// Cold-visit stage count (plannedStageKeys): a run on the auto strategy has four.
function stageShapes(shape: RunShape | undefined): readonly StageShape[] {
  if (shape?.stages?.length) return shape.stages;
  return Array.from({ length: 4 }, () => GENERIC_STAGE);
}

// Lays out a remembered header line in its own font so the bone wraps onto
// exactly as many lines, each as wide, as the text it stands for.
function TextBone({ text }: { text: string }) {
  return (
    <span className="rounded-md bg-[#ebe4d8] dark:bg-[#484135] box-decoration-clone text-transparent select-none">
      {text}
    </span>
  );
}

// Icon buttons are size-8, and globals.css grows them to 44px under a coarse
// pointer (every iPad), so the bones grow with them. 44px, not size-11: the
// root font is fluid, so size-11 is only 39.6px on a phone.
function IconButtonBone() {
  return (
    <span className="flex size-8 any-pointer-coarse:size-[44px]">
      <Skeleton containerClassName="flex-1 leading-none" height="100%" borderRadius={8} />
    </span>
  );
}

// A revisit's Overview: each block at the height it last rendered with, so the
// page lands without moving. Text lines and the stage tracker keep their shape.
function MeasuredOverviewBone({
  blocks,
  stages,
}: {
  blocks: readonly OverviewBlock[];
  stages: readonly StageShape[];
}) {
  return (
    <div>
      {blocks.map((b, i) => (
        <div key={i} style={{ marginTop: b.gap }}>
          {b.kind === "pipeline" ? (
            <PipelineStagesBone stages={stages} />
          ) : b.kind === "text" ? (
            <div className="flex flex-col justify-around" style={{ height: b.h }}>
              {Array.from({ length: Math.max(1, Math.round(b.h / 20)) }, (_, line) => (
                <Skeleton key={line} width={line === 0 ? "70%" : "45%"} height={12} />
              ))}
            </div>
          ) : (
            <div
              className="flex flex-col gap-3 overflow-hidden rounded-xl border border-border/50 bg-card p-4"
              style={{ height: b.h }}
            >
              <Skeleton width={140} height={14} />
              {b.h > 100 && (
                <Skeleton
                  height="100%"
                  borderRadius={12}
                  containerClassName="block min-h-0 flex-1 leading-none"
                />
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// Tab panels that sit 16px under the tab bar (TabsContent mt-4); Data and Logs carry their own.
const SPACED_PANELS = new Set(["playground", "best", "code", "artifact", "usage", "config"]);

// A deep link to another tab (`?tab=data`): that tab's own loading skeleton
// where it has one, else a block the height that panel last rendered at this
// width. A tab this run does not offer renders nothing, as the loaded page does.
function TabBodyBone({
  tab,
  shape,
  optimizationId,
}: {
  tab: string;
  shape: RunShape | undefined;
  optimizationId: string | undefined;
}) {
  if (shape?.tabIds && !shape.tabIds.includes(tab)) return null;
  if (tab === "usage" && optimizationId) {
    return (
      <div className="mt-4">
        <UsageTabBone optimizationId={optimizationId} />
      </div>
    );
  }
  const panel = shape?.panels?.[tab];
  const fits = panel && panel.vw === window.innerWidth;
  const gap = fits ? (panel.gap ?? 0) : 0;
  return (
    <div
      style={{
        height: fits ? panel.h : 480,
        marginTop: SPACED_PANELS.has(tab) ? `calc(1rem + ${gap}px)` : gap,
      }}
    >
      <Skeleton height="100%" borderRadius={12} containerClassName="block h-full leading-none" />
    </div>
  );
}

export function OptimizationDetailSkeleton() {
  const params = useParams<{ id?: string }>();
  const hintKey = params?.id;
  const shape = useRunShapeHint(hintKey);
  const trajectory = useTrajectoryViewHint(hintKey);
  const climb = trajectory === "climb";
  // The phone shell (below md) drops the Data, Code and Config tabs.
  // The Overview as it last rendered, when it rendered at this very width.
  const measured =
    shape?.overview && shape.overview.vw === window.innerWidth ? shape.overview.blocks : null;
  const isPhone = useIsPhone();
  // An owner's finished run: share, clone and delete; the phone shell keeps share.
  const remembered = isPhone ? shape?.actions?.phone : shape?.actions?.wide;
  const headerActions = remembered === undefined ? (isPhone ? 1 : 3) : remembered;
  const searchParams = useSearchParams();
  const tab = shownDetailTab(requestedDetailTab(searchParams.get("tab")), isPhone);
  let tabBones: Array<{ key: string | number; active: boolean; phone: boolean }>;
  // The same level gate as the loaded tab bar, so a level changed since the
  // last visit (or a first visit) draws the tabs the page will actually show.
  const level = useExperienceLevel();
  const levelTab = detailTabGate(level);
  const linkedTab = requestedDetailTab(searchParams.get("tab"));
  const keepsTab = (id: string) => levelTab(id) || id === linkedTab;
  if (shape?.tabIds) {
    tabBones = shape.tabIds.filter(keepsTab).map((id) => ({
      key: id,
      active: id === tab,
      phone: PHONE_DETAIL_TABS.has(id),
    }));
  } else {
    // Without remembered ids, Guided's hidden desk tabs (Data, Code, Config;
    // Logs also on phones) come off the default counts.
    const guided = !levelTab("logs");
    const tabs = shape?.tabs ?? Math.max(1, 6 - (guided ? 4 : 0));
    const phoneTabs = Math.min(tabs, shape?.phoneTabs ?? Math.max(1, 4 - (guided ? 1 : 0)));
    tabBones = Array.from({ length: tabs }, (_, i) => ({
      key: i,
      active: i === 0 && tab === "overview",
      phone: i < phoneTabs,
    }));
  }
  return (
    <div className="space-y-6 pb-12" aria-hidden="true">
      {/* A block of its own: the column's space-y margin is lost on Ghost's
          display: contents wrapper. */}
      {shape?.access && (
        <div>
          <Ghost>
            <RunAccessBanner tier={shape.access.tier} owner={shape.access.owner} />
          </Ghost>
        </div>
      )}
      <div className="rounded-xl border border-border/40 bg-card p-4 sm:p-5">
        <div className="flex flex-wrap items-start justify-between gap-4 sm:flex-nowrap">
          <div className="min-w-0 space-y-2 sm:flex-1">
            <div className="flex flex-col items-start gap-1.5">
              {/* StatusBadge's box: its text line, py-1 and a 1px border. */}
              <span className="inline-flex w-[72px] rounded-md border border-transparent bg-[#ebe4d8] dark:bg-[#484135] py-1 text-[0.8125rem] select-none">
                &zwnj;
              </span>
              {shape && "name" in shape ? (
                shape.name && (
                  <h2 className="text-lg font-bold tracking-tight sm:text-xl" dir="auto">
                    <TextBone text={shape.name} />
                  </h2>
                )
              ) : (
                <span className="block w-[55%] max-w-[320px]">
                  <Skeleton height={24} />
                </span>
              )}
            </div>
            {shape && "description" in shape ? (
              shape.description && (
                <p className="text-sm leading-relaxed">
                  <TextBone text={shape.description} />
                </p>
              )
            ) : (
              <span className="block w-[80%] max-w-[420px]">
                <Skeleton height={12} />
              </span>
            )}
            {params?.id ? (
              <span className="inline-flex items-center gap-1">
                <code className="font-mono text-xs break-all" dir="ltr">
                  <TextBone text={params.id} />
                </code>
                {/* The icon-xs copy button. */}
                <span className="flex size-7 any-pointer-coarse:size-[44px]" />
              </span>
            ) : (
              <div className="flex min-h-7 items-center any-pointer-coarse:min-h-[44px]">
                <span className="block w-[40%] max-w-[220px]">
                  <Skeleton height={10} />
                </span>
              </div>
            )}
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <Skeleton width={60} />
              {/* The storage link and cost chip are 44px targets on touch; the
                  chip on phones too, and the link is not shown there. */}
              {(shape?.storageLink ?? false) && (
                <span className="hidden items-center md:flex any-pointer-coarse:min-h-[44px]">
                  <Skeleton width={56} />
                </span>
              )}
              {(shape?.costChip ?? true) && (
                <span className="flex min-h-[44px] items-center sm:min-h-0 any-pointer-coarse:min-h-[44px]">
                  <Skeleton width={56} />
                </span>
              )}
            </div>
          </div>
          {headerActions !== null && (
            <div className="flex w-full items-center justify-end gap-1 sm:w-auto sm:shrink-0 sm:gap-2">
              {Array.from({ length: headerActions }, (_, i) => (
                <IconButtonBone key={i} />
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex h-11 items-stretch overflow-hidden border-b border-border/50">
          {tabBones.map(({ key, active, phone }) => (
            <span
              key={key}
              className={`min-h-[44px] shrink-0 items-center gap-1.5 border-b-2 px-2.5 sm:px-4 ${
                active ? "border-b-primary/30" : "border-transparent"
              } ${phone ? "flex" : "hidden md:flex"}`}
            >
              <Skeleton width={14} height={14} />
              <Skeleton width={48} height={12} />
            </span>
          ))}
        </div>

        {tab !== "overview" ? (
          <TabBodyBone tab={tab} shape={shape} optimizationId={params?.id} />
        ) : (
          <div className="mt-4">
            {measured ? (
              <MeasuredOverviewBone blocks={measured} stages={stageShapes(shape)} />
            ) : (
              <div className="space-y-6">
                <p className="w-[70%] max-w-[460px] text-sm">
                  <Skeleton />
                </p>

                <PipelineStagesBone stages={stageShapes(shape)} />

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                  {Array.from({ length: 3 }).map((_, i) => (
                    <div
                      key={i}
                      className="rounded-xl border border-border/50 bg-card p-6 text-center"
                    >
                      <div className="mb-2 flex h-4 items-center justify-center">
                        <Skeleton width={96} height={11} containerClassName="leading-none" />
                      </div>
                      <div className="flex h-9 items-center justify-center">
                        <Skeleton width={110} height={30} containerClassName="leading-none" />
                      </div>
                    </div>
                  ))}
                </div>

                {/* No trajectory card when the run was remembered to have no candidates yet. */}
                {trajectory !== "none" && (
                  <Card className="relative overflow-hidden">
                    <CardHeader className="flex flex-row items-center justify-between gap-3">
                      <span className="flex h-6 items-center gap-2">
                        <Skeleton width={16} height={16} />
                        <Skeleton width={120} height={16} />
                      </span>
                      <Skeleton width={72} height={12} />
                    </CardHeader>
                    <CardContent className="space-y-3">
                      {/* The generation scrubber, which a single-generation tree omits. */}
                      {trajectory !== "plain" && (
                        <div className="rounded-xl border border-border/40 bg-background/70 px-4 pt-3 pb-4">
                          <div className="mb-3 flex h-[15px] items-center">
                            <Skeleton width={88} height={10} containerClassName="leading-none" />
                          </div>
                          <div className="flex h-9 items-center">
                            <Skeleton
                              height={4}
                              borderRadius={2}
                              containerClassName="flex-1 leading-none"
                            />
                          </div>
                        </div>
                      )}
                      {/* TrajectoryTree's fixed 560px canvas, or the shorter climb chart
                  when this run is remembered to draw one. */}
                      {climb ? (
                        <div
                          className="h-(--climb-h) any-pointer-coarse:h-(--climb-h-coarse)"
                          style={
                            {
                              "--climb-h": `${CLIMB_CHART_HEIGHT_PX.fine}px`,
                              "--climb-h-coarse": `${CLIMB_CHART_HEIGHT_PX.coarse}px`,
                            } as CSSProperties
                          }
                        >
                          <Skeleton
                            height="100%"
                            borderRadius={12}
                            containerClassName="block h-full leading-none"
                          />
                        </div>
                      ) : (
                        <Skeleton height={560} borderRadius={12} />
                      )}
                    </CardContent>
                  </Card>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
