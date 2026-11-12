/**
 * How a chat's messages group into transcript blocks: a user message and
 * the agent's work after it, kept apart so the tests pin the rules.
 */

interface PairMessage {
  role: "assistant" | "user";
  hidden?: boolean;
}

export type Pair<M extends PairMessage> = {
  key: string;
  user: { msg: M; index: number } | null;
  agent: M | null;
  /** Replies before ``agent`` in the same work block, oldest first. */
  earlier: M[];
};

/**
 * Group messages into blocks of one user message and the agent's replies.
 *
 * A hidden user turn is the app carrying on for the user (the wizard's next
 * screen), so its reply joins the work block before it. Blocks break only
 * where the user spoke.
 *
 * Args:
 *   messages: The chat's messages, oldest first.
 *
 * Returns:
 *   The blocks, oldest first, each keyed by its position.
 */
export function pairMessages<M extends PairMessage>(messages: readonly M[]): Array<Pair<M>> {
  const result: Array<Pair<M>> = [];
  let currentUser: { msg: M; index: number } | null = null;
  let continuing = false;
  let seq = 0;
  messages.forEach((m, idx) => {
    if (m.role === "user") {
      if (currentUser) {
        result.push({ key: `t-${seq++}`, user: currentUser, agent: null, earlier: [] });
      }
      continuing = Boolean(m.hidden && result.at(-1)?.agent);
      currentUser = m.hidden ? null : { msg: m, index: idx };
    } else {
      const last = result.at(-1);
      if (continuing && last?.agent) {
        result[result.length - 1] = { ...last, agent: m, earlier: [...last.earlier, last.agent] };
      } else {
        result.push({ key: `t-${seq++}`, user: currentUser, agent: m, earlier: [] });
      }
      currentUser = null;
      continuing = false;
    }
  });
  if (currentUser) {
    result.push({ key: `t-${seq++}`, user: currentUser, agent: null, earlier: [] });
  }
  return result;
}
