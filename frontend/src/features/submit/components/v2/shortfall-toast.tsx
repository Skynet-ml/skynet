import type { ToastContent } from "react-toastify";

/**
 * Toast content for a held Continue. A short balance gets an add-funds button
 * beside the message, so the toast names the way out and opens it (SKY-632).
 */
export function shortfallToast(
  text: string,
  addFunds: { label: string; run: () => void } | null,
): ToastContent {
  if (!addFunds) return text;
  return ({ closeToast }) => (
    <span className="flex items-center gap-3">
      <span className="min-w-0 flex-1">{text}</span>
      <button
        type="button"
        onClick={() => {
          closeToast();
          addFunds.run();
        }}
        className="shrink-0 cursor-pointer font-semibold text-primary hover:underline focus-visible:underline focus-visible:outline-none"
      >
        {addFunds.label}
      </button>
    </span>
  );
}
