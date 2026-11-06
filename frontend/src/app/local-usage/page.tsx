"use client";

import { LocalCliUsagePanel } from "@/features/local-usage";

// A stand-alone mount until SKY-340 lands the Usage tab, which will host the panel.
export default function LocalUsagePage() {
  return (
    <main className="mx-auto w-full max-w-4xl p-6">
      <LocalCliUsagePanel />
    </main>
  );
}
