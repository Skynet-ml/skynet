"use client";

import * as React from "react";
import Link from "next/link";

import { getStatusLabel } from "@/shared/constants/job-status";
import { runHref } from "@/shared/lib/api-route";
import { formatImprovement, formatLogTimestamp, formatPercent } from "@/shared/lib/formatters";
import { formatMsg, msg } from "@/shared/lib/messages";
import { modelDisplayName } from "@/shared/lib/model-provider";
import { TERMS } from "@/shared/lib/terms";
import { cn } from "@/shared/lib/utils";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import type { AgentToolCall } from "@/shared/ui/agent/types";
import { CopyButton } from "@/shared/ui/copy-button";
import { FileTypeIcon } from "@/shared/ui/file-tree";
import { ArrowUpRight, Folder, Lock, ShieldCheck, Warning } from "@/shared/ui/icons";
import { StatusBadge, StatusDot } from "@/shared/ui/status-badge";

import { resultErrorText, resultObject } from "../lib/setup-tool-results";
import { getToolMeta, getToolTitle } from "../lib/tool-meta";
import {
  flatPairs,
  githubFile,
  githubItem,
  githubRows,
  jobRows,
  linkHref,
  logLines,
  numberedFile,
  openedTarget,
  plainLines,
  registryGroups,
  repoTree,
  settingRows,
  setupCheck,
  statRows,
  statusCounts,
  taggingGuide,
  webLinks,
  webPages,
  workspaceSave,
  writeCounts,
  writtenLines,
  type FileLine,
  type GithubRow,
} from "../lib/tool-results";

import { GainPill } from "./result-card-atoms";
import { ChatSourceContext } from "./JobSummaryCard";

const MAX_ROWS = 8;
const MAX_LINES = 400;
const ERROR_TONE = "text-[var(--danger)]";
const ADDED_TONE = "text-[var(--success)]";
const MONO_BOX =
  "max-h-52 overflow-auto rounded-md border border-border/40 bg-background/70 p-2 pe-9 font-mono text-[0.6875rem]/[1.55] text-foreground/90";

function resultOf(call: AgentToolCall): unknown {
  return call.payload?.result;
}

/** The call's result read once for each result, not again on every render. */
function useResult<T>(call: AgentToolCall, read: (value: unknown) => T): T {
  const result = call.payload?.result;
  return React.useMemo(() => read(result), [read, result]);
}

function argText(call: AgentToolCall, key: string): string {
  const v = resultObject(call.payload?.arguments)?.[key];
  return typeof v === "string" ? v.trim() : typeof v === "number" ? String(v) : "";
}

function moreText(n: number): string {
  return formatMsg("auto.features.agent.panel.components.resultcards.more", { p1: n });
}

function countText(key: Parameters<typeof formatMsg>[0], n: number): string {
  return formatMsg(key, { count: n });
}

function Detail({ parts }: { parts: Array<React.ReactNode | false | null | undefined> }) {
  const shown = parts.filter((p) => p !== false && p !== null && p !== undefined && p !== "");
  if (!shown.length) return null;
  return (
    <span className="inline-flex min-w-0 items-center gap-1">
      {shown.map((p, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span aria-hidden>·</span>}
          {typeof p === "string" ? (
            <span dir="auto" className="min-w-0 truncate">
              {p}
            </span>
          ) : (
            p
          )}
        </React.Fragment>
      ))}
    </span>
  );
}

/** A path in the step line: its file-type icon, then the path in mono. */
function PathDetail({ path, folder = false }: { path: string; folder?: boolean }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1">
      {folder ? (
        <Folder className="size-3 shrink-0 opacity-70" aria-hidden />
      ) : (
        <FileTypeIcon name={path} className="size-3 shrink-0" />
      )}
      <span dir="ltr" className="min-w-0 truncate font-mono text-[0.6875rem]">
        {path}
      </span>
    </span>
  );
}

function LineCounts({ added, removed }: { added: number; removed: number }) {
  return (
    <span dir="ltr" className="shrink-0 font-mono text-[0.6875rem] tabular-nums">
      {added > 0 && <span className={ADDED_TONE}>+{added}</span>}
      {added > 0 && removed > 0 && " "}
      {removed > 0 && <span className="text-destructive">−{removed}</span>}
    </span>
  );
}

/**
 * The frame every card here shares: the tool's icon and title, a failed mark
 * when the call errored or its result did not read, and the error text when
 * the failed row opens.
 */
function ResultStep({
  call,
  ok,
  detail,
  failText,
  children,
}: {
  call: AgentToolCall;
  ok: boolean;
  detail?: React.ReactNode;
  /** The tool's own note for a failure that arrived as a normal result. */
  failText?: string;
  children?: React.ReactNode;
}) {
  const failed = call.status === "error" || (call.status === "done" && !ok);
  const title = getToolTitle(call.tool);
  const errorText = failed ? failText || resultErrorText(resultOf(call)) : "";
  // StepRow opens when it has any children; an array of nulls must not count.
  const body = React.Children.toArray(children).length ? children : undefined;
  return (
    <StepRow
      icon={getToolMeta(call.tool).icon}
      label={title}
      detail={detail}
      status={failed ? "error" : call.status}
      errorLabel={failed ? msg("agent_run.transcript.tool_failed") : undefined}
    >
      {failed ? (
        errorText ? (
          <p dir="auto" className={cn("line-clamp-4 break-words", ERROR_TONE)}>
            {errorText}
          </p>
        ) : null
      ) : (
        body
      )}
    </StepRow>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-muted-foreground/60">{children}</p>;
}

function MoreRow({ count }: { count: number }) {
  return count > 0 ? <li className="text-muted-foreground/60">{moreText(count)}</li> : null;
}

/** A link that leaves the app opens in a new tab; a site path stays in it. */
function OutLink({
  href,
  className,
  children,
}: {
  href: string;
  className?: string;
  children: React.ReactNode;
}) {
  const safe = linkHref(href);
  if (!safe) return <span className={className}>{children}</span>;
  const external = !safe.startsWith("/");
  return (
    <a
      href={safe}
      {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      className={cn(
        "underline-offset-2 transition-colors hover:text-foreground hover:underline",
        className,
      )}
    >
      {children}
      {external && <span className="sr-only"> {msg("agent.tool.new_tab")}</span>}
    </a>
  );
}

/** A file's lines with a line-number gutter, in a scroll box with a copy button. */
function CodeLines({ lines, cut }: { lines: FileLine[]; cut: boolean }) {
  const shown = lines.slice(0, MAX_LINES);
  const text = shown.map((l) => l.text).join("\n");
  const numbered = shown.some((l) => l.n !== null);
  return (
    <div className="relative" dir="ltr">
      <div className={MONO_BOX}>
        <table className="border-collapse">
          <tbody>
            {shown.map((l, i) => (
              <tr key={i}>
                {numbered && (
                  <td className="select-none pe-3 text-end align-top tabular-nums text-muted-foreground/50">
                    {l.n}
                  </td>
                )}
                <td className="whitespace-pre-wrap break-all">{l.text || " "}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(cut || lines.length > shown.length) && (
          <p className="mt-1 font-sans text-muted-foreground/60">{msg("agent.tool.content.cut")}</p>
        )}
      </div>
      <CopyButton
        text={text}
        ariaLabel={msg("auto.features.agent.panel.components.toolcallrow.literal.10")}
        copiedAriaLabel={msg("auto.features.agent.panel.components.toolcallrow.literal.9")}
        stopPropagation
        className="absolute end-1.5 top-1.5"
      />
    </div>
  );
}

/**
 * ``web_search`` and ``search_docs``: the query and how many links came back.
 * Opens to each link's title, its site as plain text, and a short snippet.
 */
export function LinkResultsCard({ call }: { call: AgentToolCall }) {
  const links = useResult(call, webLinks);
  const query = argText(call, "query");
  return (
    <ResultStep
      call={call}
      ok={links !== null}
      detail={
        <Detail
          parts={[
            query && `“${query}”`,
            links &&
              (links.length
                ? countText("agent.tool.results.count", links.length)
                : msg("agent.tool.results.none")),
          ]}
        />
      }
    >
      {links && links.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {links.slice(0, MAX_ROWS).map((l, i) => (
            <li key={`${l.url}-${i}`} className="min-w-0">
              <OutLink href={l.url} className="block truncate font-medium text-foreground/90">
                <span dir="auto">{l.title}</span>
              </OutLink>
              <div dir="ltr" className="truncate text-[0.6875rem] text-muted-foreground/70">
                {l.host || l.url}
              </div>
              {l.snippet && (
                <p dir="auto" className="line-clamp-2 text-muted-foreground/80">
                  {l.snippet}
                </p>
              )}
            </li>
          ))}
          <MoreRow count={links.length - MAX_ROWS} />
        </ul>
      ) : null}
    </ResultStep>
  );
}

/**
 * ``fetch_url`` and ``crawl_site``: the site, and the page title or the page
 * count. Opens to each page's title, URL and a short excerpt.
 */
export function WebPagesCard({ call }: { call: AgentToolCall }) {
  const view = useResult(call, webPages);
  const url = argText(call, "url");
  const crawl = call.tool === "crawl_site";
  const first = view?.pages[0];
  const host = first?.host || url.replace(/^https?:\/\/(www\.)?/, "").split("/")[0];
  return (
    <ResultStep
      call={call}
      ok={view !== null}
      detail={
        <Detail
          parts={[
            host && (
              <span dir="ltr" className="min-w-0 truncate">
                {host}
              </span>
            ),
            crawl ? view && countText("agent.tool.pages.count", view.pages.length) : first?.title,
            view?.partial && msg("agent.tool.pages.partial"),
            view?.budget && msg("agent.tool.pages.budget"),
          ]}
        />
      }
    >
      {view && view.pages.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {view.pages.slice(0, MAX_ROWS).map((p, i) => (
            <li key={i} className="min-w-0">
              {p.title && (
                <OutLink href={p.url} className="block truncate font-medium text-foreground/90">
                  <span dir="auto">{p.title}</span>
                </OutLink>
              )}
              {p.url && (
                <OutLink
                  href={p.title ? "" : p.url}
                  className="block truncate text-[0.6875rem] text-muted-foreground/70"
                >
                  <span dir="ltr">{p.url}</span>
                </OutLink>
              )}
              {p.excerpt && (
                <p dir="auto" className="line-clamp-3 text-muted-foreground/80">
                  {p.excerpt}
                </p>
              )}
              {p.truncated && (
                <p className="text-[0.6875rem] text-muted-foreground/60">
                  {msg("agent.tool.content.cut")}
                </p>
              )}
            </li>
          ))}
          <MoreRow count={view.pages.length - MAX_ROWS} />
        </ul>
      ) : view ? (
        <Empty>{msg("agent.tool.results.none")}</Empty>
      ) : null}
    </ResultStep>
  );
}

/** ``read_repo_file``: the path and its line count. Opens to the numbered lines. */
export function RepoFileCard({ call }: { call: AgentToolCall }) {
  const view = useResult(call, numberedFile);
  const path = argText(call, "path") || call.reason?.trim() || "";
  const file = view?.kind === "file" ? view : null;
  return (
    <ResultStep
      call={call}
      ok={file !== null || view?.kind === "none"}
      failText={view?.kind === "note" ? view.text : undefined}
      detail={
        <Detail
          parts={[
            path && <PathDetail path={path} />,
            file &&
              (file.lines.length
                ? countText("agent.tool.file.lines", file.lines.length)
                : msg("agent.tool.file.empty")),
            file?.cut && msg("agent.tool.content.cut"),
          ]}
        />
      }
    >
      {file && file.lines.length > 0 ? <CodeLines lines={file.lines} cut={file.cut} /> : null}
    </ResultStep>
  );
}

/**
 * ``list_repo_folder``: the folder and how many entries it shows. Opens to
 * the tree, folders first, with the paths the optimizer may change marked.
 */
export function RepoFolderCard({ call }: { call: AgentToolCall }) {
  const view = useResult(call, repoTree);
  const path = (argText(call, "path") || call.reason?.trim() || "").replace(/^\/+|\/+$/g, "") || "/";
  const tree = view?.kind === "tree" ? view : null;
  const items = tree ? tree.entries.filter((e) => e.more === null && e.depth === 0).length : 0;
  return (
    <ResultStep
      call={call}
      ok={view !== null}
      detail={
        <Detail
          parts={[
            <PathDetail key="p" path={path} folder />,
            tree && items > 0 && countText("agent.tool.folder.items", items),
            tree && items === 0 && msg("agent.tool.folder.empty"),
          ]}
        />
      }
    >
      {tree && tree.entries.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <ul dir="ltr" className="max-h-52 overflow-y-auto font-mono text-[0.6875rem]/[1.6]">
            {tree.entries.slice(0, MAX_LINES).map((e, i) => (
              <li
                key={i}
                className="flex min-w-0 items-center gap-1.5"
                style={{ paddingInlineStart: `${e.depth * 0.875}rem` }}
              >
                {e.more !== null ? (
                  <span className="text-muted-foreground/60">{moreText(e.more)}</span>
                ) : (
                  <>
                    {e.dir ? (
                      <Folder className="size-3 shrink-0 opacity-70" aria-hidden />
                    ) : (
                      <FileTypeIcon name={e.name} className="size-3 shrink-0" />
                    )}
                    <span className={cn("min-w-0 truncate", e.dir ? "text-foreground/90" : "")}>
                      {e.name}
                    </span>
                    {e.files !== null && (
                      <span className="shrink-0 font-sans text-muted-foreground/60">
                        {countText("agent.tool.folder.files", e.files)}
                      </span>
                    )}
                    {e.editable && (
                      <span className="shrink-0 rounded bg-primary-fill/15 px-1 font-sans text-[0.625rem] font-medium text-primary">
                        {msg("agent.tool.folder.editable")}
                      </span>
                    )}
                  </>
                )}
              </li>
            ))}
          </ul>
          {tree.cut && <p className="text-muted-foreground/60">{msg("agent.tool.content.cut")}</p>}
          {tree.keyFiles.length > 0 && (
            <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-muted-foreground/70">
              <span>{msg("agent.tool.folder.read_too")}</span>
              {tree.keyFiles.map((f) => (
                <PathDetail key={f} path={f} />
              ))}
            </p>
          )}
        </div>
      ) : null}
    </ResultStep>
  );
}

/**
 * ``write_workspace_file`` and ``edit_workspace_file``: the file and the
 * lines the call added and removed. Opens to the ``optimize.toml`` error when
 * the parser found one, then the lines the call wrote.
 */
export function WorkspaceWriteCard({ call }: { call: AgentToolCall }) {
  const saved = useResult(call, workspaceSave);
  const path = argText(call, "path");
  const args = resultObject(call.payload?.arguments) ?? {};
  const counts = writeCounts(call.tool, args);
  const ok = saved !== null && !("note" in saved);
  const written = ok ? writtenLines(call.tool, args, saved.text) : [];
  return (
    <ResultStep
      call={call}
      ok={ok}
      failText={saved && "note" in saved ? saved.note : undefined}
      detail={
        <Detail
          parts={[
            path && <PathDetail path={path} />,
            ok && (counts.added > 0 || counts.removed > 0) && <LineCounts {...counts} />,
            ok && saved.tomlError && (
              <span className={cn("inline-flex items-center gap-1", ERROR_TONE)}>
                <Warning className="size-3 shrink-0" aria-hidden />
                {msg("agent.tool.check_setup.failed")}
              </span>
            ),
          ]}
        />
      }
    >
      {ok ? (
        <div className="flex flex-col gap-1.5">
          {saved.tomlError && (
            <p dir="auto" className={cn("break-words", ERROR_TONE)}>
              {saved.tomlError.message}
            </p>
          )}
          {written.length > 0 && <CodeLines lines={written} cut={false} />}
        </div>
      ) : null}
    </ResultStep>
  );
}

function GithubFlags({ flags }: { flags: GithubRow["flags"] }) {
  return (
    <>
      {flags.includes("private") && (
        <Lock
          className="size-3 shrink-0 text-muted-foreground"
          role="img"
          aria-label={msg("agent.tool.github_repos.private")}
        />
      )}
      {flags.includes("protected") && (
        <ShieldCheck
          className="size-3 shrink-0 text-muted-foreground"
          role="img"
          aria-label={msg("agent.tool.protected")}
        />
      )}
      {flags.includes("prerelease") && (
        <span className="shrink-0 rounded bg-muted px-1 text-[0.625rem] text-muted-foreground">
          {msg("agent.tool.github.prerelease")}
        </span>
      )}
      {flags.includes("draft") && (
        <span className="shrink-0 rounded bg-muted px-1 text-[0.625rem] text-muted-foreground">
          {msg("agent.tool.github.draft")}
        </span>
      )}
    </>
  );
}

/** ``paths``: the rows are files and folders, so each gets its type icon. */
function GithubRowView({ row, paths = false }: { row: GithubRow; paths?: boolean }) {
  return (
    <li className="min-w-0">
      <div className="flex min-w-0 items-center gap-1.5">
        {row.sha && (
          <code dir="ltr" className="shrink-0 font-mono text-[0.6875rem] text-muted-foreground/70">
            {row.sha}
          </code>
        )}
        {paths &&
          (row.flags.includes("dir") ? (
            <Folder className="size-3 shrink-0 opacity-70" aria-hidden />
          ) : (
            <FileTypeIcon name={row.title} className="size-3 shrink-0" />
          ))}
        <OutLink href={row.href} className="min-w-0 truncate font-medium text-foreground/90">
          <span dir="auto">{row.title}</span>
        </OutLink>
        <GithubFlags flags={row.flags} />
        {row.meta.length > 0 && (
          <span dir="auto" className="shrink-0 text-muted-foreground/70">
            {row.meta.join(" · ")}
          </span>
        )}
      </div>
      {row.subtitle && (
        <p dir="auto" className="truncate text-muted-foreground/70" title={row.subtitle}>
          {row.subtitle}
        </p>
      )}
    </li>
  );
}

const ITEM_TOOLS = ["get_commit", "get_tag", "get_latest_release", "get_release_by_tag"];

/** The repository or ref a GitHub tool call names, for its step line. */
function githubTarget(call: AgentToolCall): string {
  const owner = argText(call, "owner");
  const repo = argText(call, "repo");
  const ref =
    argText(call, "path") ||
    argText(call, "sha") ||
    argText(call, "tag") ||
    argText(call, "branch");
  return [owner && repo ? `${owner}/${repo}` : repo, ref].filter(Boolean).join(" · ");
}

/**
 * The read-only GitHub repository tools: the repository or search, and how
 * many rows came back. Opens to the rows: repositories, code paths, commits,
 * branches, tags or releases. ``get_file_contents`` opens to the file's lines
 * or the folder's entries; a single commit opens to its changed files.
 */
export function GithubToolCard({ call }: { call: AgentToolCall }) {
  const result = resultOf(call);
  const tool = call.tool;
  const isItem = ITEM_TOOLS.includes(tool);
  const file = React.useMemo(
    () => (tool === "get_file_contents" ? githubFile(result) : null),
    [tool, result],
  );
  const item = React.useMemo(
    () => (isItem ? githubItem(tool, result) : null),
    [isItem, tool, result],
  );
  const list = React.useMemo(
    () => (isItem || file ? null : githubRows(tool, result)),
    [isItem, file, tool, result],
  );
  const query = argText(call, "query");
  const target = githubTarget(call);
  const where = query ? `“${query}”` : target;
  const whereNode = where && (
    <span dir={query ? "auto" : "ltr"} className="min-w-0 truncate">
      {where}
    </span>
  );

  if (file) {
    const lines = plainLines(file.body);
    const path = argText(call, "path");
    return (
      <ResultStep
        call={call}
        ok
        detail={
          <Detail
            parts={[
              path ? <PathDetail path={path} /> : whereNode,
              file.binary
                ? msg("agent.tool.file.binary")
                : lines.length
                  ? countText("agent.tool.file.lines", lines.length)
                  : msg("agent.tool.file.empty"),
              file.cut && msg("agent.tool.content.cut"),
            ]}
          />
        }
      >
        {lines.length > 0 ? <CodeLines lines={lines} cut={file.cut} /> : null}
      </ResultStep>
    );
  }

  if (isItem) {
    const text = item?.kind === "text" ? item : null;
    const one = item?.kind === "item" ? item : null;
    return (
      <ResultStep
        call={call}
        ok={item !== null}
        detail={
          <Detail
            parts={[
              whereNode,
              one && <span className="min-w-0 truncate">{one.title}</span>,
              one?.added != null &&
                one.removed != null &&
                (one.added > 0 || one.removed > 0) && (
                  <LineCounts added={one.added} removed={one.removed} />
                ),
              text?.cut && msg("agent.tool.content.cut"),
            ]}
          />
        }
      >
        {text ? (
          <CodeLines lines={plainLines(text.text)} cut={text.cut} />
        ) : one ? (
          <div className="flex flex-col gap-1.5">
            <ul className="flex flex-col">
              <GithubRowView row={{ ...one, key: one.title, subtitle: "" }} />
            </ul>
            {one.subtitle && (
              <p dir="auto" className="line-clamp-4 whitespace-pre-line text-muted-foreground/80">
                {one.subtitle}
              </p>
            )}
            {one.files.length > 0 && (
              <ul className="flex flex-col gap-0.5">
                {one.files.slice(0, MAX_ROWS).map((f) => (
                  <li key={f.path} className="flex min-w-0 items-center gap-1.5">
                    <PathDetail path={f.path} />
                    <LineCounts added={f.added} removed={f.removed} />
                  </li>
                ))}
                <MoreRow count={one.files.length - MAX_ROWS} />
              </ul>
            )}
          </div>
        ) : null}
      </ResultStep>
    );
  }

  const count = list
    ? list.kind === "text"
      ? list.cut && msg("agent.tool.content.cut")
      : list.rows.length === 0
        ? msg("agent.tool.results.none")
        : list.total !== null && list.total > list.rows.length
          ? formatMsg("agent.tool.shown_of_total", { shown: list.rows.length, total: list.total })
          : countText("agent.tool.results.count", list.rows.length)
    : null;
  return (
    <ResultStep call={call} ok={list !== null} detail={<Detail parts={[whereNode, count]} />}>
      {list?.kind === "text" ? (
        <CodeLines lines={plainLines(list.text)} cut={list.cut} />
      ) : list && list.rows.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <ul className="flex flex-col gap-1.5">
            {list.rows.slice(0, MAX_ROWS).map((r) => (
              <GithubRowView
                key={r.key}
                row={r}
                paths={call.tool === "search_code" || call.tool === "get_file_contents"}
              />
            ))}
            <MoreRow count={list.rows.length - MAX_ROWS} />
          </ul>
          {list.cut && <p className="text-muted-foreground/60">{msg("agent.tool.content.cut")}</p>}
        </div>
      ) : null}
    </ResultStep>
  );
}

const LEVEL_TONE: Record<string, string> = {
  ERROR: ERROR_TONE,
  CRITICAL: ERROR_TONE,
  WARNING: "text-[var(--warning)]",
};

/** ``get_job_logs``: how many log lines came back. Opens to the newest lines in a mono block. */
export function JobLogsCard({ call }: { call: AgentToolCall }) {
  const lines = useResult(call, logLines);
  const id = argText(call, "optimization_id") || argText(call, "job_id");
  const shown = lines ? lines.slice(-MAX_LINES) : [];
  const text = shown.map((l) => `${l.time} ${l.level} ${l.message}`).join("\n");
  return (
    <ResultStep
      call={call}
      ok={lines !== null}
      detail={
        <Detail
          parts={[
            id && (
              <span dir="ltr" className="font-mono text-[0.6875rem]">
                {id.slice(0, 8)}
              </span>
            ),
            lines &&
              (lines.length
                ? countText("agent.tool.logs.count", lines.length)
                : msg("agent.tool.logs.none")),
          ]}
        />
      }
    >
      {shown.length > 0 ? (
        <div className="relative" dir="ltr">
          <div className={MONO_BOX}>
            {shown.map((l, i) => (
              <div key={i} className="flex min-w-0 gap-2">
                {l.time && (
                  <span className="shrink-0 tabular-nums text-muted-foreground/50">
                    {formatLogTimestamp(l.time)}
                  </span>
                )}
                {l.level && (
                  <span
                    className={cn(
                      "w-11 shrink-0",
                      LEVEL_TONE[l.level] ?? "text-muted-foreground/70",
                    )}
                  >
                    {l.level.slice(0, 5)}
                  </span>
                )}
                <span className="min-w-0 whitespace-pre-wrap break-words">{l.message}</span>
              </div>
            ))}
          </div>
          <CopyButton
            text={text}
            ariaLabel={msg("auto.features.agent.panel.components.toolcallrow.literal.10")}
            copiedAriaLabel={msg("auto.features.agent.panel.components.toolcallrow.literal.9")}
            stopPropagation
            className="absolute end-1.5 top-1.5"
          />
        </div>
      ) : null}
    </ResultStep>
  );
}

/**
 * ``list_jobs``: how many runs came back. Opens to each run's name (a link to
 * it), status, model and gain.
 */
export function JobListCard({ call }: { call: AgentToolCall }) {
  const list = useResult(call, jobRows);
  const source = React.useContext(ChatSourceContext);
  const count = list
    ? list.total !== null && list.total > list.rows.length
      ? formatMsg("agent.tool.shown_of_total", { shown: list.rows.length, total: list.total })
      : countText("agent.tool.runs.count", list.rows.length)
    : null;
  return (
    <ResultStep call={call} ok={list !== null} detail={<Detail parts={[count]} />}>
      {list && list.rows.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {list.rows.slice(0, MAX_ROWS).map((r) => (
            <li key={r.id} className="min-w-0">
              <div className="flex min-w-0 items-center gap-1.5">
                <Link
                  href={runHref(r.id, source)}
                  className="min-w-0 truncate font-medium text-foreground/90 underline-offset-2 hover:underline"
                >
                  <span dir="auto">{r.name || r.id.slice(0, 8)}</span>
                </Link>
                {r.status && <StatusBadge status={r.status} compact />}
                <span className="ms-auto shrink-0">
                  <GainPill baseline={r.baseline} optimized={r.optimized} />
                </span>
              </div>
              {(r.model || r.optimizer) && (
                <p dir="ltr" className="truncate text-start text-muted-foreground/70">
                  {[r.model && modelDisplayName(r.model), r.optimizer].filter(Boolean).join(" · ")}
                </p>
              )}
            </li>
          ))}
          <MoreRow count={list.rows.length - MAX_ROWS} />
        </ul>
      ) : list ? (
        <Empty>{msg("agent.tool.results.none")}</Empty>
      ) : null}
    </ResultStep>
  );
}

/**
 * ``get_optimizer_stats`` and ``get_model_stats``: how many optimizers or
 * models. Opens to one row each: runs, success rate and average gain.
 */
export function UsageStatsCard({ call }: { call: AgentToolCall }) {
  const view = useResult(call, statRows);
  return (
    <ResultStep
      call={call}
      ok={view !== null}
      detail={
        <Detail
          parts={[
            view &&
              (view.rows.length
                ? countText("agent.tool.results.count", view.rows.length)
                : msg("agent.tool.results.none")),
            view?.truncated &&
              msg("auto.features.agent.panel.components.analyticssummarycard.sampled"),
          ]}
        />
      }
    >
      {view && view.rows.length > 0 ? (
        <table className="w-full table-fixed border-collapse">
          <thead>
            <tr className="text-[0.625rem] uppercase tracking-wide text-muted-foreground/60">
              <th className="w-[40%] pb-1 text-start font-normal">
                <span className="sr-only">{msg("agent.tool.stats.name")}</span>
              </th>
              <th className="pb-1 text-end font-normal">{TERMS.optimizationTypeRunPlural}</th>
              <th className="pb-1 text-end font-normal">
                {msg("auto.features.agent.panel.components.analyticssummarycard.success_rate")}
              </th>
              <th className="pb-1 text-end font-normal">
                {msg("auto.features.agent.panel.components.analyticssummarycard.avg_improvement")}
              </th>
            </tr>
          </thead>
          <tbody className="tabular-nums">
            {view.rows.slice(0, MAX_ROWS).map((r) => (
              <tr key={r.name}>
                <td dir="ltr" className="truncate py-0.5 text-start text-foreground/90">
                  {modelDisplayName(r.name)}
                </td>
                <td dir="ltr" className="py-0.5 text-end">
                  {r.runs ?? "—"}
                </td>
                <td dir="ltr" className="py-0.5 text-end">
                  {formatPercent(r.successRate)}
                </td>
                <td dir="ltr" className="py-0.5 text-end">
                  {formatImprovement(r.avgGain)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {view && view.rows.length > MAX_ROWS && (
        <p className="text-muted-foreground/60">{moreText(view.rows.length - MAX_ROWS)}</p>
      )}
    </ResultStep>
  );
}

/** ``get_optimization_counts``: the total. Opens to the count of each status. */
export function StatusCountsCard({ call }: { call: AgentToolCall }) {
  const view = useResult(call, statusCounts);
  return (
    <ResultStep
      call={call}
      ok={view !== null}
      detail={
        <Detail parts={[view && formatMsg("agent.tool.counts.total", { count: view.total })]} />
      }
    >
      {view && view.counts.length > 0 ? (
        <ul className="flex flex-wrap gap-x-3 gap-y-1">
          {view.counts.map(([status, n]) => (
            <li key={status} className="inline-flex items-center gap-1.5">
              <StatusDot status={status} size="sm" />
              <span>{getStatusLabel(status)}</span>
              <span dir="ltr" className="tabular-nums text-foreground/90">
                {n}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </ResultStep>
  );
}

const REGISTRY_LABEL: Record<string, Parameters<typeof msg>[0]> = {
  modules: "agent.tool.registry.modules",
  metrics: "agent.tool.registry.metrics",
  optimizers: "agent.tool.registry.optimizers",
};

function registryLabel(kind: string): string {
  const key = REGISTRY_LABEL[kind];
  return key ? msg(key) : kind;
}

/** ``get_registry_snapshot``: the names of each kind, as small mono chips. */
export function RegistryCard({ call }: { call: AgentToolCall }) {
  const groups = useResult(call, registryGroups);
  return (
    <ResultStep
      call={call}
      ok={groups !== null}
      detail={
        <Detail
          parts={groups?.map(([kind, names]) => `${registryLabel(kind)} ${names.length}`) ?? []}
        />
      }
    >
      {groups && groups.length > 0 ? (
        <dl className="flex flex-col gap-1.5">
          {groups.map(([kind, names]) => (
            <div key={kind} className="min-w-0">
              <dt className="text-[0.625rem] uppercase tracking-wide text-muted-foreground/60">
                {registryLabel(kind)}
              </dt>
              <dd dir="ltr" className="flex flex-wrap gap-1">
                {names.slice(0, 24).map((n, i) => (
                  <code
                    key={`${i}-${n}`}
                    className="rounded bg-muted/60 px-1 font-mono text-[0.6875rem] text-foreground/85"
                  >
                    {n}
                  </code>
                ))}
                {names.length > 24 && (
                  <span className="text-muted-foreground/60">{moreText(names.length - 24)}</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </ResultStep>
  );
}

/** Label and value pairs, keys in mono, for a settings read or a reply. */
function Pairs({ pairs }: { pairs: Array<[React.ReactNode, React.ReactNode]> }) {
  return (
    <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-0.5">
      {pairs.map(([k, v], i) => (
        <React.Fragment key={i}>
          <dt className="truncate text-muted-foreground/70">{k}</dt>
          <dd className="min-w-0 truncate text-foreground/85">{v}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

function monoPairs(pairs: Array<[string, string]>): Array<[React.ReactNode, React.ReactNode]> {
  return pairs.map(([k, v]) => [
    <code key="k" dir="ltr" className="font-mono text-[0.6875rem]">
      {k}
    </code>,
    <span key="v" dir="auto">
      {v}
    </span>,
  ]);
}

/** ``serve_info``: whether serving is on, and the endpoint, model and optimizer. */
export function ServeInfoCard({ call }: { call: AgentToolCall }) {
  const r = useResult(call, resultObject);
  const on = r?.serving_enabled !== false;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  const endpoint = str(r?.endpoint_url);
  const pairs: Array<[React.ReactNode, React.ReactNode]> = [];
  if (endpoint)
    pairs.push([
      msg("agent.tool.serve.endpoint"),
      <code key="e" dir="ltr" className="font-mono text-[0.6875rem]">
        {endpoint}
      </code>,
    ]);
  if (str(r?.model_name))
    pairs.push([
      TERMS.model,
      <span key="m" dir="ltr">
        {modelDisplayName(str(r?.model_name))}
      </span>,
    ]);
  if (str(r?.optimizer_name))
    pairs.push([
      TERMS.optimizer,
      <span key="o" dir="ltr">
        {str(r?.optimizer_name)}
      </span>,
    ]);
  if (str(r?.module_name))
    pairs.push([
      TERMS.module,
      <span key="d" dir="ltr">
        {str(r?.module_name)}
      </span>,
    ]);
  return (
    <ResultStep
      call={call}
      ok={r !== null}
      detail={
        r ? (
          <span className="inline-flex items-center gap-1.5">
            <StatusDot status={on ? "success" : "cancelled"} size="sm" />
            {msg(on ? "serve_tab.status.on" : "serve_tab.status.off")}
          </span>
        ) : undefined
      }
    >
      {pairs.length > 0 ? <Pairs pairs={pairs} /> : null}
    </ResultStep>
  );
}

/** ``list_settings``: how many settings areas. Opens to each area's key, tab and summary. */
export function SettingsListCard({ call }: { call: AgentToolCall }) {
  const rows = useResult(call, settingRows);
  return (
    <ResultStep
      call={call}
      ok={rows !== null}
      detail={<Detail parts={[rows && countText("agent.tool.settings.count", rows.length)]} />}
    >
      {rows && rows.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {rows.map((r) => (
            <li key={r.key} className="min-w-0">
              <div className="flex min-w-0 items-center gap-1.5">
                <code dir="ltr" className="shrink-0 font-mono text-[0.6875rem] text-foreground/90">
                  {r.key}
                </code>
                {!r.changeable && (
                  <span className="shrink-0 text-muted-foreground/60">
                    {msg("agent.tool.settings.read_only")}
                  </span>
                )}
                {r.protectedActions > 0 && (
                  <ShieldCheck
                    className="size-3 shrink-0 text-muted-foreground"
                    role="img"
                    aria-label={msg("agent.tool.protected")}
                  />
                )}
              </div>
              {r.summary && (
                <p dir="auto" className="truncate text-muted-foreground/70" title={r.summary}>
                  {r.summary}
                </p>
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </ResultStep>
  );
}

/**
 * ``get_setting``, ``change_setting`` and ``change_protected_setting``: the
 * settings area or action. Opens to the values read or changed, secrets
 * masked as the route returns them, or the route's note.
 */
export function SettingValuesCard({ call }: { call: AgentToolCall }) {
  const r = useResult(call, resultObject);
  const key = argText(call, "key") || argText(call, "action");
  const target = argText(call, "target");
  // Only the server's reply (secrets masked) or the client patch: never the
  // call's own values, which can hold a secret the user typed.
  const body = r && "result" in r ? r.result : r && "client_patch" in r ? r.client_patch : null;
  const note = typeof r?.note === "string" ? r.note.trim() : "";
  const shown = flatPairs(body);
  return (
    <ResultStep
      call={call}
      ok={r !== null && r.ok !== false}
      detail={
        <Detail
          parts={[
            key && (
              <code dir="ltr" className="font-mono text-[0.6875rem]">
                {key}
              </code>
            ),
            target && (
              <span dir="ltr" className="min-w-0 truncate">
                {target}
              </span>
            ),
          ]}
        />
      }
    >
      {shown.length > 0 || note ? (
        <div className="flex flex-col gap-1">
          {note && <p dir="auto">{note}</p>}
          {shown.length > 0 && <Pairs pairs={monoPairs(shown)} />}
        </div>
      ) : null}
    </ResultStep>
  );
}

/** ``open_page``: the settings tab or app path the browser opened. */
export function OpenPageCard({ call }: { call: AgentToolCall }) {
  const opened = useResult(call, openedTarget);
  const fallback = argText(call, "tab") || argText(call, "path");
  const target = opened?.target || fallback;
  return (
    <ResultStep
      call={call}
      ok={opened !== null}
      detail={
        target ? (
          <span
            dir="ltr"
            className="inline-flex min-w-0 items-center gap-1 font-mono text-[0.6875rem]"
          >
            <span className="min-w-0 truncate">{target}</span>
            {opened?.kind === "route" && (
              <ArrowUpRight className="size-3 shrink-0 rtl:-scale-x-100" aria-hidden />
            )}
          </span>
        ) : undefined
      }
    />
  );
}

/**
 * ``get_tagging_guide_for_agent`` and ``save_tagging_guide_for_agent``: the
 * labeling session, its categories and rule count. Opens to its question and
 * categories.
 */
export function TaggingGuideCard({ call }: { call: AgentToolCall }) {
  const guide = useResult(call, taggingGuide);
  return (
    <ResultStep
      call={call}
      ok={guide !== null}
      detail={
        guide ? (
          <Detail
            parts={[
              guide.name,
              guide.categories.length > 0 &&
                countText("agent.tool.tagging_guide.categories", guide.categories.length),
              guide.rules > 0 && countText("agent.tool.tagging_guide.rules", guide.rules),
            ]}
          />
        ) : undefined
      }
    >
      {guide && (guide.question || guide.categories.length > 0) ? (
        <div className="flex flex-col gap-1.5">
          {guide.question && (
            <p dir="auto" className="line-clamp-3 text-foreground/85">
              {guide.question}
            </p>
          )}
          {guide.categories.length > 0 && (
            <ul className="flex flex-wrap gap-1">
              {guide.categories.slice(0, 16).map((c, i) => (
                <li
                  key={`${i}-${c}`}
                  dir="auto"
                  className="rounded bg-muted/60 px-1.5 text-[0.6875rem] text-foreground/85"
                >
                  {c}
                </li>
              ))}
              {guide.categories.length > 16 && (
                <li className="text-muted-foreground/60">
                  {moreText(guide.categories.length - 16)}
                </li>
              )}
            </ul>
          )}
        </div>
      ) : null}
    </ResultStep>
  );
}

/** ``check_setup``: whether ``optimize.toml`` is valid. Opens to each error and its place. */
export function SetupCheckCard({ call }: { call: AgentToolCall }) {
  const check = useResult(call, setupCheck);
  return (
    <ResultStep
      call={call}
      ok={check !== null}
      detail={
        check ? (
          check.ok ? (
            <span>{msg("agent.tool.check_setup.ok")}</span>
          ) : (
            <span className={cn("inline-flex items-center gap-1", ERROR_TONE)}>
              <Warning className="size-3 shrink-0" aria-hidden />
              {msg("agent.tool.check_setup.failed")}
            </span>
          )
        ) : undefined
      }
    >
      {check && check.errors.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {check.errors.map((e, i) => (
            <li key={i} className="min-w-0">
              {e.where && (
                <code dir="ltr" className="me-1.5 font-mono text-[0.6875rem] text-foreground/80">
                  [{e.where}]
                </code>
              )}
              <span dir="auto" className={ERROR_TONE}>
                {e.message}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </ResultStep>
  );
}

/**
 * ``create_github_repository``: the new repository (a link to it), its
 * default branch and first commit.
 */
export function CreatedRepoCard({ call }: { call: AgentToolCall }) {
  const r = useResult(call, resultObject);
  const name = typeof r?.full_name === "string" ? r.full_name : "";
  const branch = typeof r?.default_branch === "string" ? r.default_branch : "";
  const commit = typeof r?.commit === "string" ? r.commit.slice(0, 7) : "";
  return (
    <ResultStep
      call={call}
      ok={!!name}
      detail={
        name ? (
          <span dir="ltr" className="inline-flex min-w-0 items-center gap-1.5">
            <OutLink href={`https://github.com/${name}`} className="min-w-0 truncate font-medium">
              {name}
            </OutLink>
            {branch && <span className="shrink-0 text-muted-foreground/70">{branch}</span>}
            {commit && (
              <code className="shrink-0 font-mono text-[0.6875rem] text-muted-foreground/70">
                {commit}
              </code>
            )}
          </span>
        ) : undefined
      }
    />
  );
}

/**
 * ``pause_job``, ``resume_job`` and ``restart_job``: the run's short id and
 * the status the call left it in.
 */
export function RunLifecycleCard({ call }: { call: AgentToolCall }) {
  const r = useResult(call, resultObject);
  const status = typeof r?.status === "string" ? r.status : "";
  const id = argText(call, "optimization_id") || argText(call, "id");
  return (
    <ResultStep
      call={call}
      ok={!!status}
      detail={
        <Detail
          parts={[
            id && (
              <span dir="ltr" className="font-mono text-[0.6875rem]">
                {id.slice(0, 8)}
              </span>
            ),
            status && <StatusBadge status={status} compact />,
          ]}
        />
      }
    />
  );
}

/** Calls ``card`` during render, so the boundary above also catches a throw in it. */
function CardBody({
  call,
  card,
}: {
  call: AgentToolCall;
  card: (call: AgentToolCall) => React.ReactNode;
}) {
  return <>{card(call)}</>;
}

/**
 * Keeps one bad card from breaking the panel: a card that throws shows the
 * tool's step line with a short note instead.
 */
export class ToolCardBoundary extends React.Component<
  { call: AgentToolCall; card: (call: AgentToolCall) => React.ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: Error) {
    console.error(`Tool card ${this.props.call.tool} failed to render`, error);
  }

  // A new call or a new result may render fine, so try the card again.
  componentDidUpdate(prev: { call: AgentToolCall }) {
    const { call } = this.props;
    if (
      this.state.failed &&
      (prev.call.id !== call.id || prev.call.payload?.result !== call.payload?.result)
    )
      this.setState({ failed: false });
  }

  render() {
    const { call, card } = this.props;
    if (!this.state.failed) return <CardBody call={call} card={card} />;
    return (
      <StepRow
        icon={getToolMeta(call.tool).icon}
        label={getToolTitle(call.tool)}
        detail={<span className="text-muted-foreground/70">{msg("agent.tool.card_error")}</span>}
        status={call.status}
        errorLabel={msg("agent_run.transcript.tool_failed")}
      />
    );
  }
}
