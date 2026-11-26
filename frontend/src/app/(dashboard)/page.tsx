"use client";

import { Suspense } from "react";
import { useSession } from "next-auth/react";
import { DashboardView } from "@/features/dashboard";
import { LandingPage } from "@/features/landing";

export default function Page() {
  // The proxy lets a signed-out visitor reach "/" (SKY-922). The session is
  // server-resolved, so the status is stable from the first render.
  const { status } = useSession();
  if (status === "unauthenticated") return <LandingPage />;
  return (
    <Suspense fallback={null}>
      <DashboardView />
    </Suspense>
  );
}
