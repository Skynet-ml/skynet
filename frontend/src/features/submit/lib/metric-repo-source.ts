/**
 * Which source the Metric step's explorer reads: a folder on this computer or
 * a hosted repository (SKY-1001).
 */
import type { RepoHost } from "@/shared/lib/api";
import { isLocalRepo, type RepoDraft } from "./wizard-v2";

export type MetricRepoSource =
  | { kind: "local"; folder: string }
  | { kind: "host"; repository: string; ref: string; host: RepoHost };

/**
 * Choose the explorer's source for a draft.
 *
 * A local draft can still hold an old repository name, so its folder wins.
 *
 * Args:
 *   repo: The valid repository draft, or null when the draft has an issue.
 *
 * Returns:
 *   The local folder, the hosted repository at its commit (or branch), or null when there is nothing to read.
 */
export function metricRepoSource(repo: RepoDraft | null): MetricRepoSource | null {
  if (!repo) return null;
  if (isLocalRepo(repo)) {
    const folder = (repo.localPath ?? "").trim();
    return folder ? { kind: "local", folder } : null;
  }
  const repository = repo.repository.trim();
  if (!repository) return null;
  return {
    kind: "host",
    repository,
    ref: repo.commit.trim() || repo.branch.trim(),
    host: repo.provider ?? "github",
  };
}
