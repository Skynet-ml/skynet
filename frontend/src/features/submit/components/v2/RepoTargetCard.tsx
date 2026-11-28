"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion, useReducedMotion } from "motion/react";
import { toast } from "react-toastify";

import { cn } from "@/shared/lib/utils";
import {
  CircleNotch,
  Desktop,
  FolderOpen,
  FolderPlus,
  Globe,
  Lock,
  Plus,
} from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { Segmented } from "@/shared/ui/segmented";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import { useConnectors } from "@/features/connectors";
import {
  createGithubRepository,
  listGithubBranches,
  previewLocalEnvSecrets,
  SAVED_SECRETS_CHANGED_EVENT,
  type LocalEnvPreview,
  listRepoCommits,
  startConnectorOAuth,
  REPO_HOST_NAMES as HOST_NAMES,
  REPO_HOSTS,
  type RepoCommit,
  type RepoHost,
} from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import type { UnifiedCase } from "@/shared/types/unified-run";
import { rememberOAuthReturn } from "@/shared/lib/oauth-return";
import { userErrorMessage } from "@/shared/lib/user-error";

import { isLocalRepo, withLocalFolder, type RepoDraft } from "../../lib/wizard-v2";
import { v2Msg } from "./ArtifactConfirmCard";
import { ChoiceIconTile, type ChoiceIconGesture } from "./ChoiceIconTile";
import { HostMark, RepoPicker } from "./RepoPicker";
import { InfoTip, RepoSecretsField } from "./RepoSecretsField";

/** GitHub's repository-name rule, the same one the create route checks. */
const REPO_NAME = /^[A-Za-z0-9._-]{1,100}$/;

/** The commit picker's value for "latest on the branch"; Radix Select items cannot use "". */
const LATEST = "__latest__";
// Session key that carries the "Create new" host pick across a sign-in redirect.
const NEW_HOST_KEY = "skynet.submit.repo-new-host.v1";

/**
 * Monospace for a typed branch or SHA only. An empty field shows its translated
 * placeholder, and Hebrew in the code font looks broken, so it keeps the UI font (SKY-633).
 */
const monoWhenSet = (value: string) => (value ? "font-mono" : "");

// The desktop app's folder bridge (SKY-992). It is missing in a browser and in
// older desktop builds, so every call goes through optional chaining.
declare global {
  interface Window {
    skynetDesktop?: {
      /** Open the native folder picker; null when the user cancels. */
      pickFolder?: () => Promise<string | null>;
      /** Make ~/Documents/Skynet/<name>; null when the name is refused or taken. */
      createFolder?: (name: string) => Promise<string | null>;
    };
  }
}

/** Whether this page can offer a folder on this computer as the run source. */
function desktopFolderBridge(): boolean {
  return isLocalMode() && typeof window.skynetDesktop?.pickFolder === "function";
}

/** What a new repository's first commit holds: the task, the scorer and the cases. */
export interface RepoSeed {
  objective: string;
  scorerCode: string;
  cases: UnifiedCase[];
}

/**
 * A repository run's target: which GitHub, GitLab or Bitbucket repository, the branch or pinned
 * commit it starts from. The user picks an existing repository or creates
 * a new one on any of the hosts, seeded with the task. The host link lives in
 * Settings; this offers it when it is missing.
 */
export function RepoTargetCard({
  repo,
  onChange,
  issue,
  guided,
  seed,
  localFolders = false,
}: {
  repo: RepoDraft;
  onChange: (repo: RepoDraft) => void;
  /** An i18n key naming what is missing, or null when the target can run. */
  issue: string | null;
  guided: boolean;
  seed: RepoSeed;
  /** The run starts on this computer, so a local folder can be its source (SKY-993). */
  localFolders?: boolean;
}) {
  const t = (key: string, params?: Record<string, string | number>) => v2Msg(key, guided, params);
  const { byProvider, loading } = useConnectors();
  const isLinked = (target: RepoHost) => {
    const account = byProvider(target);
    return account?.connected === true && account.status !== "invalid";
  };
  // Nothing is preselected: the user (or the agent, through the draft) picks the
  // source and then the host. A draft that already names one opens on it.
  const [source, setSource] = useState<"existing" | "new" | "folder" | null>(
    isLocalRepo(repo) ? "folder" : repo.repository || repo.provider ? "existing" : null,
  );
  // The bridge exists only in the browser, so the first render matches the server's.
  const [hasBridge, setHasBridge] = useState(false);
  useEffect(() => setHasBridge(desktopFolderBridge()), []);
  const offerFolder = localFolders && hasBridge;
  // A folder source stays in the draft when the run moves to the web, so the
  // run-place block can say why; the card shows no folder panel then.
  const shown = source === "folder" && !offerFolder ? null : source;
  const [folderName, setFolderName] = useState("");
  const [folderBusy, setFolderBusy] = useState(false);
  const [hostPicked, setHostPicked] = useState<RepoHost | null>(
    repo.provider ?? (repo.repository ? "github" : null),
  );
  const host: RepoHost = hostPicked ?? "github";
  const linked = hostPicked !== null && isLinked(hostPicked);
  const reduceMotion = useReducedMotion();
  const [connecting, setConnecting] = useState(false);
  const [branches, setBranches] = useState<string[]>([]);
  const [commits, setCommits] = useState<RepoCommit[]>([]);
  const [newName, setNewName] = useState("");
  const [isPrivate, setIsPrivate] = useState(true);
  const [creating, setCreating] = useState(false);
  const patch = (next: Partial<RepoDraft>) => onChange({ ...repo, ...next });
  // The folder dialogs resolve later; by then the draft can hold other edits.
  const latestRepo = useRef(repo);
  useEffect(() => {
    latestRepo.current = repo;
  }, [repo]);
  /** The draft without a local folder, for a git source. */
  const gitRepo = (): RepoDraft => {
    const { localPath: _localPath, envClashes: _clashes, envChoices: _choices, ...rest } = repo;
    return rest;
  };

  // The names in the folder's .env, read again when the folder or the saved
  // secrets change, so the clashes match what the run start imports (SKY-994).
  const folderPath = isLocalRepo(repo) ? (repo.localPath ?? "").trim() : "";
  const [envPreview, setEnvPreview] = useState<LocalEnvPreview | null>(null);
  useEffect(() => {
    if (!folderPath || !offerFolder) {
      setEnvPreview(null);
      return;
    }
    // A new folder must not show the last folder's preview while its own read runs or fails.
    setEnvPreview(null);
    let controller = new AbortController();
    const load = () => {
      controller.abort();
      controller = new AbortController();
      previewLocalEnvSecrets(folderPath, controller.signal)
        .then((preview) => {
          setEnvPreview(preview);
          // Only a read of the file changes the clashes. A missing or failed read
          // keeps a clash the run start found, because that clash still needs a pick.
          if (preview.status !== "found") return;
          const current = latestRepo.current;
          if (!isLocalRepo(current) || (current.localPath ?? "").trim() !== folderPath) return;
          const clashes = preview.entries.filter((entry) => entry.clash).map((entry) => entry.name);
          const choices = Object.fromEntries(
            Object.entries(current.envChoices ?? {}).filter(([name]) => clashes.includes(name)),
          );
          const held = current.envClashes ?? [];
          if (
            held.length === clashes.length &&
            held.every((name, i) => name === clashes[i]) &&
            Object.keys(choices).length === Object.keys(current.envChoices ?? {}).length
          )
            return;
          onChange({ ...current, envClashes: clashes, envChoices: choices });
        })
        .catch(() => {
          // The run start reads the file again and reports a failure there.
        });
    };
    load();
    window.addEventListener(SAVED_SECRETS_CHANGED_EVENT, load);
    return () => {
      controller.abort();
      window.removeEventListener(SAVED_SECRETS_CHANGED_EVENT, load);
    };
    // onChange is a new function on each render; the folder decides what to read.
  }, [folderPath, offerFolder]);

  const pickFolder = async () => {
    setFolderBusy(true);
    try {
      const path = await window.skynetDesktop?.pickFolder?.();
      if (path) onChange(withLocalFolder(latestRepo.current, path));
    } catch {
      toast.error(t("submit.v2.repo.folder.pick_failed"));
    } finally {
      setFolderBusy(false);
    }
  };

  const createFolder = async () => {
    const wanted = folderName.trim();
    if (!wanted) return;
    setFolderBusy(true);
    try {
      const path = await window.skynetDesktop?.createFolder?.(wanted);
      if (!path) throw new Error("folder refused");
      onChange(withLocalFolder(latestRepo.current, path));
      setFolderName("");
    } catch {
      toast.error(t("submit.v2.repo.folder.create_failed"));
    } finally {
      setFolderBusy(false);
    }
  };

  // The draft keeps no host for "Create new", so a sign-in started there saves
  // the pick and the card reopens on it after the redirect.
  useEffect(() => {
    let saved: string | null = null;
    try {
      saved = window.sessionStorage.getItem(NEW_HOST_KEY);
      window.sessionStorage.removeItem(NEW_HOST_KEY);
    } catch {}
    const restored = REPO_HOSTS.find((value) => value === saved);
    if (!restored) return;
    setSource("new");
    setHostPicked(restored);
  }, []);

  const repository = repo.repository.trim();
  useEffect(() => {
    setBranches([]);
    if (!linked || !repository) return;
    let cancelled = false;
    listGithubBranches(repository, host)
      .then((res) => {
        if (!cancelled) setBranches(res.branches);
      })
      .catch(() => {
        // Typing a branch name still works without the list.
      });
    return () => {
      cancelled = true;
    };
  }, [linked, repository, host]);

  const branch = repo.branch.trim();
  useEffect(() => {
    setCommits([]);
    if (!linked || !repository) return;
    let cancelled = false;
    listRepoCommits(repository, branch, host)
      .then((res) => {
        if (!cancelled) setCommits(res.commits);
      })
      .catch(() => {
        // "Latest on the branch" still works without the list.
      });
    return () => {
      cancelled = true;
    };
  }, [linked, repository, host, branch]);
  // A value saved earlier stays selectable when the host list no longer holds it.
  const branchOptions = branch && !branches.includes(branch) ? [branch, ...branches] : branches;
  const commitOptions =
    repo.commit && !commits.some((c) => c.sha === repo.commit)
      ? [{ sha: repo.commit, title: "", date: null }, ...commits]
      : commits;

  // Picking a host that has no linked account starts its sign-in; there is no separate connect card.
  const connect = async (target: RepoHost) => {
    if (loading || connecting || isLinked(target)) return;
    setConnecting(true);
    try {
      const { authorize_url } = await startConnectorOAuth(target);
      rememberOAuthReturn(`${window.location.pathname}${window.location.search}`);
      if (source === "new") {
        try {
          window.sessionStorage.setItem(NEW_HOST_KEY, target);
        } catch {}
      }
      window.location.assign(authorize_url);
    } catch (err) {
      toast.error(
        userErrorMessage(
          err,
          // GitHub keeps its own wording.
          target === "github"
            ? msg("submit.blackbox.repo.connect_failed")
            : msg("submit.v2.repo.host.connect_failed", { host: HOST_NAMES[target] }),
        ),
      );
      setConnecting(false);
    }
  };

  const name = newName.trim();
  const nameInvalid = name !== "" && !REPO_NAME.test(name);
  const objective = seed.objective.trim();
  const canCreate = linked && REPO_NAME.test(name) && objective !== "" && !creating;

  const create = async () => {
    setCreating(true);
    try {
      const created = await createGithubRepository(
        {
          name,
          private: isPrivate,
          objective,
          scorer_code: seed.scorerCode.trim() ? seed.scorerCode : null,
          cases: seed.cases.length > 0 ? seed.cases.map((c) => ({ ...c })) : null,
        },
        host,
      );
      patch({
        provider: host,
        repository: created.full_name,
        branch: created.default_branch,
        commit: "",
        // The run pushes candidate branches only to GitHub; on the other hosts it
        // opens a merge request, as for any repository the user already had.
        createdBySkynet: host === "github",
      });
      toast.success(v2Msg("submit.v2.repo.new.created", guided, { name: created.full_name }));
      setNewName("");
      setSource("existing");
    } catch (err) {
      toast.error(userErrorMessage(err, t("submit.v2.repo.new.failed")));
    } finally {
      setCreating(false);
    }
  };

  return (
    // Before a pick, the two source rows share the height the screen has left,
    // so they line up with the agent chat beside them. A pick lets them settle
    // to their content height and makes room for the host picker below.
    <div className="flex flex-1 flex-col gap-5">
      <div
        role="group"
        aria-label={t("submit.v2.repo.repository_label")}
        className="flex flex-col gap-3 transition-[flex-grow] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none"
        style={{ flexGrow: shown ? 0 : 1 }}
      >
        <SourceCard
          selected={shown === "existing"}
          disabled={folderBusy}
          onSelect={() => {
            if (isLocalRepo(repo)) onChange(gitRepo());
            setSource("existing");
            setHostPicked(repo.provider ?? (repo.repository ? "github" : null));
          }}
          title={t("submit.v2.repo.source.existing")}
          desc={t("submit.v2.repo.source.existing_desc")}
          gesture="open"
          icon={
            <FolderOpen
              className="size-5 transition-transform duration-300 ease-out motion-safe:group-hover:-rotate-12 motion-safe:group-hover:scale-110 motion-reduce:transition-none"
              aria-hidden="true"
            />
          }
        />
        <SourceCard
          selected={shown === "new"}
          disabled={folderBusy}
          onSelect={() => {
            if (isLocalRepo(repo)) onChange(gitRepo());
            setSource("new");
            // The user picks the host next; picking the card starts no sign-in.
            setHostPicked(null);
          }}
          title={t("submit.v2.repo.source.new")}
          desc={t("submit.v2.repo.source.new_desc")}
          gesture="add"
          icon={
            <Plus
              className="size-5 transition-transform duration-300 ease-out motion-safe:group-hover:rotate-90 motion-safe:group-hover:scale-110 motion-reduce:transition-none"
              aria-hidden="true"
            />
          }
        />
        {offerFolder && (
          <SourceCard
            selected={shown === "folder"}
            disabled={folderBusy}
            // The draft changes only when the user picks or creates a folder, so
            // a look at this card keeps the repository and its draft files.
            onSelect={() => setSource("folder")}
            title={t("submit.v2.repo.source.folder")}
            desc={t("submit.v2.repo.source.folder_desc")}
            gesture="boot"
            icon={
              <Desktop
                className="size-5 transition-transform duration-300 ease-out motion-safe:group-hover:scale-110 motion-reduce:transition-none"
                aria-hidden="true"
              />
            }
          />
        )}
      </div>

      {shown === "folder" && (
        <motion.div
          key="folder"
          initial={reduceMotion ? false : { opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
          className="flex flex-col gap-5"
        >
          <div className="flex flex-col gap-3 rounded-xl border border-border p-4">
            <span className="text-sm font-medium text-foreground">
              {t("submit.v2.repo.folder.label")}
            </span>
            <div id="v2-repo-folder-path" aria-live="polite">
              {repo.localPath?.trim() ? (
                <p dir="ltr" className="break-all text-start font-mono text-sm text-foreground">
                  {repo.localPath}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">{t("submit.v2.repo.folder.none")}</p>
              )}
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={() => void pickFolder()}
              disabled={folderBusy}
              aria-describedby="v2-repo-folder-path"
              className="min-h-11 w-full gap-2"
            >
              <FolderOpen className="size-4" aria-hidden="true" />
              {t("submit.v2.repo.folder.choose")}
            </Button>
          </div>
          {typeof window.skynetDesktop?.createFolder === "function" && (
            <div className="flex flex-col gap-3 rounded-xl border border-border p-4">
              <span className="text-sm font-medium text-foreground">
                {t("submit.v2.repo.folder.new")}
              </span>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="v2-repo-folder-name">
                  {t("submit.v2.repo.folder.new_name_label")}
                </Label>
                <Input
                  id="v2-repo-folder-name"
                  value={folderName}
                  onChange={(e) => setFolderName(e.target.value)}
                  placeholder="my-project"
                  autoComplete="off"
                  spellCheck={false}
                  dir="ltr"
                  className={`${TOUCH_FIELD} ${monoWhenSet(folderName)}`}
                />
              </div>
              <Button
                type="button"
                onClick={() => void createFolder()}
                disabled={folderBusy || !folderName.trim()}
                className="min-h-11 w-full gap-2"
              >
                <FolderPlus className="size-4" aria-hidden="true" />
                {t("submit.v2.repo.folder.create")}
              </Button>
            </div>
          )}
          {folderPath && (envPreview || (repo.envClashes ?? []).length > 0) && (
            <EnvSecrets
              preview={envPreview}
              clashes={repo.envClashes ?? []}
              choices={repo.envChoices ?? {}}
              onChoose={(name, choice) =>
                patch({ envChoices: { ...(repo.envChoices ?? {}), [name]: choice } })
              }
              t={t}
            />
          )}
          <RepoSecretsField
            secrets={repo.secrets ?? []}
            onChange={(secrets) => patch({ secrets })}
            t={t}
          />
        </motion.div>
      )}

      {shown && shown !== "folder" && (
        <motion.div
          key={shown}
          initial={reduceMotion ? false : { opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
          className="flex flex-col gap-5"
        >
          <Segmented<RepoHost>
            label={t("submit.v2.repo.host_label")}
            value={hostPicked}
            onChange={(next) => {
              setHostPicked(next);
              if (source === "existing" && next !== repo.provider)
                onChange({
                  ...repo,
                  provider: next,
                  repository: "",
                  branch: "",
                  commit: "",
                  createdBySkynet: false,
                });
              void connect(next);
            }}
            className="w-full"
            options={REPO_HOSTS.map((value) => ({
              value,
              label: HOST_NAMES[value],
              icon: <HostMark host={value} size={14} className="size-3.5" />,
              tip: !isLinked(value)
                ? msg("submit.v2.repo.host.connect", { host: HOST_NAMES[value] })
                : source === "new"
                  ? msg("submit.v2.repo.host.create_tip", { host: HOST_NAMES[value] })
                  : msg("submit.v2.repo.host.pick_tip", { host: HOST_NAMES[value] }),
            }))}
          />

          {source === "new"
            ? linked && (
                <div className="flex flex-col gap-4 rounded-xl border border-border p-4">
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center gap-1">
                      <Label htmlFor="v2-repo-new-name">{t("submit.v2.repo.new.name_label")}</Label>
                      {objective && <InfoTip text={t("submit.v2.repo.new.seed_hint")} />}
                    </div>
                    <Input
                      id="v2-repo-new-name"
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                      placeholder="my-optimized-app"
                      autoComplete="off"
                      spellCheck={false}
                      dir="ltr"
                      aria-invalid={nameInvalid}
                      className={`${TOUCH_FIELD} font-mono`}
                    />
                    {nameInvalid && (
                      <p role="alert" className="text-xs text-destructive">
                        {t("submit.v2.repo.new.name_invalid")}
                      </p>
                    )}
                  </div>
                  <Segmented<"private" | "public">
                    label={t("submit.v2.repo.new.visibility_label")}
                    value={isPrivate ? "private" : "public"}
                    onChange={(value) => setIsPrivate(value === "private")}
                    options={[
                      {
                        value: "private",
                        label: t("submit.v2.repo.new.private"),
                        icon: <Lock className="size-3.5" aria-hidden="true" />,
                      },
                      {
                        value: "public",
                        label: t("submit.v2.repo.new.public"),
                        icon: <Globe className="size-3.5" aria-hidden="true" />,
                      },
                    ]}
                  />
                  {!objective && (
                    <p className="text-xs text-muted-foreground">
                      {t("submit.v2.repo.new.needs_objective")}
                    </p>
                  )}
                  <Button
                    type="button"
                    onClick={() => void create()}
                    disabled={!canCreate}
                    className="min-h-11 w-full gap-2"
                  >
                    {creating ? (
                      <CircleNotch
                        className="size-4 animate-spin motion-reduce:animate-none"
                        aria-hidden="true"
                      />
                    ) : (
                      <HostMark host={host} size={16} className="size-4" />
                    )}
                    {creating ? t("submit.v2.repo.new.creating") : t("submit.v2.repo.new.create")}
                  </Button>
                </div>
              )
            : // With no host account there is nothing to pick from; branch and
              // commit wait for a repository.
              linked && (
                <>
                  <div className="flex flex-col gap-1.5">
                    <span className="text-sm font-medium text-foreground">
                      {t("submit.v2.repo.repository_label")}
                    </span>
                    <RepoPicker
                      key={host}
                      host={host}
                      value={repo.repository}
                      onPick={(picked) =>
                        patch({
                          repository: picked.full_name,
                          branch: picked.default_branch ?? "",
                          commit: "",
                          createdBySkynet: false,
                        })
                      }
                    />
                  </div>

                  {repository && (
                    <div className="grid gap-4 sm:grid-cols-2">
                      <div className="flex flex-col gap-1.5">
                        <Label htmlFor="v2-repo-branch">
                          {msg("submit.blackbox.repo.branch_label")}
                        </Label>
                        {branchOptions.length > 0 ? (
                          <Select
                            value={repo.branch}
                            onValueChange={(next) => patch({ branch: next, commit: "" })}
                          >
                            <SelectTrigger
                              id="v2-repo-branch"
                              dir="ltr"
                              className={`${TOUCH_FIELD} w-full ${monoWhenSet(repo.branch)}`}
                            >
                              <SelectValue
                                placeholder={msg("submit.blackbox.repo.branch_default")}
                              />
                            </SelectTrigger>
                            <SelectContent>
                              {branchOptions.map((name) => (
                                <SelectItem key={name} value={name} className="font-mono">
                                  {name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        ) : (
                          <Input
                            // Uncontrolled until blur: each patch of another branch drops
                            // the draft files, so a keystroke must not send one. The key
                            // resets the field when the branch changes elsewhere.
                            key={repo.branch}
                            id="v2-repo-branch"
                            defaultValue={repo.branch}
                            onBlur={(e) => {
                              if (e.target.value !== repo.branch) patch({ branch: e.target.value });
                            }}
                            placeholder={msg("submit.blackbox.repo.branch_default")}
                            autoComplete="off"
                            spellCheck={false}
                            dir="ltr"
                            className={`${TOUCH_FIELD} ${monoWhenSet(repo.branch)}`}
                          />
                        )}
                      </div>
                      <div className="flex flex-col gap-1.5">
                        <Label htmlFor="v2-repo-commit">{t("submit.v2.repo.commit_label")}</Label>
                        <Select
                          value={repo.commit || LATEST}
                          onValueChange={(next) => patch({ commit: next === LATEST ? "" : next })}
                        >
                          <SelectTrigger
                            id="v2-repo-commit"
                            dir="ltr"
                            className={`${TOUCH_FIELD} w-full`}
                          >
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent className="max-w-sm">
                            <SelectItem value={LATEST}>
                              {t("submit.v2.repo.commit_placeholder")}
                            </SelectItem>
                            {commitOptions.map((c) => (
                              <SelectItem key={c.sha} value={c.sha}>
                                <span className="flex min-w-0 items-center gap-2">
                                  <span className="font-mono text-xs text-muted-foreground">
                                    {c.sha.slice(0, 7)}
                                  </span>
                                  {c.title && <span className="truncate">{c.title}</span>}
                                </span>
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                    </div>
                  )}
                </>
              )}

          {linked && (
            <RepoSecretsField
              secrets={repo.secrets ?? []}
              onChange={(secrets) => patch({ secrets })}
              t={t}
            />
          )}
        </motion.div>
      )}

      {/* Gilad asked to drop the repository hint (SKY-401); the picker already
          says what to type, and the key still blocks Continue. */}
      {issue &&
        issue !== "submit.v2.repo.issue.repository" &&
        // The folder panel's empty state already asks for a folder.
        !(shown === "folder" && issue === "submit.v2.repo.issue.local_path") && (
        <p role="status" className="text-xs text-muted-foreground">
          {/* With no folder card on offer, the user can only go back to a repository. */}
          {t(
            issue === "submit.v2.repo.issue.local_path" && !offerFolder
              ? "submit.v2.run_on.blocked.local_folder"
              : issue,
          )}
        </p>
      )}
    </div>
  );
}

/**
 * What the run start does with the folder's root `.env` file (SKY-994): it
 * saves each pair as a saved secret. A name already saved with another value
 * changes only when the user picks Use .env. Names only; never a value.
 * The clash picks show whatever the preview says: a clash that the run start
 * found later still blocks the run, so its pick must stay on screen.
 */
function EnvSecrets({
  preview,
  clashes,
  choices,
  onChoose,
  t,
}: {
  preview: LocalEnvPreview | null;
  clashes: string[];
  choices: Record<string, "keep" | "use">;
  onChoose: (name: string, choice: "keep" | "use") => void;
  t: (key: string, params?: Record<string, string | number>) => string;
}) {
  const found = preview?.status === "found" ? preview : null;
  const note =
    preview && preview.status !== "found" && preview.status !== "missing" ? (
      <p role="status" className="text-xs text-muted-foreground">
        {t(`submit.v2.repo.env.${preview.status}`)}
      </p>
    ) : null;
  if (clashes.length === 0) {
    if (!found) return note;
    if (found.entries.length === 0 && found.skipped.length === 0) return null;
  }
  const options = [
    { value: "keep" as const, label: t("submit.v2.repo.env.keep") },
    { value: "use" as const, label: t("submit.v2.repo.env.use") },
  ];
  const undecided = clashes.filter((name) => !choices[name]);
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border p-4">
      <span className="text-sm font-medium text-foreground">{t("submit.v2.repo.env.title")}</span>
      {note}
      {found && found.entries.length > 0 && (
        <p className="text-xs text-muted-foreground">{t("submit.v2.repo.env.intro")}</p>
      )}
      {clashes.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-muted-foreground">{t("submit.v2.repo.env.clash_intro")}</p>
          <ul className="flex flex-col gap-2">
            {clashes.map((name) => (
              <li key={name} className="flex flex-wrap items-center justify-between gap-2">
                <span dir="ltr" className="font-mono text-sm text-foreground">
                  {name}
                </span>
                <Segmented
                  size="sm"
                  value={choices[name] ?? null}
                  onChange={(choice) => onChoose(name, choice)}
                  options={options}
                  label={t("submit.v2.repo.env.choice_label", { name })}
                />
              </li>
            ))}
          </ul>
          {undecided.length > 0 && (
            <p role="status" className="text-xs text-destructive">
              {t("submit.v2.repo.env.undecided", { names: undecided.join(", ") })}
            </p>
          )}
        </div>
      )}
      {found && found.skipped.length > 0 && (
        <p className="text-xs text-muted-foreground">
          {t("submit.v2.repo.env.skipped", {
            lines: found.skipped
              .map((skip) => (skip.name ? `${skip.line} (${skip.name})` : String(skip.line)))
              .join(", "),
          })}
        </p>
      )}
    </div>
  );
}

/**
 * One of the two ways to start: a wide pressable row. Hover lifts it, lights a
 * glow under the cursor side and animates its icon; the chosen card keeps a primary
 * border, and its icon tile pops and sends out one ring.
 */
function SourceCard({
  selected,
  onSelect,
  title,
  desc,
  icon,
  gesture,
  disabled = false,
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  desc: string;
  icon: ReactNode;
  gesture: ChoiceIconGesture;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      disabled={disabled}
      className={cn(
        "group relative isolate flex min-h-20 flex-1 cursor-pointer items-center gap-4 overflow-hidden rounded-xl border bg-card px-5 py-4 text-start outline-none",
        "transition-[translate,scale,border-color,box-shadow,background-color] duration-300 ease-out motion-reduce:transition-none",
        "hover:-translate-y-0.5 hover:shadow-[0_10px_30px_-12px] hover:shadow-primary-fill/40 active:translate-y-0 active:scale-[0.99] motion-reduce:hover:translate-y-0",
        "focus-visible:ring-[3px] focus-visible:ring-ring/50",
        "disabled:pointer-events-none disabled:opacity-60",
        selected
          ? "border-primary-fill bg-primary-fill/5 shadow-[0_0_0_1px] shadow-primary-fill/40"
          : "border-border hover:border-primary-fill/50",
      )}
    >
      {/* A soft glow that rises from the card's top corner on hover. */}
      <span
        aria-hidden="true"
        className={cn(
          "pointer-events-none absolute -top-16 -start-16 -z-10 size-48 rounded-full bg-primary-fill/20 blur-3xl transition-opacity duration-500",
          selected ? "opacity-70" : "opacity-0 group-hover:opacity-100",
        )}
      />
      {/* A light sweep crosses the card once on hover. */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-y-0 -start-1/2 -z-10 w-1/3 -skew-x-12 bg-gradient-to-r from-transparent via-foreground/[0.06] to-transparent opacity-0 transition-[inset-inline-start,opacity] duration-700 ease-out group-hover:start-[120%] group-hover:opacity-100 motion-reduce:hidden"
      />
      <ChoiceIconTile selected={selected} gesture={gesture}>
        {icon}
      </ChoiceIconTile>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-sm font-semibold text-foreground">{title}</span>
        <span className="text-xs leading-relaxed text-muted-foreground">{desc}</span>
      </span>
    </button>
  );
}
