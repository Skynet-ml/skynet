"use client";

import * as React from "react";

import { formatMsg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { separatorParts } from "./message-time";

// A marker the date goes through the template as, so the label can bold the
// date wherever the locale puts it.
const DATE_MARK = "\u0000";

/** The centered "Today 9:05 AM" line that opens a turn after a pause. */
export function TimestampSeparator({ at, now }: { at: number; now: number }) {
  const { date, time, older } = separatorParts(at, now, getActiveIntlLocale());
  const key = older ? "shared.agent.timestamp_older" : "shared.agent.timestamp_recent";
  const label = formatMsg(key, { date, time });
  const [before, after = ""] = formatMsg(key, { date: DATE_MARK, time }).split(DATE_MARK);
  return (
    <div role="separator" aria-label={label} className="flex justify-center py-4">
      <time
        dir="auto"
        dateTime={new Date(at).toISOString()}
        className="select-none text-sm leading-5 text-muted-foreground"
      >
        {before}
        <span className="font-medium">{date}</span>
        {after}
      </time>
    </div>
  );
}
