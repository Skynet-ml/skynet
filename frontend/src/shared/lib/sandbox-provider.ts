import { createElement } from "react";
import type { IconProps } from "@phosphor-icons/react";

import { msg } from "@/shared/lib/messages";
import type { SandboxProviderCatalog } from "@/shared/lib/api";
import type { MessageKey } from "@/shared/lib/generated/ui-catalog";
import { Cube, Desktop, Globe } from "@/shared/ui/icons";

// The run view's runtime names and the Mac/Web icons of the run place choice;
// UI copy never names the sandbox vendor.
const SANDBOX_PROVIDERS: Record<string, { label: MessageKey; icon: typeof Cube }> = {
  vercel: { label: "submit.runtime.vercel", icon: Globe },
  docker: { label: "submit.runtime.docker", icon: Desktop },
};

/** The sandbox provider list of the run's server: loading, failed, or ready. */
export type SandboxProviders =
  | { status: "loading" | "failed"; catalog: null }
  | { status: "ready"; catalog: SandboxProviderCatalog };

/**
 * The translated name of a sandbox provider. A provider the UI has no name
 * for yet shows the server's label, else its raw id, so a new provider needs
 * no UI change.
 */
export function sandboxProviderLabel(id: string, serverLabel?: string): string {
  const known = SANDBOX_PROVIDERS[id];
  return known ? msg(known.label) : serverLabel || id;
}

/** The icon of a sandbox provider; a provider the UI does not know shows a generic box. */
export function sandboxProviderIcon(id: string): typeof Cube {
  return SANDBOX_PROVIDERS[id]?.icon ?? Cube;
}

/** The icon of a sandbox provider, as one stable component for every provider. */
export function SandboxProviderIcon({ id, ...props }: { id: string } & IconProps) {
  return createElement(sandboxProviderIcon(id), props);
}

/** The chosen provider while the server still offers it, else null (the server default). */
export function offeredSandboxProvider(
  chosen: string | null | undefined,
  providers: ReadonlyArray<{ id: string }>,
): string | null {
  return chosen && providers.some((provider) => provider.id === chosen) ? chosen : null;
}

/**
 * The provider the run request names: the provider the picker shows, so the
 * run uses what the user saw. A list that is still loading or failed names
 * none, and the server default applies.
 */
export function requestSandboxProvider(
  chosen: string | null | undefined,
  sandboxes: SandboxProviders,
): string | null {
  return sandboxes.catalog ? (shownSandboxProvider(chosen, sandboxes.catalog) ?? null) : null;
}

/**
 * The provider the picker shows: the offered pick, else the server default,
 * else the first offered provider.
 */
export function shownSandboxProvider(
  chosen: string | null | undefined,
  catalog: SandboxProviderCatalog,
): string | undefined {
  return (
    offeredSandboxProvider(chosen, catalog.providers) ??
    offeredSandboxProvider(catalog.default_provider, catalog.providers) ??
    catalog.providers[0]?.id
  );
}

/**
 * Whether run setup shows the sandbox picker. A failed read shows the field
 * with its failure note; a list still loading, or empty, shows nothing.
 */
export function showsSandboxPicker(sandboxes: SandboxProviders): boolean {
  return sandboxes.status === "failed" || (sandboxes.catalog?.providers.length ?? 0) > 0;
}
