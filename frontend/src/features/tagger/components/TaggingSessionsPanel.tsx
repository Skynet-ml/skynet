"use client";

import * as React from "react";
import { MagnifyingGlass, Plus, Tag, WarningCircle } from "@/shared/ui/icons";
import { toast } from "react-toastify";

import {
  bulkDeleteTaggerSessions,
  listTaggerSessions,
  type TaggerSessionSummary,
} from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { Button } from "@/shared/ui/primitives/button";
import { EmptyState } from "@/shared/ui/empty-state";
import { Ghost, Skeleton } from "@/shared/ui/skeleton";
import { ProgressBar } from "@/shared/ui/progress-bar";
import {
  LIST_TOOLBAR_BUTTON_CLASS,
  ListPageSkeleton,
  type ListRowActionSlot,
} from "@/shared/ui/list-page-skeleton";
import { hintedCount, rememberLayout, useLayoutHint } from "@/shared/lib/layout-hint";
import { SearchField } from "@/shared/ui/search-field";
import { SelectionBar } from "@/shared/ui/selection-bar";
import { TAGGER_SESSIONS_CHANGED } from "../hooks/use-tagger";
import { TaggingSessionCard } from "./TaggingSessionCard";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

/**
 * Let the caller resume a saved labeling session or begin a new one.
 *
 * Fetches its own list and refreshes on {@link TAGGER_SESSIONS_CHANGED}, fired
 * whenever a session is renamed or deleted.
 */
export function TaggingSessionsPanel({ onStartNew }: { onStartNew: () => void }) {
  const [sessions, setSessions] = React.useState<TaggerSessionSummary[]>([]);
  const [loaded, setLoaded] = React.useState(false);
  const [loadFailed, setLoadFailed] = React.useState(false);
  const [loadingSessions, setLoadingSessions] = React.useState(false);
  const [search, setSearch] = React.useState("");
  const [selectedIds, setSelectedIds] = React.useState<Set<string>>(new Set());
  // Last-toggled session id — the shift-click range anchor, kept as an id (not
  // an index) so it survives the search filter reordering the visible list.
  const [anchorId, setAnchorId] = React.useState<string | null>(null);
  const [bulkOpen, setBulkOpen] = React.useState(false);
  const [bulkDeleting, setBulkDeleting] = React.useState(false);

  const fetchSessions = React.useCallback(async () => {
    setLoadingSessions(true);
    try {
      const res = await listTaggerSessions({ limit: 200 });
      const { items } = res;
      setSessions(items);
      rememberLayout(SESSIONS_LAYOUT_KEY, items.length);
      // Drop selections that no longer resolve to a listed owned session
      // (deleted elsewhere, or shared-in and therefore not bulk-deletable),
      // so the bar never counts ghosts.
      setSelectedIds((prev) => {
        const next = new Set(
          [...prev].filter((id) => items.some((s) => s.id === id && s.role === "owner")),
        );
        return next.size === prev.size ? prev : next;
      });
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
    } finally {
      setLoaded(true);
      setLoadingSessions(false);
    }
  }, []);

  const confirmBulkDelete = async () => {
    if (bulkDeleting) return;
    setBulkDeleting(true);
    try {
      const res = await bulkDeleteTaggerSessions([...selectedIds]);
      if (res.skipped.length > 0) {
        toast.warning(formatMsg("shared.selection.delete_skipped", { count: res.skipped.length }));
      }
      setBulkOpen(false);
      setSelectedIds(new Set());
      setAnchorId(null);
      window.dispatchEvent(new Event(TAGGER_SESSIONS_CHANGED));
    } catch (err) {
      toast.error(userErrorMessage(err, msg("datasets.toast.delete_failed")));
    } finally {
      setBulkDeleting(false);
    }
  };

  React.useEffect(() => {
    void fetchSessions();
    const onChanged = () => void fetchSessions();
    window.addEventListener(TAGGER_SESSIONS_CHANGED, onChanged);
    return () => window.removeEventListener(TAGGER_SESSIONS_CHANGED, onChanged);
  }, [fetchSessions]);

  const filteredSessions = React.useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return sessions;
    return sessions.filter((session) => session.name.toLowerCase().includes(query));
  }, [search, sessions]);

  // Same mechanism as the storage cleanup drawer: plain click toggles one row,
  // shift-click applies the clicked row's new state to the whole visible range
  // between it and the previous toggle. Shared-in rows inside a range are
  // skipped — only owned sessions can be bulk-deleted.
  const toggleSelected = React.useCallback(
    (id: string, shiftKey: boolean) => {
      const visible = filteredSessions;
      setSelectedIds((prev) => {
        const next = new Set(prev);
        const willSelect = !prev.has(id);
        const index = visible.findIndex((s) => s.id === id);
        const anchor = anchorId === null ? -1 : visible.findIndex((s) => s.id === anchorId);
        if (shiftKey && anchor !== -1 && index !== -1) {
          const [lo, hi] = anchor < index ? [anchor, index] : [index, anchor];
          for (let i = lo; i <= hi; i++) {
            const row = visible[i];
            if (!row || row.role !== "owner") continue;
            if (willSelect) next.add(row.id);
            else next.delete(row.id);
          }
        } else if (willSelect) {
          next.add(id);
        } else {
          next.delete(id);
        }
        return next;
      });
      setAnchorId(id);
    },
    [filteredSessions, anchorId],
  );

  if (!loaded) {
    return (
      <section className="w-full pb-16" aria-busy="true">
        <SessionsSkeleton />
        <span className="sr-only" role="status">
          {msg("tagger.session.loading")}
        </span>
      </section>
    );
  }

  if (loadFailed && sessions.length === 0) {
    return (
      <section className="w-full pb-16">
        <div className="mt-5 rounded-xl border border-dashed border-transparent" aria-live="polite">
          <EmptyState
            icon={WarningCircle}
            iconWrap="tile"
            title={msg("tagger.session.load_failed")}
            description={msg("tagger.session.load_failed_body")}
            action={{
              label: msg("tagger.session.retry"),
              onClick: fetchSessions,
              iconOnly: true,
              loading: loadingSessions,
            }}
          />
        </div>
      </section>
    );
  }

  if (sessions.length === 0) {
    return (
      <section className="w-full pb-16">
        <div className="mt-5 rounded-xl border border-dashed border-transparent">
          <EmptyState
            icon={Tag}
            iconWrap="tile"
            title={msg("tagger.session.empty_title")}
            description={msg("tagger.session.empty_body")}
            action={{ label: msg("tagger.session.start_new"), onClick: onStartNew }}
          />
        </div>
      </section>
    );
  }

  return (
    <section className="w-full pb-16">
      <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
        <SearchField
          value={search}
          onValueChange={setSearch}
          placeholder={msg("tagger.session.search_placeholder")}
          className="flex-1"
        />
        <Button
          variant="outline"
          onClick={onStartNew}
          className="!h-[44px] w-full shrink-0 rounded-2xl sm:w-auto"
        >
          <Plus className="size-4" aria-hidden="true" />
          {msg("tagger.session.start_new")}
        </Button>
      </div>

      <div className="mt-5 rounded-xl border border-dashed border-transparent">
        {filteredSessions.length === 0 ? (
          <EmptyState icon={MagnifyingGlass} title={msg("tagger.session.search_empty")} />
        ) : (
          <div className="flex flex-col gap-2.5 p-0.5">
            {filteredSessions.map((session) => (
              <TaggingSessionCard
                key={session.id}
                session={session}
                onChanged={fetchSessions}
                selected={selectedIds.has(session.id)}
                onToggleSelect={(shiftKey) => toggleSelected(session.id, shiftKey)}
              />
            ))}
          </div>
        )}
      </div>

      <SelectionBar
        count={selectedIds.size}
        onClear={() => {
          setSelectedIds(new Set());
          setAnchorId(null);
        }}
        onDelete={() => setBulkOpen(true)}
      />

      <ConfirmDialog
        open={bulkOpen}
        onOpenChange={setBulkOpen}
        variant="danger"
        title={msg("tagger.session.bulk_delete_title")}
        description={
          <>
            {formatMsg("tagger.session.bulk_delete_body", { count: selectedIds.size })}{" "}
            {msg("delete.irreversible")}
          </>
        }
        cancelLabel={msg("datasets.delete.cancel")}
        confirmLabel={msg("datasets.delete.confirm")}
        onConfirm={confirmBulkDelete}
        pending={bulkDeleting}
      />
    </section>
  );
}

/** Remembers how many sessions the chooser last listed, for the next skeleton. */
const SESSIONS_LAYOUT_KEY = "tagging-sessions-count";

// An owned session's actions: share, rename | delete.
const SESSION_ACTIONS: readonly ListRowActionSlot[] = ["icon", "icon", "divider", "icon"];

/**
 * The chooser while its sessions load: the search + start-new toolbar and
 * TaggingSessionCard rows (with their progress column), as many as the
 * chooser last held. Nothing when it was empty, since the empty state has
 * no toolbar or rows.
 */
function SessionsSkeleton() {
  const count = hintedCount(useLayoutHint<number>(SESSIONS_LAYOUT_KEY), 5, 12);
  if (count === 0) return null;
  return (
    <ListPageSkeleton
      count={count}
      actions={SESSION_ACTIONS}
      toolbar={
        <Ghost>
          <Button variant="outline" tabIndex={-1} className={LIST_TOOLBAR_BUTTON_CLASS}>
            <Plus className="size-4" />
            {msg("tagger.session.start_new")}
          </Button>
        </Ghost>
      }
      trailing={
        <div className="flex w-20 shrink-0 flex-col items-end gap-1.5">
          <span className="text-xs">
            <Skeleton width={36} />
          </span>
          <ProgressBar value={0} size="sm" />
        </div>
      }
    />
  );
}
