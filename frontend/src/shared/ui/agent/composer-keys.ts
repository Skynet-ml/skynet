interface EscapeEvent {
  key: string;
  defaultPrevented?: boolean;
  preventDefault: () => void;
}

/** Leave Escape to open pickers, suggestions, and IME before stopping a turn. */
export function handleComposerEscape(
  event: EscapeEvent,
  state: { streaming: boolean; popupOpen: boolean; composing: boolean },
  stop?: () => void,
): boolean {
  if (event.key !== "Escape" || event.defaultPrevented || !state.streaming || state.popupOpen || state.composing || !stop) {
    return false;
  }
  event.preventDefault();
  stop();
  return true;
}
