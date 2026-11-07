import type { ContractCheckItem, ContractCheckResponse } from "@/shared/types/unified-run";

import { automaticTurn } from "@/shared/ui/agent/automatic-turn";

/**
 * The automatic turn a failed setup check sends the setup agent: one line per
 * failed check, with its exact error under it.
 *
 * Args:
 *   check: The failed check.
 *   describe: The failed check's line, in the user's language.
 *
 * Returns:
 *   The message, with the automatic-turn prefix.
 */
export function setupCheckFeedback(
  check: ContractCheckResponse,
  describe: (item: ContractCheckItem) => string,
): string {
  const lines = check.checks
    .filter((item) => item.status === "failed")
    .map((item) => {
      const head = `- ${describe(item)}`;
      return item.detail ? `${head}\n\n\`\`\`\n${item.detail}\n\`\`\`` : head;
    });
  return automaticTurn(lines.join("\n"));
}
