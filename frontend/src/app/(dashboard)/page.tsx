"use client";

import { Suspense } from "react";
import { useSession } from "next-auth/react";
import { redirect } from "next/navigation";
import { DashboardView } from "@/features/dashboard";
import { LandingPage } from "@/features/landing";
import { isLocalMode } from "@/shared/lib/runtime-env";

export default function Page() {
  // The proxy lets a signed-out visitor reach "/" (SKY-922). The session is
  // server-resolved, so the status is stable from the first render.
  const { status } = useSession();
  if (status === "unauthenticated") {
    // The desktop app opens "/" and signs its one local user in on /login only.
    if (isLocalMode()) redirect("/login");
    return <LandingPage />;
  }
  return (
    <Suspense fallback={null}>
      <DashboardView />
    </Suspense>
  );
}
