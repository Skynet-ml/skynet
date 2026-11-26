"use client";

import { useSession } from "next-auth/react";
import { DashboardSkeleton } from "@/features/dashboard";

export default function Loading() {
  // A signed-out visitor at "/" gets the landing page, not the dashboard (SKY-922).
  const { status } = useSession();
  return status === "unauthenticated" ? null : <DashboardSkeleton />;
}
