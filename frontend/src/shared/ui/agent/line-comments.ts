/**
 * Comments the user left on lines of the Changes pane's diffs. They ride
 * inside the next message string as tagged blocks after the typed text, so
 * the agent reads the file, the line and the comment, and the sent bubble
 * shows each one as a row linked to its line.
 */

import type { MidTurnQueue } from "./steer-queue";

/** A comment on one line of a file's current text. */
export interface LineComment {
  path: string;
  line: number;
  text: string;
}

const BLOCK_RE = /\n*<line_comment path="([^"]*)" line="(\d+)">\n?([\s\S]*?)\n?<\/line_comment>/g;

/**
 * Add line comments to a message, after everything else in it.
 *
 * Args:
 *   message: The message string, with any pasted-text and file blocks.
 *   comments: The comments to send with it.
 *
 * Returns:
 *   The message with one block per comment; the message itself when there are none.
 */
export function joinLineComments(message: string, comments: readonly LineComment[]): string {
  if (!comments.length) return message;
  const blocks = comments
    .map((c) => {
      const path = c.path.replace(/["<>\r\n]/g, "_");
      const text = c.text.replaceAll("</line_comment>", "</line_comment >");
      return `<line_comment path="${path}" line="${Math.max(1, Math.floor(c.line))}">\n${text}\n</line_comment>`;
    })
    .join("\n\n");
  return message ? `${message}\n\n${blocks}` : blocks;
}

/**
 * Take the line comments out of a message.
 *
 * Args:
 *   message: A message string.
 *
 * Returns:
 *   The message without its comment blocks, and the comments in order.
 */
export function splitLineComments(message: string): { rest: string; comments: LineComment[] } {
  const comments: LineComment[] = [];
  const rest = message.replace(BLOCK_RE, (_, path: string, line: string, text: string) => {
    comments.push({ path, line: Number(line), text });
    return "";
  });
  return { rest: comments.length ? rest.replace(/^\n+/, "") : rest, comments };
}

/**
 * Send line comments with a message the user steers or queues mid-turn.
 *
 * Args:
 *   midTurn: The agent's mid-turn queue.
 *   comments: The comments to send with the next message.
 *   clear: Drops the comments once they are sent.
 *
 * Returns:
 *   The queue, unchanged when there are no comments.
 */
export function withLineComments(
  midTurn: MidTurnQueue,
  comments: readonly LineComment[],
  clear?: () => void,
): MidTurnQueue {
  if (!comments.length) return midTurn;
  return {
    ...midTurn,
    steer: (text) => {
      midTurn.steer(joinLineComments(text.trim(), comments));
      clear?.();
    },
    queue: (text) => {
      midTurn.queue(joinLineComments(text.trim(), comments));
      clear?.();
    },
  };
}
