"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useRouter } from "next/navigation";
import { Popover as PopoverPrimitive } from "radix-ui";
import { toast } from "react-toastify";
import {
  ArrowRight,
  CaretRight,
  Check,
  CircleNotch,
  DotsThree,
  Folder,
  FolderOpen,
  FolderPlus,
  FunnelSimple,
  PencilSimple,
  Plus,
  ShareNetwork,
  Trash,
  Users,
} from "@/shared/ui/icons";
import { cn } from "@/shared/lib/utils";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/primitives/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import {
  COMPACT_POPOVER_ICON_CLASS,
  COMPACT_POPOVER_ITEM_CLASS,
  COMPACT_POPOVER_PANEL_CLASS,
} from "@/shared/ui/compact-popover-menu";
import {
  createFolder,
  deleteFolder,
  listFolderRuns,
  moveFolder,
  moveRunsToFolder,
  updateFolder,
  type RunFolder,
  type SidebarJobItem,
} from "@/shared/lib/api";
import {
  DEFAULT_RUN_VIEW,
  applyRunView,
  isFiltered,
  sortFolders,
  type RunView,
} from "../lib/run-view";
import { ROW_MOTION, rowTransition } from "../lib/row-motion";
import { runKey } from "../lib/merge-runs";
import { FolderShareDialog } from "./FolderShareDialog";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { SharedItemMenuItems, useSharedItemActions } from "@/shared/ui/shared-item-menu";

/** A folder action the sidebar opens a dialog for. */
export type FolderDialogState =
  | { kind: "create"; parentId: string | null }
  | { kind: "rename"; folder: RunFolder }
  | { kind: "share"; folder: RunFolder }
  | { kind: "move-folder"; folder: RunFolder }
  | { kind: "delete"; folder: RunFolder }
  | { kind: "move-runs"; optimizationIds: string[]; currentFolderId: string | null };

const canEditFolder = (folder: RunFolder) => folder.role === "owner" || folder.role === "editor";

/** Map each folder id to its visible child folders. */
export function childrenByParent(folders: RunFolder[]): Map<string | null, RunFolder[]> {
  const ids = new Set(folders.map((f) => f.id));
  const map = new Map<string | null, RunFolder[]>();
  for (const folder of folders) {
    const parent = folder.parent_id && ids.has(folder.parent_id) ? folder.parent_id : null;
    const list = map.get(parent) ?? [];
    list.push(folder);
    map.set(parent, list);
  }
  return map;
}

// ---------------------------------------------------------------------------
// Filter and sort menu
// ---------------------------------------------------------------------------

interface ViewRow {
  key: keyof RunView;
  label: string;
  options: Array<{ value: string; label: string }>;
}

function viewRows(): ViewRow[] {
  return [
    {
      key: "activity",
      label: msg("sidebar.filter.activity"),
      options: [
        { value: "all", label: msg("sidebar.filter.activity.all") },
        { value: "7", label: msg("sidebar.filter.activity.7") },
        { value: "30", label: msg("sidebar.filter.activity.30") },
        { value: "90", label: msg("sidebar.filter.activity.90") },
      ],
    },
    {
      key: "status",
      label: msg("sidebar.filter.status"),
      options: [
        { value: "all", label: msg("sidebar.filter.all") },
        { value: "active", label: msg("sidebar.filter.status.active") },
        { value: "success", label: msg("sidebar.filter.status.success") },
        { value: "stopped", label: msg("sidebar.filter.status.stopped") },
      ],
    },
    {
      key: "type",
      label: msg("sidebar.filter.type"),
      options: [
        { value: "all", label: msg("sidebar.filter.all") },
        { value: "run", label: msg("sidebar.filter.type.run") },
        { value: "grid_search", label: msg("sidebar.filter.type.grid") },
        { value: "blackbox", label: msg("sidebar.filter.type.blackbox") },
      ],
    },
    {
      key: "automation",
      label: msg("sidebar.filter.automation"),
      options: [
        { value: "all", label: msg("sidebar.filter.all") },
        { value: "automated", label: msg("sidebar.filter.automation.automated") },
        { value: "manual", label: msg("sidebar.filter.automation.manual") },
      ],
    },
    {
      key: "group",
      label: msg("sidebar.filter.group"),
      options: [
        { value: "folder", label: msg("sidebar.filter.group.folder") },
        { value: "none", label: msg("sidebar.filter.group.none") },
      ],
    },
    {
      key: "sort",
      label: msg("sidebar.filter.sort"),
      options: [
        { value: "recent", label: msg("sidebar.filter.sort.recent") },
        { value: "name", label: msg("sidebar.filter.sort.name") },
      ],
    },
  ];
}

/**
 * The sidebar's filter button: each row names a setting and its current value
 * and opens a submenu of choices. Filters sit above a separator, grouping and
 * sorting below it. A dot on the button marks an active filter.
 */
export function SidebarFilterMenu({
  view,
  onChange,
}: {
  view: RunView;
  onChange: (next: RunView) => void;
}) {
  const filtered = isFiltered(view);
  const rows = viewRows();
  const label = msg("sidebar.filter.button");

  const renderRow = (row: ViewRow) => {
    const current = row.options.find((o) => o.value === view[row.key]);
    return (
      <DropdownMenuSub key={row.key}>
        <DropdownMenuSubTrigger className="text-[0.8125rem]">
          <span className="flex-1 text-foreground/80">{row.label}</span>
          <span className="text-muted-foreground">{current?.label}</span>
          <CaretRight
            className="size-3 text-muted-foreground/70 rtl:-scale-x-100"
            aria-hidden="true"
          />
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent className="min-w-40">
          {row.options.map((option) => {
            const selected = option.value === view[row.key];
            return (
              <DropdownMenuItem
                key={option.value}
                className="text-[0.8125rem]"
                onSelect={(e) => {
                  // Keep the menu open so several settings can change in one go.
                  e.preventDefault();
                  onChange({ ...view, [row.key]: option.value });
                }}
              >
                <span className="flex-1">{option.label}</span>
                <Check
                  className={cn("size-3.5 text-primary", !selected && "invisible")}
                  aria-hidden="true"
                />
              </DropdownMenuItem>
            );
          })}
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    );
  };

  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={label}
              className="relative text-muted-foreground hover:text-foreground data-[state=open]:bg-sidebar-accent/50 data-[state=open]:text-foreground"
            >
              <FunnelSimple className="size-3.5" aria-hidden="true" />
              {filtered && (
                <span
                  aria-hidden="true"
                  className="absolute end-0.5 top-0.5 size-1.5 rounded-full bg-primary-fill"
                />
              )}
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">{label}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className="w-60">
        {rows.slice(0, 4).map(renderRow)}
        <DropdownMenuSeparator />
        {rows.slice(4).map(renderRow)}
        {(filtered || view.group !== "folder" || view.sort !== "recent") && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="text-[0.8125rem] text-muted-foreground"
              onSelect={() => onChange(DEFAULT_RUN_VIEW)}
            >
              {msg("sidebar.filter.reset")}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Header button that starts a new top-level folder. */
export function NewFolderButton({ onClick }: { onClick: () => void }) {
  const label = msg("folders.new");
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      autoTooltip={false}
      aria-label={label}
      onClick={onClick}
      className="text-muted-foreground hover:text-foreground"
    >
      <FolderPlus className="size-3.5" aria-hidden="true" />
    </Button>
  );
}

// ---------------------------------------------------------------------------
// Tree
// ---------------------------------------------------------------------------

interface TreeProps {
  childMap: Map<string | null, RunFolder[]>;
  expanded: ReadonlySet<string>;
  onToggle: (folderId: string) => void;
  view: RunView;
  /** Bumped whenever the sidebar refreshes, so open folders refetch their runs. */
  version: number;
  renderRun: (job: SidebarJobItem) => React.ReactNode;
  onAction: (action: FolderDialogState) => void;
}

/** A list of sibling folders, each collapsible, ordered by the view's sort. */
export function FolderTree({
  folders,
  depth = 0,
  inheritedFrom = null,
  ...props
}: TreeProps & {
  folders: RunFolder[];
  depth?: number;
  /** Top folder whose share reaches these folders; null at the top level. */
  inheritedFrom?: string | null;
}) {
  return (
    <>
      {sortFolders(folders, props.view.sort).map((folder, idx) => (
        <motion.div
          key={folder.id}
          layout="position"
          initial={false}
          transition={rowTransition(idx)}
        >
          <FolderNode folder={folder} depth={depth} inheritedFrom={inheritedFrom} {...props} />
        </motion.div>
      ))}
    </>
  );
}

function FolderNode({
  folder,
  depth,
  inheritedFrom,
  ...props
}: TreeProps & { folder: RunFolder; depth: number; inheritedFrom: string | null }) {
  const { expanded, onToggle, view, version, renderRun, onAction, childMap } = props;
  const isOpen = expanded.has(folder.id);
  const [runs, setRuns] = React.useState<SidebarJobItem[] | null>(null);
  const [menuOpen, setMenuOpen] = React.useState(false);
  const isRtl = getActiveDir() === "rtl";
  const subfolders = childMap.get(folder.id) ?? [];
  const editable = canEditFolder(folder);
  const isOwner = folder.role === "owner";
  const router = useRouter();
  const shared = useSharedItemActions(
    { kind: "folders", id: folder.id, name: folder.name, role: folder.role },
    { onChanged: () => window.dispatchEvent(new Event("optimizations-changed")) },
  );

  // Runs load lazily the first time a folder opens, and refresh with the rest
  // of the sidebar while it stays open.
  React.useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    listFolderRuns(folder.id)
      .then((res) => {
        if (!cancelled) setRuns(res.items);
      })
      .catch(() => {
        if (!cancelled) setRuns((prev) => prev ?? []);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen, folder.id, version]);

  const visibleRuns = runs ? applyRunView(runs, view) : null;
  const FolderIcon = isOpen ? FolderOpen : Folder;

  const menuItem = (
    Icon: React.ComponentType<{ className?: string }>,
    label: string,
    action: FolderDialogState,
    destructive = false,
  ) => (
    <PopoverPrimitive.Close asChild>
      <button
        type="button"
        onClick={() => onAction(action)}
        className={cn(
          COMPACT_POPOVER_ITEM_CLASS,
          destructive && "text-destructive hover:bg-destructive/10 focus-visible:bg-destructive/10",
        )}
      >
        <Icon
          className={cn(COMPACT_POPOVER_ICON_CLASS, destructive && "text-destructive")}
          aria-hidden="true"
        />
        <span className="flex-1 text-start">{label}</span>
      </button>
    </PopoverPrimitive.Close>
  );

  return (
    <div>
      <div
        className={cn(
          "group flex min-h-[44px] items-center gap-1 rounded-lg pe-1 text-[0.6875rem] text-muted-foreground transition-colors duration-150 hover:bg-sidebar-accent/30 hover:text-foreground lg:min-h-0",
          menuOpen && "bg-sidebar-accent/30 text-foreground",
        )}
      >
        <button
          type="button"
          onClick={() => onToggle(folder.id)}
          aria-expanded={isOpen}
          className="flex min-h-[44px] min-w-0 flex-1 items-center gap-1.5 rounded-lg py-2 ps-1.5 text-start focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 lg:min-h-0"
        >
          <CaretRight
            className={cn(
              "size-3 shrink-0 text-muted-foreground/60 transition-transform duration-150 motion-reduce:transition-none",
              isRtl && "-scale-x-100",
              isOpen && (isRtl ? "-rotate-90" : "rotate-90"),
            )}
            aria-hidden="true"
          />
          <FolderIcon
            className={cn(
              "size-3.5 shrink-0",
              isOpen ? "text-primary/80" : "text-muted-foreground/80",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 truncate font-medium" title={folder.name} dir="auto">
            {folder.name}
          </span>
          {folder.shared && (
            <Users
              className="size-3 shrink-0 text-muted-foreground/50"
              aria-label={msg("folders.shared")}
            />
          )}
          <span className="shrink-0 tabular-nums text-muted-foreground/45">{folder.run_count}</span>
        </button>
        <PopoverPrimitive.Root open={menuOpen} onOpenChange={setMenuOpen}>
          <PopoverPrimitive.Trigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={msg("folders.menu", { name: folder.name })}
              className="shrink-0 text-muted-foreground opacity-100 hover:text-foreground lg:opacity-0 lg:group-hover:opacity-100 lg:focus-visible:opacity-100 lg:data-[state=open]:opacity-100"
            >
              <DotsThree className="size-3.5" aria-hidden="true" />
            </Button>
          </PopoverPrimitive.Trigger>
          <PopoverPrimitive.Portal>
            <PopoverPrimitive.Content
              align="end"
              side="bottom"
              sideOffset={6}
              collisionPadding={8}
              className={COMPACT_POPOVER_PANEL_CLASS}
            >
              {editable && (
                <PopoverPrimitive.Close asChild>
                  <button
                    type="button"
                    onClick={() => router.push(`/submit?folder=${encodeURIComponent(folder.id)}`)}
                    className={COMPACT_POPOVER_ITEM_CLASS}
                  >
                    <Plus className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
                    <span className="flex-1 text-start">{msg("folders.new_run")}</span>
                  </button>
                </PopoverPrimitive.Close>
              )}
              {editable &&
                menuItem(FolderPlus, msg("folders.new_subfolder"), {
                  kind: "create",
                  parentId: folder.id,
                })}
              {editable &&
                menuItem(PencilSimple, msg("folders.rename"), { kind: "rename", folder })}
              {!isLocalMode() &&
                menuItem(ShareNetwork, msg("folders.share"), { kind: "share", folder })}
              {isOwner &&
                menuItem(ArrowRight, msg("folders.move"), { kind: "move-folder", folder })}
              {isOwner && (
                <>
                  <div role="separator" className="mx-3.5 my-1 border-t border-border/40" />
                  {menuItem(Trash, msg("folders.delete"), { kind: "delete", folder }, true)}
                </>
              )}
              {!isOwner && (
                <>
                  <div role="separator" className="mx-3.5 my-1 border-t border-border/40" />
                  <SharedItemMenuItems actions={shared} inheritedFrom={inheritedFrom} />
                </>
              )}
            </PopoverPrimitive.Content>
          </PopoverPrimitive.Portal>
        </PopoverPrimitive.Root>
      </div>

      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.16, ease: [0.25, 1, 0.5, 1] }}
            className="overflow-hidden"
          >
            <div className="ms-3 border-s border-sidebar-border/50 ps-1.5">
              {subfolders.length > 0 && (
                <FolderTree
                  folders={subfolders}
                  depth={depth + 1}
                  inheritedFrom={inheritedFrom ?? folder.name}
                  {...props}
                />
              )}
              {visibleRuns === null ? (
                <div className="flex items-center gap-2 px-2 py-1.5 text-[0.625rem] text-muted-foreground/60">
                  <CircleNotch className="size-3 animate-spin" aria-hidden="true" />
                  {msg("folders.loading")}
                </div>
              ) : (
                <AnimatePresence initial={false}>
                  {visibleRuns.map((job, idx) => (
                    <motion.div
                      key={runKey(job)}
                      layout="position"
                      initial={ROW_MOTION.initial}
                      animate={ROW_MOTION.animate}
                      exit={ROW_MOTION.exit}
                      transition={rowTransition(idx)}
                    >
                      {renderRun(job)}
                    </motion.div>
                  ))}
                </AnimatePresence>
              )}
              {visibleRuns !== null && visibleRuns.length === 0 && subfolders.length === 0 && (
                <p className="px-2 py-1.5 text-[0.625rem] text-muted-foreground/50">
                  {runs && runs.length > 0 ? msg("sidebar.filter.no_match") : msg("folders.empty")}
                </p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      {!isOwner && shared.dialog}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dialogs
// ---------------------------------------------------------------------------

/**
 * Every folder dialog in one place, driven by the sidebar's ``dialog`` state.
 * ``onDone`` runs after a successful change so the sidebar can refetch.
 */
export function FolderDialogs({
  dialog,
  folders,
  onClose,
  onDone,
}: {
  dialog: FolderDialogState | null;
  folders: RunFolder[];
  onClose: () => void;
  onDone: (result?: { createdId?: string; deletedIds?: string[] }) => void;
}) {
  const shareFolder = dialog?.kind === "share" ? dialog.folder : null;
  // The share dialog animates closed, so keep its folder mounted after the
  // state clears instead of unmounting mid-transition.
  const [lastShared, setLastShared] = React.useState<RunFolder | null>(null);
  React.useEffect(() => {
    if (shareFolder) setLastShared(shareFolder);
  }, [shareFolder]);

  return (
    <>
      <FolderNameDialog dialog={dialog} onClose={onClose} onDone={onDone} />
      <DeleteFolderDialog dialog={dialog} onClose={onClose} onDone={onDone} />
      <MoveDialog dialog={dialog} folders={folders} onClose={onClose} onDone={onDone} />
      {lastShared && (
        <FolderShareDialog
          folderId={lastShared.id}
          folderName={lastShared.name}
          open={shareFolder !== null}
          onOpenChange={(open) => {
            if (!open) onClose();
          }}
          onChanged={() => onDone()}
        />
      )}
    </>
  );
}

function FolderNameDialog({
  dialog,
  onClose,
  onDone,
}: {
  dialog: FolderDialogState | null;
  onClose: () => void;
  onDone: (result?: { createdId?: string }) => void;
}) {
  const open = dialog?.kind === "create" || dialog?.kind === "rename";
  const renaming = dialog?.kind === "rename" ? dialog.folder : null;
  const [name, setName] = React.useState("");
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    if (open) setName(renaming?.name ?? "");
  }, [open, renaming]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || saving || !dialog) return;
    setSaving(true);
    try {
      if (dialog.kind === "rename") {
        await updateFolder(dialog.folder.id, { name: trimmed });
        toast.success(msg("folders.renamed"));
        onDone();
      } else if (dialog.kind === "create") {
        const created = await createFolder({ name: trimmed, parent_id: dialog.parentId });
        toast.success(msg("folders.created"));
        onDone({ createdId: created.id });
      }
      onClose();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("folders.error")));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="w-[min(24rem,92vw)] max-w-[min(24rem,92vw)] sm:max-w-sm">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>
              {renaming
                ? msg("folders.rename.title")
                : dialog?.kind === "create" && dialog.parentId
                  ? msg("folders.new_subfolder")
                  : msg("folders.new")}
            </DialogTitle>
            <DialogDescription className="sr-only">{msg("folders.name_label")}</DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={120}
            placeholder={msg("folders.name_placeholder")}
            aria-label={msg("folders.name_label")}
            dir="auto"
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={saving}>
              {msg("folders.cancel")}
            </Button>
            <Button type="submit" disabled={saving || name.trim().length === 0}>
              {saving ? (
                <CircleNotch
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
              ) : renaming ? (
                msg("folders.save")
              ) : (
                msg("folders.create")
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DeleteFolderDialog({
  dialog,
  onClose,
  onDone,
}: {
  dialog: FolderDialogState | null;
  onClose: () => void;
  onDone: (result?: { deletedIds?: string[] }) => void;
}) {
  const folder = dialog?.kind === "delete" ? dialog.folder : null;
  const [deleting, setDeleting] = React.useState(false);
  const [before, after] = msg("folders.delete.body").split("{name}");

  const confirm = async () => {
    if (!folder) return;
    setDeleting(true);
    try {
      const res = await deleteFolder(folder.id);
      toast.success(msg("folders.deleted"));
      onDone({ deletedIds: res.deleted });
      onClose();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("folders.error")));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <ConfirmDialog
      open={folder !== null}
      onOpenChange={(next) => !next && onClose()}
      variant="danger"
      title={msg("folders.delete.title")}
      description={
        <>
          {before}
          <span className="font-semibold text-foreground break-words" dir="auto">
            {folder?.name}
          </span>
          {after}
        </>
      }
      cancelLabel={msg("folders.cancel")}
      confirmLabel={msg("folders.delete")}
      onConfirm={confirm}
      pending={deleting}
    />
  );
}

/**
 * Pick a destination folder for runs or for a folder. Only folders the caller
 * can edit are offered, and a folder can't move into itself or below itself.
 */
function MoveDialog({
  dialog,
  folders,
  onClose,
  onDone,
}: {
  dialog: FolderDialogState | null;
  folders: RunFolder[];
  onClose: () => void;
  onDone: () => void;
}) {
  const open = dialog?.kind === "move-runs" || dialog?.kind === "move-folder";
  const current =
    dialog?.kind === "move-runs"
      ? dialog.currentFolderId
      : dialog?.kind === "move-folder"
        ? dialog.folder.parent_id
        : null;
  const [target, setTarget] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    if (open) setTarget(current);
  }, [open, current]);

  const options = React.useMemo(() => {
    const childMap = childrenByParent(folders);
    const blocked = new Set<string>();
    if (dialog?.kind === "move-folder") {
      const stack = [dialog.folder.id];
      while (stack.length) {
        const id = stack.pop()!;
        blocked.add(id);
        for (const child of childMap.get(id) ?? []) stack.push(child.id);
      }
    }
    const rows: Array<{ folder: RunFolder; depth: number }> = [];
    const walk = (parent: string | null, depth: number) => {
      for (const folder of sortFolders(childMap.get(parent) ?? [], "name")) {
        if (blocked.has(folder.id)) continue;
        if (canEditFolder(folder)) rows.push({ folder, depth });
        walk(folder.id, depth + 1);
      }
    };
    walk(null, 0);
    return rows;
  }, [dialog, folders]);

  const submit = async () => {
    if (!dialog || saving) return;
    setSaving(true);
    try {
      if (dialog.kind === "move-runs") {
        const res = await moveRunsToFolder(dialog.optimizationIds, target);
        if (res.skipped.length > 0) toast.error(msg("folders.move.partial"));
        else toast.success(msg("folders.moved"));
      } else if (dialog.kind === "move-folder") {
        await moveFolder(dialog.folder.id, target);
        toast.success(msg("folders.moved"));
      }
      onDone();
      onClose();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("folders.error")));
    } finally {
      setSaving(false);
    }
  };

  const row = (id: string | null, label: string, depth: number, Icon = Folder) => {
    const selected = target === id;
    return (
      <button
        key={id ?? "__none__"}
        type="button"
        role="radio"
        aria-checked={selected}
        onClick={() => setTarget(id)}
        className={cn(
          "flex min-h-[44px] w-full items-center gap-2 rounded-lg px-2 py-1.5 text-start text-sm transition-colors lg:min-h-0",
          selected
            ? "bg-primary-fill text-primary-foreground"
            : "text-foreground/80 hover:bg-accent/50",
        )}
        style={{ paddingInlineStart: `${0.5 + depth * 1}rem` }}
      >
        <Icon
          className={cn(
            "size-4 shrink-0",
            selected ? "text-primary-foreground" : "text-muted-foreground",
          )}
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1 truncate" dir="auto">
          {label}
        </span>
        {selected && <Check className="size-3.5 shrink-0 text-primary" aria-hidden="true" />}
      </button>
    );
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="w-[min(26rem,92vw)] max-w-[min(26rem,92vw)] sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{msg("folders.move.title")}</DialogTitle>
          <DialogDescription>
            {dialog?.kind === "move-folder"
              ? msg("folders.move.folder_hint")
              : msg("folders.move.runs_hint")}
          </DialogDescription>
        </DialogHeader>
        <div role="radiogroup" className="max-h-[50dvh] overflow-y-auto">
          {row(null, msg("folders.move.none"), 0, FolderOpen)}
          {options.map(({ folder, depth }) => row(folder.id, folder.name, depth))}
          {options.length === 0 && (
            <p className="px-2 py-3 text-xs text-muted-foreground">
              {msg("folders.move.no_targets")}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            {msg("folders.cancel")}
          </Button>
          <Button onClick={submit} disabled={saving || target === current}>
            {saving ? (
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              msg("folders.move.confirm")
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
