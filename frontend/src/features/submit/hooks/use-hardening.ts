"use client";

import * as React from "react";
import { toast } from "react-toastify";
import {
  createHardeningJob,
  decideHardeningFix,
  getHardeningJob,
  getLatestHardeningJob,
  retryHardeningJob,
  setHardeningExploitVerdict,
} from "@/shared/lib/api";
import { subscriptionAgentModel, type LocalStatus } from "@/features/local-runtime";
import { msg, type MessageKey } from "@/shared/lib/messages";
import type { BlackboxTarget, CatalogModel, ModelConfig } from "@/shared/types/api";
import type { CreateHardeningJobRequest, HardeningJob, HardeningStatus } from "@/shared/types/hardening";
import { WIZARD_HARDENING_KEY, keepSessionValue, readSessionValue } from "../lib/session-state";
import { repoIssue, type RepoDraft } from "../lib/wizard-v2";
import { useWizardV2 } from "./use-unified-wizard";

const POLL_MS = 4000;
// Only the worker moves a job out of these; every other status waits on the user or is final.
const POLLED: ReadonlySet<HardeningStatus> = new Set(["preflight", "running"]);

/**
 * The default attacker and judge. A managed reflection model serves as is. In
 * local mode a subscription reflection model maps to a model its CLI serves.
 * Any other reflection model (BYOK, or a subscription in a hosted build) falls
 * back to the catalog default, since a hardening job bills managed usage.
 */
export function hardeningDefaultModel(
  reflection: ModelConfig | null | undefined,
  models: CatalogModel[],
  local: boolean,
  status: LocalStatus | null,
): string {
  const name = reflection?.name.trim() ?? "";
  const source = reflection?.token_source;
  if (name && (!source || source === "managed")) return name;
  if (local && name && source === "subscription" && reflection?.cli_provider) {
    const mapped = subscriptionAgentModel(name, reflection.cli_provider, status);
    if (mapped) return mapped;
  }
  return models.find((m) => m.is_default)?.value ?? models[0]?.value ?? "";
}

export type HardeningSettings = Omit<CreateHardeningJobRequest, "draft_id" | "target" | "binding_id">;

/**
 * The run submit's repository target (see repo-run.ts), without secrets: the
 * job row keeps the target as sent, so it never takes a secret value.
 *
 * Args:
 *   repo: The wizard's repository draft, or none for a setup without a repository.
 *
 * Returns:
 *   The target, or null when the setup has no repository.
 *
 * Throws:
 *   Error: Its message is the draft's issue key when the repository is not
 *   valid; a job without its target would harden the wrong metric.
 */
export function hardeningTarget(repo: RepoDraft | null | undefined): BlackboxTarget | null {
  if (!repo) return null;
  const issue = repoIssue(repo);
  if (issue) throw new Error(issue);
  return {
    kind: "repo",
    repo: {
      provider: "github",
      repository: repo.repository.trim(),
      branch: repo.branch.trim() || null,
      commit: repo.commit.trim() || null,
      editable_paths: repo.editablePaths.map((path) => path.trim()).filter(Boolean),
      ...(repo.createdBySkynet ? { created_by_skynet: true } : {}),
    },
    setup_command: repo.setupCommand.trim() || null,
  };
}

export interface HardeningState {
  job: HardeningJob | null;
  /** The first read of the draft's newest job is still out. */
  loading: boolean;
  /** A user action is out; its buttons hold still. */
  busy: boolean;
  start: (settings: HardeningSettings) => Promise<void>;
  verdict: (exploitId: string, confirmed: boolean) => Promise<void>;
  decideFix: (approve: boolean) => Promise<void>;
  retry: () => Promise<void>;
}

// The budget session and the repository draft throw i18n keys, not text.
function errorText(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return message.startsWith("budget.") || message.startsWith("submit.v2.repo.issue.")
    ? msg(message as MessageKey)
    : message;
}

// The setup has no server-side draft id yet, so it keeps one beside its
// answers. A reload finds its job again; a new setup starts a new draft.
function draftId(): string {
  const kept = readSessionValue(WIZARD_HARDENING_KEY);
  if (kept) return kept;
  const id = crypto.randomUUID();
  keepSessionValue(WIZARD_HARDENING_KEY, id);
  return id;
}

/**
 * The setup draft's metric-hardening job: read once, polled while the
 * worker owns it, and changed by the user's verdicts and fix decisions.
 *
 * Args:
 *   active: The hardening screen is on view; polling runs only then.
 *
 * Returns:
 *   The job and its actions.
 */
export function useHardening(active: boolean): HardeningState {
  const [job, setJob] = React.useState<HardeningJob | null>(null);
  const [loaded, setLoaded] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const draft = React.useRef<string | null>(null);
  const { state, ensureBudget } = useWizardV2();
  const repo = state.task.repo;

  React.useEffect(() => {
    if (!active || loaded) return;
    const ctrl = new AbortController();
    draft.current ??= draftId();
    getLatestHardeningJob(draft.current, ctrl.signal)
      .then((latest) => setJob(latest))
      .catch(() => {})
      .finally(() => {
        if (!ctrl.signal.aborted) setLoaded(true);
      });
    return () => ctrl.abort();
  }, [active, loaded]);

  const jobId = job?.id ?? null;
  const polled = job !== null && POLLED.has(job.status);
  React.useEffect(() => {
    if (!active || !jobId || !polled) return;
    const ctrl = new AbortController();
    // The next read waits for the last one, so a slow answer never lands after a newer one.
    let timer = 0;
    const poll = () => {
      timer = window.setTimeout(() => {
        getHardeningJob(jobId, ctrl.signal)
          .then((next) => {
            if (!ctrl.signal.aborted) setJob(next);
          })
          .catch(() => {})
          .finally(() => {
            if (!ctrl.signal.aborted) poll();
          });
      }, POLL_MS);
    };
    poll();
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [active, jobId, polled]);

  const act = React.useCallback(async (call: () => Promise<HardeningJob>) => {
    setBusy(true);
    try {
      setJob(await call());
    } catch (err) {
      if (!(err instanceof Error && err.name === "AbortError")) toast.error(errorText(err));
    } finally {
      setBusy(false);
    }
  }, []);

  const start = React.useCallback(
    (settings: HardeningSettings) => {
      draft.current ??= draftId();
      const id = draft.current;
      return act(async () => {
        const target = hardeningTarget(repo);
        const budget = await ensureBudget();
        try {
          return await createHardeningJob({
            ...settings,
            draft_id: id,
            target,
            binding_id: budget.id,
          });
        } catch (err) {
          // The draft may already hold a job that the first read missed; show it.
          const latest = await getLatestHardeningJob(id).catch(() => null);
          if (latest) setJob(latest);
          throw err;
        }
      });
    },
    [act, ensureBudget, repo],
  );
  const verdict = React.useCallback(
    (exploitId: string, confirmed: boolean) =>
      jobId ? act(() => setHardeningExploitVerdict(jobId, exploitId, confirmed)) : Promise.resolve(),
    [act, jobId],
  );
  const decideFix = React.useCallback(
    (approve: boolean) => (jobId ? act(() => decideHardeningFix(jobId, approve)) : Promise.resolve()),
    [act, jobId],
  );
  const retry = React.useCallback(
    () => (jobId ? act(() => retryHardeningJob(jobId)) : Promise.resolve()),
    [act, jobId],
  );

  return { job, loading: active && !loaded, busy, start, verdict, decideFix, retry };
}
