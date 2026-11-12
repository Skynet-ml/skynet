"use client";

import { ChartBar, Database, WarningCircle } from "@/shared/ui/icons";
import type { Icon } from "@/shared/ui/icons";
import { EmptyState } from "@/shared/ui/empty-state";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import { FadeIn } from "@/shared/ui/motion";
import { TERMS } from "@/shared/lib/terms";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CARD_TOOLBAR_HEIGHT, EMPTY_CARD_STATE } from "../constants";

interface AnalyticsEmptyProps {
  variant?: "no-data" | "no-results" | "loading-error";
  onRetry?: () => void;
}

export function AnalyticsEmpty({
  variant = "no-data",
  onRetry,
}: AnalyticsEmptyProps) {
  const configs: Record<
    NonNullable<AnalyticsEmptyProps["variant"]>,
    {
      icon: Icon;
      title: string;
      description?: string;
      action?: {
        label: string;
        onClick?: () => void;
        href?: string;
        icon?: Icon;
        iconOnly?: boolean;
      };
    }
  > = {
    "no-data": {
      icon: Database,
      title: msg("auto.features.dashboard.components.analyticsempty.literal.1"),
      description: msg("dashboard.analytics.empty_body"),
    },
    "no-results": {
      icon: ChartBar,
      title: msg("auto.features.dashboard.components.analyticsempty.literal.2"),
      description: formatMsg("auto.features.dashboard.components.analyticsempty.template.2", {
        p1: TERMS.optimizationPlural,
      }),
    },
    "loading-error": {
      icon: WarningCircle,
      title: msg("auto.features.dashboard.components.analyticsempty.literal.3"),
      description: msg("auto.features.dashboard.components.analyticsempty.literal.4"),
      action: onRetry
        ? {
            label: msg("auto.features.dashboard.components.analyticsempty.2"),
            onClick: onRetry,
            iconOnly: true,
          }
        : undefined,
    },
  };

  const config = configs[variant];
  const isNoData = variant === "no-data";

  const empty = (
    <EmptyState
      variant={isNoData ? "page" : "list"}
      icon={config.icon}
      iconWrap={isNoData ? "tile" : "none"}
      title={config.title}
      description={config.description}
      action={config.action}
      className={isNoData ? EMPTY_CARD_STATE : "min-h-[40dvh] justify-center"}
    >
      {isNoData && <div className="h-8" aria-hidden="true" />}
    </EmptyState>
  );

  // Same card, padding and toolbar-height spacer as the Jobs tab empty state.
  // The h-8 spacer takes the place of the Jobs tab's small action button.
  return (
    <FadeIn>
      {isNoData ? (
        <Card className="overflow-hidden border-border/60">
          <CardContent className="px-3 pt-4 sm:px-6 sm:pt-5">
            <div className={cn("mb-3", CARD_TOOLBAR_HEIGHT)} aria-hidden="true" />
            {empty}
          </CardContent>
        </Card>
      ) : (
        empty
      )}
    </FadeIn>
  );
}
