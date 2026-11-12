"use client";

import * as React from "react";

import { useAgentFieldLock } from "@/features/agent-panel";
import { cn } from "@/shared/lib/utils";
import { PingDot } from "@/shared/ui/ping-dot";

type LockedField = Parameters<typeof useAgentFieldLock>[0];

/**
 * Holds a group of wizard controls while the setup agent writes one of their
 * fields: they turn read-only, with the agent's dot at the group's top end
 * corner, and open again once the write lands.
 */
export function AgentFieldLock({
  fields,
  className,
  children,
}: {
  fields: LockedField;
  className?: string;
  children: React.ReactNode;
}) {
  const locked = useAgentFieldLock(fields);
  return (
    <fieldset
      disabled={locked}
      aria-busy={locked || undefined}
      className={cn("relative min-w-0", className)}
    >
      {locked && <PingDot size="sm" tone="agent" className="absolute end-1 top-1 z-10" />}
      {children}
    </fieldset>
  );
}
