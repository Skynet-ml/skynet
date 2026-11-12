// Table cells clamp to a line or two, so long prose past this never shows;
// handing the browser the full multi-KB value still costs a full text layout
// per cell, which froze grids for most of a second on wide 3000-row datasets.
const CELL_PREVIEW_CHARS = 300;

export function clipText(text: string, max = CELL_PREVIEW_CHARS): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
