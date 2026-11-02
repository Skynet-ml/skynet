// Stands in for use-linked-account in account-menu.states.test.tsx, so a static
// render can show each link state (a static render never runs the hook's effect).
import type { LinkedAccount } from "../../hooks/use-linked-account";

export {
  type LinkedAccount,
  linkedUsername,
  loadLinkedAccount,
  onLinkChange,
  startHostedSignIn,
  unlinkHostedAccount,
  wasLinked,
} from "../../hooks/use-linked-account";

export const mockAccount: { current: LinkedAccount | undefined } = { current: undefined };

export function useLinkedAccount(): LinkedAccount | undefined {
  return mockAccount.current;
}
