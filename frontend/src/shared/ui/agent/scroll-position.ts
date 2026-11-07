/** Where a chat's scroll box sits relative to its newest message. */

// Within this many px of the bottom still counts as "at the bottom", so
// momentum scrolling and fractional positions don't break follow mode.
export const STICKY_BOTTOM_SLACK_PX = 48;

interface ScrollBox {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
}

/** True when the box shows its last line, or is within the slack of it. */
export function isNearBottom(box: ScrollBox, slack = STICKY_BOTTOM_SLACK_PX): boolean {
  return box.scrollHeight - box.scrollTop - box.clientHeight < slack;
}
