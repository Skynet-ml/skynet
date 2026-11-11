import type { ExecutionRuntime, RuntimeCostProfile } from "./api";

export interface ExecutionRuntimeCatalog {
  runtimes: Array<{
    id: ExecutionRuntime;
    available: boolean;
    unavailable_reason: string | null;
    cost: RuntimeCostProfile;
    checkpoint_restore_supported: boolean;
    checkpoint_restore_reason: string | null;
  }>;
  default_runtime: ExecutionRuntime;
  run_recovery_eligibility: string;
}
