"use client";

import * as React from "react";
import Link from "next/link";
import {
  BookOpen,
  Code,
  Desktop,
  DotsThree,
  FileText,
  Gear,
  ShieldCheck,
  SignIn,
  SignOut,
} from "@/shared/ui/icons";
import { signOut, useSession } from "next-auth/react";
import { toast } from "react-toastify";
import { LEGAL_LINKS, SOURCE_CODE_URL } from "@/features/legal/legal-config";
import { msg } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { cn } from "@/shared/lib/utils";
import {
  COMPACT_POPOVER_ICON_CLASS,
  COMPACT_POPOVER_ITEM_CLASS,
  COMPACT_POPOVER_PANEL_CLASS,
} from "@/shared/ui/compact-popover-menu";
import { useLocale } from "@/shared/providers";
import { dirForLocale, docsPathForLocale } from "@/shared/lib/locale";
import { docsLinkProps } from "@/shared/lib/docs-link";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { useSettingsModal } from "@/features/settings";
import {
  type LinkedAccount,
  startHostedSignIn,
  unlinkHostedAccount,
  useLinkedAccount,
} from "@/shared/hooks/use-linked-account";

export { startHostedSignIn };

/**
 * The Mac app's identity labels. An unknown link (`undefined`: loading, or the
 * status call failed) shows only the title, so a linked Mac never reads "Not
 * signed in" by mistake.
 */
export function localIdentity(account: LinkedAccount | undefined): {
  name: string;
  detail: string | null;
} {
  if (!account) return { name: msg("local_identity.title"), detail: null };
  if (!account.linked)
    return { name: msg("local_identity.title"), detail: msg("settings.account.signed_out") };
  const name = account.name ?? account.email ?? msg("account.linked_fallback_name");
  if (account.offline) return { name, detail: msg("local_identity.linked_offline") };
  const detail =
    account.email && account.email !== name ? account.email : msg("local_identity.linked");
  return { name, detail };
}

/** Two-letter monogram from a display name or email — the avatar fallback when there's no image. */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const first = parts[0] ?? "";
  if (parts.length >= 2) {
    const second = parts[1] ?? "";
    return ((first[0] ?? "") + (second[0] ?? "")).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

function Avatar({
  image,
  label,
  size,
  device = false,
}: {
  image?: string | null;
  label: string;
  size: number;
  device?: boolean;
}) {
  if (device) {
    return (
      <span
        aria-hidden="true"
        className="grid shrink-0 place-items-center rounded-full bg-muted text-muted-foreground"
        style={{ width: size, height: size }}
      >
        <Desktop style={{ width: size * 0.55, height: size * 0.55 }} />
      </span>
    );
  }
  if (image) {
    return (
      // Avatars come from arbitrary OAuth providers (Google, GitHub, Auth0); a
      // plain <img> avoids per-host next/image remotePatterns config.
      <img
        src={image}
        alt=""
        width={size}
        height={size}
        referrerPolicy="no-referrer"
        className="shrink-0 rounded-full object-cover"
        style={{ width: size, height: size }}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className="grid shrink-0 place-items-center rounded-full bg-primary-fill font-semibold text-primary-foreground"
      style={{ width: size, height: size, fontSize: size * 0.4 }}
    >
      {initials(label)}
    </span>
  );
}

/**
 * Account menu — the profile button anchored at the foot of the sidebar.
 *
 * Mirrors a familiar account button (avatar, name, plan badge) the way ChatGPT
 * sits one at the bottom of its rail. Clicking it opens a menu *upward* with a
 * profile header row (→ account settings tab), a separator, the Settings
 * destination, then a separator and Log out — so
 * Settings lives inside this menu rather than as its own row. When the rail is
 * collapsed the trigger shrinks to the avatar alone, with a tooltip naming it.
 * All targets are real Skynet surfaces; logical alignment and a flipped chevron
 * keep it correct in RTL.
 *
 * `trigger` swaps the built-in avatar button for a caller-owned control (the
 * phone shell's Account tab), which then opens the same menu.
 */
export function AccountMenu({
  collapsed = false,
  trigger: customTrigger,
  align = "start",
}: {
  collapsed?: boolean;
  trigger?: React.ReactNode;
  align?: "start" | "center" | "end";
}) {
  const { data: session } = useSession();
  const { locale } = useLocale();
  const dir = dirForLocale(locale);
  const isRtl = dir === "rtl";
  // The collapsed-rail tooltip reads toward the content area: right in LTR, left in RTL.
  const tooltipSide = isRtl ? "left" : "right";
  const { setOpen: setSettingsOpen } = useSettingsModal();
  const [open, setOpen] = React.useState(false);
  // Read the link again on each open: the sign-in finishes in the browser.
  const [reads, setReads] = React.useState(0);
  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) setReads((n) => n + 1);
  };
  const local = isLocalMode();
  const account = useLinkedAccount(reads);
  const linked = account?.linked ? account : null;

  if (!session?.user) return null;

  // The Mac app's session user is an internal placeholder, never a real
  // account, so local mode shows the linked hosted account or "Local mode".
  const identity = local ? localIdentity(account) : null;
  const name = identity ? identity.name : (session.user.name ?? session.user.email ?? "");
  const email = identity ? (identity.detail ?? "") : (session.user.email ?? "");
  // Skip the email line when there's no display name — `name` already falls back to it.
  const showEmail = Boolean(email) && email !== name;
  const deviceAvatar = local && !linked;
  // The local session user has no provider picture; the linked account has one.
  const image = local ? (linked?.image ?? null) : session.user.image;

  const close = () => setOpen(false);

  const trigger = customTrigger ? (
    customTrigger
  ) : collapsed ? (
    <button
      type="button"
      aria-label={msg("app.shell.account.aria")}
      className="flex items-center justify-center rounded-full transition-transform duration-150 active:scale-95 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45"
    >
      <Avatar image={image} label={name} size={28} device={deviceAvatar} />
    </button>
  ) : (
    <button
      type="button"
      aria-label={msg("app.shell.account.aria")}
      className="group flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-start transition-colors duration-200 hover:bg-sidebar-accent/40 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45"
    >
      <Avatar image={image} label={name} size={28} device={deviceAvatar} />
      {/* Physical edge from the UI dir, not logical `text-start`: the name keeps
          dir="auto" so it shapes and truncates by its own script, which would make
          `start` resolve to the name's direction and split it from the email line
          onto the opposite edge in a mixed-script UI (e.g. a Latin name, Hebrew UI). */}
      <span className={cn("flex min-w-0 flex-1 flex-col", isRtl ? "text-right" : "text-left")}>
        <span
          dir="auto"
          className="truncate text-sm font-medium leading-tight text-sidebar-foreground"
        >
          {name}
        </span>
        {showEmail && (
          <span
            dir={local ? "auto" : "ltr"}
            className="truncate text-xs leading-tight text-muted-foreground"
          >
            {email}
          </span>
        )}
      </span>
      <DotsThree
        className="size-4 shrink-0 text-muted-foreground/50 transition-colors group-hover:text-muted-foreground"
        aria-hidden="true"
      />
    </button>
  );

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      {collapsed && !customTrigger ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>{trigger}</PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent side={tooltipSide}>{msg("app.shell.account.aria")}</TooltipContent>
        </Tooltip>
      ) : (
        <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      )}

      <PopoverContent
        side="top"
        align={align}
        dir={dir}
        className={cn(COMPACT_POPOVER_PANEL_CLASS, "w-64 max-w-[90vw]")}
        collisionPadding={12}
      >
        {/* Identity header — who you're signed in as. Deliberately non-interactive:
            account and general settings both live in the items below, so a click
            target here would just duplicate the Settings row. Surfaces the email,
            the way ChatGPT's account menu does. */}
        <div className="flex items-center gap-2.5 px-3 py-2">
          <Avatar image={image} label={name} size={32} device={deviceAvatar} />
          {/* Physical edge from the UI dir — see the trigger above for why `text-start`
              can't be used while the name carries dir="auto". */}
          <span className={cn("flex min-w-0 flex-1 flex-col", isRtl ? "text-right" : "text-left")}>
            <span dir="auto" className="truncate font-medium leading-tight text-foreground">
              {name}
            </span>
            {showEmail && (
              <span dir={local ? "auto" : "ltr"} className="truncate text-xs text-muted-foreground">
                {email}
              </span>
            )}
          </span>
        </div>

        <div role="separator" className="my-1 h-px bg-border/60" />

        {local && account?.linked === false && (
          <button
            type="button"
            onClick={() => {
              close();
              void startHostedSignIn();
            }}
            className={COMPACT_POPOVER_ITEM_CLASS}
          >
            <SignIn className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
            {msg("local_identity.sign_in")}
          </button>
        )}

        <button
          type="button"
          onClick={() => {
            close();
            setSettingsOpen(true);
          }}
          className={COMPACT_POPOVER_ITEM_CLASS}
        >
          <Gear className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
          {msg("app.shell.account.settings")}
        </button>

        <Link {...docsLinkProps(docsPathForLocale(locale), local)} onClick={close} className={COMPACT_POPOVER_ITEM_CLASS}>
          <BookOpen className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
          {msg("app.shell.account.docs")}
        </Link>

        <div role="separator" className="my-1 h-px bg-border/60" />

        <Link href={LEGAL_LINKS.terms} onClick={close} className={COMPACT_POPOVER_ITEM_CLASS}>
          <FileText className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
          {msg("legal.terms_link")}
        </Link>
        <Link href={LEGAL_LINKS.privacy} onClick={close} className={COMPACT_POPOVER_ITEM_CLASS}>
          <ShieldCheck className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
          {msg("legal.privacy_link")}
        </Link>
        <a
          href={SOURCE_CODE_URL}
          target="_blank"
          rel="noopener noreferrer"
          onClick={close}
          className={COMPACT_POPOVER_ITEM_CLASS}
        >
          <Code className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
          {msg("legal.source_link")}
        </a>

        {/* The Mac app's local session is never ended: its data stays on the Mac.
            Log out there unlinks the hosted account, so it shows only when linked. */}
        {(!local || linked) && (
          <>
            <div role="separator" className="my-1 h-px bg-border/60" />

            <button
              type="button"
              onClick={() => {
                close();
                if (!local) {
                  void signOut({ callbackUrl: "/login" });
                  return;
                }
                unlinkHostedAccount().catch(() =>
                  toast.error(msg("local_identity.sign_out_failed")),
                );
              }}
              className={COMPACT_POPOVER_ITEM_CLASS}
            >
              {/* Point the arrow "outward" toward the sidebar's edge so it reads as
                  leaving: the rail sits on the left in LTR (arrow ←, flipped from the
                  icon's default) and on the right in RTL (arrow →, the default). Derive
                  the flip from `isRtl` rather than the `rtl:` Tailwind variant — this
                  menu is a portaled popover where the variant doesn't fire reliably, and
                  the rest of the file drives direction the same way. */}
              <SignOut
                className={cn(COMPACT_POPOVER_ICON_CLASS, !isRtl && "-scale-x-100")}
                aria-hidden="true"
              />
              {msg("app.shell.logout")}
            </button>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
