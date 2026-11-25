/**
 * The stylesheet for chat message Markdown, after the Codex desktop app's look.
 *
 * It ships as a string that MessageMarkdown renders as a hoisted React
 * `<style>`, not as a CSS import: Node test runs load the renderer through
 * jiti, which cannot import CSS. The rules sit in the components layer, so a
 * caller's Tailwind classes win.
 */

export const MARKDOWN_CSS = `
/* Registered as a length so it resolves to px once, on the root: a heading's
   bigger font must not grow the spacing unit inside it. */
@property --md-s {
  syntax: "<length>";
  inherits: true;
  initial-value: 4px;
}

@layer components {
  .md-root {
    --md-s: 0.25em;
    --md-border: var(--border);
    --md-border-light: var(--border-subtle);
    overflow-wrap: anywhere;
    line-height: 1.625;
  }
  .md-root > :first-child {
    margin-top: 0;
  }
  .md-root > :last-child {
    margin-bottom: 0;
  }
  .md-root strong {
    font-weight: 600;
  }

  .md-root p {
    margin: 0 0 var(--md-s);
  }
  .md-root p:not(:first-child) {
    margin-top: calc(var(--md-s) * 2);
  }
  .md-root :is(h1, h2, h3, ul, ol) + p {
    margin-top: 0;
  }

  .md-root :is(h1, h2, h3, h4, h5, h6) {
    font-weight: 600;
  }
  .md-root h1 {
    font-size: 1.5em;
    line-height: calc(var(--md-s) * 8);
    margin: calc(var(--md-s) * 4) 0 calc(var(--md-s) * 2);
  }
  .md-root h2 {
    font-size: 1.25em;
    line-height: calc(var(--md-s) * 7);
    margin: calc(var(--md-s) * 4) 0 var(--md-s);
  }
  .md-root h3 {
    font-size: 1.125em;
    line-height: calc(var(--md-s) * 7);
    margin: calc(var(--md-s) * 4) 0 var(--md-s);
  }
  .md-root h4 {
    font-size: 1em;
    line-height: calc(var(--md-s) * 6);
    margin: calc(var(--md-s) * 4) 0 0;
  }
  .md-root :is(h5, h6) {
    font-size: 1em;
    margin: 0;
  }

  .md-root :is(ul, ol) {
    margin: 0 0 var(--md-s);
    padding-inline-start: 1.625em;
    list-style-position: outside;
  }
  .md-root ul {
    list-style-type: disc;
  }
  .md-root ul ul {
    list-style-type: circle;
  }
  .md-root ul ul ul {
    list-style-type: square;
  }
  .md-root ol {
    list-style-type: decimal;
  }
  .md-root li {
    padding-inline-start: calc(var(--md-s) * 1.5);
  }
  .md-root li::marker {
    font-weight: 600;
  }
  .md-root li :is(ul, ol) {
    margin: 0;
  }
  .md-root li > p {
    margin: 0;
  }
  .md-root li > p + p {
    margin-top: calc(var(--md-s) * 4);
  }
  .md-root li.task-list-item {
    list-style-type: none;
  }
  .md-root .task-list-item input {
    margin-inline-end: 0.375rem;
    vertical-align: -0.125rem;
  }

  .md-root blockquote {
    position: relative;
    margin: 0 0 calc(var(--md-s) * 2);
    padding-block: calc(var(--md-s) * 2);
    padding-inline-start: calc(var(--md-s) * 6);
    background: transparent;
  }
  .md-root blockquote::before {
    content: "";
    position: absolute;
    inset-block: calc(var(--md-s) * 2);
    inset-inline-start: 0;
    width: var(--md-s);
    border-radius: 9999px;
    background: var(--md-border);
  }

  .md-root hr {
    border: 0;
    border-top: 1px solid var(--md-border);
    margin: calc(var(--md-s) * 7) 0;
  }

  .md-root a {
    color: inherit;
    text-decoration: underline dotted 1px;
    text-decoration-color: var(--muted-foreground);
    text-underline-offset: 2px;
  }
  .md-root a:hover {
    color: var(--primary);
    text-decoration-style: solid;
    text-decoration-color: currentColor;
  }

  .md-root :not(pre) > code {
    font-family: var(--md-code-font, var(--font-mono));
    font-size: 0.92em;
    padding: 1px 6px;
    border-radius: 6px;
    background: color-mix(in oklab, var(--muted), currentColor 6%);
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
    unicode-bidi: isolate;
  }

  .md-root .md-code {
    margin-block: calc(var(--md-s) * 5);
    border: 1px solid var(--md-border-light);
    border-radius: 1.25rem;
    overflow: clip;
    background: var(--md-code-bg, var(--surface-2));
  }
  .md-root .md-code pre {
    font-family: var(--md-code-font, var(--font-mono));
    font-size: var(--md-code-size, 0.875rem);
    line-height: max(1.5em, 1.25rem);
  }

  .md-root .md-table-scroll {
    margin-block: calc(var(--md-s) * 2);
    overflow-x: auto;
  }
  .md-root .md-table-scroll[data-fade-left] {
    mask-image: linear-gradient(to right, transparent, black 1.5rem);
  }
  .md-root .md-table-scroll[data-fade-right] {
    mask-image: linear-gradient(to left, transparent, black 1.5rem);
  }
  .md-root .md-table-scroll[data-fade-left][data-fade-right] {
    mask-image: linear-gradient(
      to right,
      transparent,
      black 1.5rem,
      black calc(100% - 1.5rem),
      transparent
    );
  }
  .md-root table {
    min-width: 100%;
    width: fit-content;
    border-collapse: collapse;
    font-size: 0.875rem;
    font-variant-numeric: tabular-nums;
  }
  .md-root :is(th, td) {
    padding-inline: 0 calc(var(--md-s) * 6);
    vertical-align: top;
    text-align: start;
  }
  .md-root th {
    font-weight: 600;
    padding-block: calc(var(--md-s) * 2);
    border-bottom: 1px solid var(--md-border);
  }
  .md-root td {
    padding-block: calc(var(--md-s) * 2.5);
  }
  /* Codex's column widths, in 24ths of a 40rem message column. In the
     message only the caps apply; the expanded view also keeps the floors. */
  .md-root [data-col-size="sm"] {
    min-width: calc(40rem * 4 / 24);
    max-width: calc(40rem * 6 / 24);
  }
  .md-root [data-col-size="md"] {
    min-width: calc(40rem * 6 / 24);
    max-width: calc(40rem * 8 / 24);
  }
  .md-root [data-col-size="lg"] {
    min-width: calc(40rem * 8 / 24);
    max-width: calc(40rem * 12 / 24);
  }
  .md-root [data-col-size="xl"] {
    min-width: calc(40rem * 14 / 24);
    max-width: calc(40rem * 18 / 24);
  }
  .md-root .md-table-scroll [data-col-size] {
    min-width: auto;
  }
  .md-root [data-numeric] {
    white-space: nowrap;
  }
  .md-root tbody tr:not(:last-child) td {
    border-bottom: 1px solid var(--md-border-light);
  }

  .md-root .md-math {
    margin-block: calc(var(--md-s) * 2);
    overflow-x: auto;
  }

  .md-root img {
    max-width: 100%;
    border-radius: 0.5rem;
  }

  .md-user > p {
    margin: 0;
  }
  .md-user pre {
    font-family: var(--md-code-font, var(--font-mono));
  }
}

@media (prefers-reduced-motion: no-preference) {
  .md-root.md-fade > * {
    animation: md-fade-in 160ms ease-out;
  }
}

@keyframes md-fade-in {
  from {
    opacity: 0;
  }
}
`;
