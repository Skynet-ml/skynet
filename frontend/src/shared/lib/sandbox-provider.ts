import { msg } from "@/shared/lib/messages";
import type { MessageKey } from "@/shared/lib/generated/ui-catalog";

const SANDBOX_PROVIDER_LABEL_KEYS: Record<string, MessageKey> = {
  // The run view's runtime names; UI copy never names the sandbox vendor.
  vercel: "submit.runtime.vercel",
  docker: "submit.runtime.docker",
};

/**
 * The translated name of a sandbox provider. A provider the UI has no name
 * for yet shows the server's label, else its raw id, so a new provider needs
 * no UI change.
 */
export function sandboxProviderLabel(id: string, serverLabel?: string): string {
  const key = SANDBOX_PROVIDER_LABEL_KEYS[id];
  return key ? msg(key) : serverLabel || id;
}

/** The chosen provider while the server still offers it, else null (the server default). */
export function offeredSandboxProvider(
  chosen: string | null | undefined,
  providers: ReadonlyArray<{ id: string }>,
): string | null {
  return chosen && providers.some((provider) => provider.id === chosen) ? chosen : null;
}
