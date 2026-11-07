"use client";

import { AnimatePresence, motion } from "framer-motion";
import { CaretRight } from "@/shared/ui/icons";
import {
  LIST_ROW_CLASS,
  LIST_ROW_META_CLASS,
  LIST_ROW_META_DOT_CLASS,
  LIST_ROW_TITLE_CLASS,
} from "@/shared/ui/list-row";
import { PingDot } from "@/shared/ui/ping-dot";
import { StatusBadge } from "@/shared/ui/status-badge";
import { formatId, formatRelativeTime } from "@/shared/lib";
import { ACTIVE_STATUSES } from "@/shared/constants/job-status";
import { formatMsg, msg } from "@/shared/lib/messages";
import { TERMS } from "@/shared/lib/terms";
import { cn } from "@/shared/lib/utils";
import type { OptimizationSummaryResponse } from "@/shared/types/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { runKey } from "@/features/sidebar";
import { LiveElapsed } from "./LiveElapsed";
import { formatScore } from "../lib/status-badges";

type PhoneJobListProps = {
  items: OptimizationSummaryResponse[];
  showOwner: boolean;
  sessionUser: string;
  onOpenJob: (id: string, source?: ApiSource) => void;
};

// Phone replacement for the jobs table: one tappable card per run carrying
// only what a glance needs (name, status, owner, age, score, elapsed).
// Selection, column filters, resize and export stay desktop-only.
export function PhoneJobList({ items, showOwner, sessionUser, onOpenJob }: PhoneJobListProps) {
  return (
    <ul className="flex flex-col gap-2">
      <AnimatePresence initial={false}>
        {items.map((job, idx) => {
          const active = ACTIVE_STATUSES.has(job.status);
          const owner =
            showOwner && job.username
              ? job.username.toLowerCase() === sessionUser.toLowerCase()
                ? msg("dashboard.owner.me")
                : job.username
              : null;
          return (
            <motion.li
              key={runKey(job)}
              layout="position"
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, transition: { duration: 0.15 } }}
              transition={{
                layout: { duration: 0.3, ease: [0.2, 0.8, 0.2, 1] },
                opacity: { duration: 0.3, delay: Math.min(idx, 12) * 0.03 },
                y: { duration: 0.3, delay: Math.min(idx, 12) * 0.03 },
              }}
            >
              <button
                type="button"
                onClick={() => onOpenJob(job.optimization_id, job.source)}
                aria-label={formatMsg("auto.features.dashboard.components.jobstab.template.3", {
                  p1: TERMS.optimization,
                })}
                className={cn(LIST_ROW_CLASS, "w-full flex-nowrap")}
              >
                <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                  <div className="flex items-center gap-2">
                    {active && <PingDot className="shrink-0" />}
                    <span className={cn(LIST_ROW_TITLE_CLASS, "min-w-0 flex-1")} dir="auto">
                      {job.name || (
                        <span className="font-mono text-primary" dir="ltr">
                          {formatId(job.optimization_id)}
                        </span>
                      )}
                    </span>
                    <StatusBadge status={job.status} compact className="shrink-0" />
                  </div>
                  {(job.name || owner) && (
                    <div className={cn(LIST_ROW_META_CLASS, "mt-0")}>
                      {job.name && (
                        <span className="font-mono text-primary/80" dir="ltr">
                          {formatId(job.optimization_id)}
                        </span>
                      )}
                      {owner && (
                        <>
                          {job.name && (
                            <span aria-hidden="true" className={LIST_ROW_META_DOT_CLASS}>
                              ·
                            </span>
                          )}
                          <span className="truncate font-semibold text-foreground/80" dir="auto">
                            {owner}
                          </span>
                        </>
                      )}
                    </div>
                  )}
                  <div className="flex items-center gap-2 text-xs text-muted-foreground tabular-nums">
                    <span>{formatRelativeTime(job.created_at)}</span>
                    <span aria-hidden="true" className={LIST_ROW_META_DOT_CLASS}>
                      ·
                    </span>
                    <LiveElapsed
                      startedAt={job.started_at}
                      createdAt={job.created_at}
                      elapsedSeconds={job.elapsed_seconds}
                      isActive={active}
                    />
                    <span className="ms-auto">{formatScore(job)}</span>
                  </div>
                </div>
                <CaretRight
                  aria-hidden="true"
                  className="size-4 shrink-0 text-muted-foreground/40 rtl:rotate-180"
                />
              </button>
            </motion.li>
          );
        })}
      </AnimatePresence>
    </ul>
  );
}
