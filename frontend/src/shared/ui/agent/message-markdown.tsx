"use client";

import * as React from "react";
import { ArrowsOut, Code, Eye, Play, TextAlignLeft } from "@/shared/ui/icons";
import ReactMarkdown, {
  defaultUrlTransform,
  type Components,
  type UrlTransform,
} from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  codePalette,
  safeFontFamily,
  useCodeAppearance,
  type CodePalette,
} from "@/shared/lib/code-appearance";
import { docsLinkHref } from "@/shared/lib/docs-link";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CopyButton, CopyGlyph } from "@/shared/ui/copy-button";
import { Button } from "@/shared/ui/primitives/button";
import { Dialog, DialogContent, DialogTitle } from "@/shared/ui/primitives/dialog";
import type { CodeToken } from "./markdown-highlight";
import {
  DISPLAY_MATH_LANG,
  INLINE_MATH_MARK,
  codeBlockLabel,
  endsInOpenFence,
  guessLanguage,
  isInlineImageSrc,
  isMathFence,
  isSvgFence,
  prepareMath,
  repairStreamingMarkdown,
  svgDataUrl,
} from "./markdown-source";
import { MARKDOWN_CSS } from "./message-markdown-styles";

const MarkdownMath = React.lazy(() => import("./markdown-math"));

interface MarkdownContextValue {
  onRunCode?: (code: string, language: string) => void;
  /** The exact text the parser read, so a table can copy its own source. */
  source: string;
  userTone: boolean;
  /** While a reply streams into an unclosed fence: where that fence's code block ends at the least. */
  openFenceEnd: number | null;
}

const MarkdownContext = React.createContext<MarkdownContextValue>({
  source: "",
  userTone: false,
  openFenceEnd: null,
});

function extractCodeText(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractCodeText).join("");
  if (React.isValidElement(node)) {
    const props = node.props as { children?: React.ReactNode };
    return extractCodeText(props.children);
  }
  return "";
}

// One wrap setting for every code block, kept across reloads, as in Codex.
const WRAP_KEY = "skynet:code-wrap";
const wrapListeners = new Set<() => void>();
let wrapCache: boolean | null = null;

function readWrap(): boolean {
  if (wrapCache === null) {
    try {
      wrapCache = localStorage.getItem(WRAP_KEY) === "1";
    } catch {
      wrapCache = false;
    }
  }
  return wrapCache;
}

function setWrap(next: boolean) {
  wrapCache = next;
  try {
    localStorage.setItem(WRAP_KEY, next ? "1" : "0");
  } catch {
    // Storage is blocked: the setting lasts for this tab only.
  }
  wrapListeners.forEach((l) => l());
}

function subscribeWrap(listener: () => void) {
  wrapListeners.add(listener);
  return () => {
    wrapListeners.delete(listener);
  };
}

const HIGHLIGHT_THROTTLE_MS = 120;

/** Highlighted tokens for the newest code they still match, at most once per throttle window. */
function useHighlight(language: string, code: string) {
  const [result, setResult] = React.useState<{ code: string; tokens: CodeToken[] } | null>(null);
  const lastRun = React.useRef(0);
  const seq = React.useRef(0);
  const applied = React.useRef(0);
  const mounted = React.useRef(true);
  React.useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  React.useEffect(() => {
    if (!language) return;
    const wait = Math.max(0, lastRun.current + HIGHLIGHT_THROTTLE_MS - Date.now());
    const id = window.setTimeout(() => {
      lastRun.current = Date.now();
      const run = ++seq.current;
      void import("./markdown-highlight")
        .then((m) => m.highlightTokens(language, code))
        .then((tokens) => {
          // A slow parse must not overwrite a newer one that finished first.
          if (!mounted.current || !tokens || run < applied.current) return;
          applied.current = run;
          setResult({ code, tokens });
        })
        .catch(() => {});
    }, wait);
    return () => window.clearTimeout(id);
  }, [language, code]);
  return result && code.startsWith(result.code) ? result : null;
}

const themeVars = (c: CodePalette) =>
  `--md-code-bg:${c.bg};--code-text:${c.text};--code-keyword:${c.keyword};--code-accent:${c.keyword};` +
  `--code-string:${c.string};--code-comment:${c.comment};--code-number:${c.number};--code-function:${c.fn};` +
  `--code-type:${c.type};--code-punct:${c.punct};--code-operator:${c.punct};`;

/** The user's Settings > Code colours for chat code blocks; none for the Skynet themes. */
function CodeThemeStyle() {
  const { light, dark } = useCodeAppearance();
  const rules: string[] = [];
  if (light !== "skynet")
    rules.push(`html:not(.dark) .md-code{${themeVars(codePalette("light", light))}}`);
  if (dark !== "skynet") rules.push(`html.dark .md-code{${themeVars(codePalette("dark", dark))}}`);
  return rules.length ? <style>{rules.join("\n")}</style> : null;
}

const WRAP_CLASS = { on: "whitespace-pre-wrap wrap-anywhere", off: "whitespace-pre" };

function CodeBlock({
  language,
  code,
  open,
  preview,
}: {
  language: string;
  code: string;
  open: boolean;
  /** A rendered view of the code (an image or a formula); the header toggles it with the source. */
  preview?: React.ReactNode;
}) {
  const { onRunCode } = React.useContext(MarkdownContext);
  const wrap = React.useSyncExternalStore(subscribeWrap, readWrap, () => false);
  const highlighted = useHighlight(language, code);
  const [showSource, setShowSource] = React.useState(false);
  const previewing = preview != null && !showSource;
  const wrapLabel = msg(wrap ? "shared.agent.wrap_off" : "shared.agent.wrap_on");
  const viewLabel = msg(showSource ? "shared.agent.show_preview" : "shared.agent.show_source");
  return (
    <div className="md-code">
      <CodeThemeStyle />
      <div className="sticky top-0 z-[1] flex min-h-12 items-center gap-2 bg-[var(--md-code-bg,var(--surface-2))] ps-4 pe-1.5 text-sm font-medium">
        <span className="min-w-0 flex-1 truncate text-foreground/70">
          {codeBlockLabel(language) ||
            msg(language ? "shared.agent.plain_text" : "shared.agent.code_block")}
        </span>
        <div className="flex shrink-0 items-center gap-0.5">
          {preview != null && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={viewLabel}
              onClick={() => setShowSource(!showSource)}
            >
              {showSource ? (
                <Eye className="size-3.5" aria-hidden="true" />
              ) : (
                <Code className="size-3.5" aria-hidden="true" />
              )}
            </Button>
          )}
          {!previewing && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-pressed={wrap}
              aria-label={wrapLabel}
              onClick={() => setWrap(!wrap)}
            >
              <TextAlignLeft className="size-3.5" aria-hidden="true" />
            </Button>
          )}
          {onRunCode && (
            <button
              type="button"
              onClick={() => onRunCode(code, language)}
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-foreground/60 hover:text-foreground/80 hover:bg-black/[0.04] dark:hover:bg-white/[0.06] transition-colors cursor-pointer"
              aria-label={msg("shared.agent.run_code")}
            >
              <Play className="size-3" />
              <span>{msg("shared.agent.run_code")}</span>
            </button>
          )}
          {/* As in Codex, a fence that is still streaming has no copy yet. */}
          {!open && <CopyButton text={code} ariaLabel={msg("shared.agent.copy_code")} />}
        </div>
      </div>
      {previewing ? (
        <div className="overflow-auto px-4 pb-3 md:px-5">{preview}</div>
      ) : (
        <pre
          dir="ltr"
          className={cn(
            "overflow-auto px-4 pb-3 text-left md:px-5",
            wrap ? WRAP_CLASS.on : WRAP_CLASS.off,
          )}
        >
          <code>
            {highlighted ? (
              <>
                {highlighted.tokens.map((t, i) =>
                  t.style ? (
                    <span key={i} style={t.style}>
                      {t.text}
                    </span>
                  ) : (
                    t.text
                  ),
                )}
                {code.slice(highlighted.code.length)}
              </>
            ) : (
              code
            )}
          </code>
        </pre>
      )}
    </div>
  );
}

function MathFallback({ tex }: { tex: string }) {
  return <code dir="ltr">{tex}</code>;
}

function MathFormula({ tex, display }: { tex: string; display: boolean }) {
  return (
    <React.Suspense fallback={<MathFallback tex={tex} />}>
      <MarkdownMath tex={tex} display={display} />
    </React.Suspense>
  );
}

type Edges = { left: boolean; right: boolean };

// The attributes a copied table keeps; the app's classes and data attributes stay behind.
const COPY_ATTRS: Record<string, string[]> = {
  A: ["href", "title"],
  IMG: ["src", "alt", "title", "width", "height"],
  TD: ["align", "colspan", "rowspan"],
  TH: ["align", "colspan", "rowspan"],
};

/** The table as clean HTML for the clipboard, as Codex copies it. */
function tableHtml(table: HTMLTableElement): string {
  const copy = table.cloneNode(true) as HTMLTableElement;
  for (const el of [copy, ...copy.querySelectorAll("*")]) {
    const keep = COPY_ATTRS[el.tagName] ?? [];
    // The renderer writes a column's alignment as a style; pasted HTML reads `align`.
    const align = (el as HTMLElement).style?.textAlign;
    for (const name of el.getAttributeNames()) if (!keep.includes(name)) el.removeAttribute(name);
    if (align && keep.includes("align")) el.setAttribute("align", align);
  }
  return copy.outerHTML;
}

/** Copy the table's Markdown as plain text and its HTML as rich text; plain text only if rich copy fails. */
async function copyTable(markdown: string, html: string): Promise<boolean> {
  try {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/plain": new Blob([markdown], { type: "text/plain" }),
        "text/html": new Blob([html], { type: "text/html" }),
      }),
    ]);
    return true;
  } catch {
    try {
      await navigator.clipboard.writeText(markdown);
      return true;
    } catch {
      return false;
    }
  }
}

function TableCopyButton({
  markdown,
  tableRef,
}: {
  markdown: string;
  tableRef: React.RefObject<HTMLTableElement | null>;
}) {
  const [copiedAt, setCopiedAt] = React.useState(0);
  React.useEffect(() => {
    if (!copiedAt) return;
    const id = window.setTimeout(() => setCopiedAt(0), 1500);
    return () => window.clearTimeout(id);
  }, [copiedAt]);
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      autoTooltip={false}
      aria-label={msg("shared.agent.copy_table")}
      onClick={async () => {
        const table = tableRef.current;
        if (await copyTable(markdown, table ? tableHtml(table) : "")) setCopiedAt(Date.now());
      }}
    >
      <CopyGlyph copied={copiedAt > 0} className="size-3.5" />
    </Button>
  );
}

function TableBlock({
  start,
  end,
  children,
}: {
  start?: number;
  end?: number;
  children: React.ReactNode;
}) {
  const { source } = React.useContext(MarkdownContext);
  const ref = React.useRef<HTMLDivElement>(null);
  const tableRef = React.useRef<HTMLTableElement>(null);
  const [edges, setEdges] = React.useState<Edges | null>(null);
  const [expanded, setExpanded] = React.useState(false);
  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const max = el.scrollWidth - el.clientWidth;
      if (max <= 1) return setEdges(null);
      // In RTL scrollLeft runs from -max to 0.
      const fromLeft =
        getComputedStyle(el).direction === "rtl" ? max + el.scrollLeft : el.scrollLeft;
      setEdges({ left: fromLeft > 1, right: fromLeft < max - 1 });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    el.addEventListener("scroll", measure, { passive: true });
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", measure);
    };
  }, []);
  const markdown = start !== undefined && end !== undefined ? source.slice(start, end) : "";
  return (
    <div className="group/table relative">
      <div
        ref={ref}
        className="md-table-scroll"
        data-fade-left={edges?.left ? "" : undefined}
        data-fade-right={edges?.right ? "" : undefined}
        role={edges ? "region" : undefined}
        tabIndex={edges ? 0 : undefined}
        aria-label={edges ? msg("shared.agent.scrollable_table") : undefined}
      >
        <table ref={tableRef} dir="auto">
          {children}
        </table>
      </div>
      <div className="absolute -top-7 end-0 flex items-center gap-0.5 opacity-0 transition-opacity group-hover/table:opacity-100 focus-within:opacity-100 any-pointer-coarse:opacity-100">
        {markdown && <TableCopyButton markdown={markdown} tableRef={tableRef} />}
        {/* As in Codex, only a table too wide for the message gets a bigger view. */}
        {edges && (
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={msg("shared.agent.expand_table")}
            aria-haspopup="dialog"
            aria-expanded={expanded}
            onClick={() => setExpanded(true)}
          >
            <ArrowsOut className="size-3.5" aria-hidden="true" />
          </Button>
        )}
      </div>
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-fit max-w-[calc(100%-2rem)] flex-col px-4 pt-12 pb-6 sm:max-w-[80vw] sm:px-8"
        >
          <DialogTitle className="sr-only">{msg("shared.agent.table_preview")}</DialogTitle>
          <div
            className="md-root min-h-0 overflow-auto"
            role="region"
            tabIndex={0}
            aria-label={msg("shared.agent.scrollable_table")}
          >
            <table dir="auto">{children}</table>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

type MdNode = { type: string; value?: string; children?: MdNode[] };

// Raw HTML stays text, except `<br>`, which tables need for a line break in a cell.
function remarkBreakTags() {
  const walk = (node: MdNode) => {
    node.children?.forEach((child, i) => {
      if (child.type === "html" && /^<br\s*\/?>$/i.test(child.value?.trim() ?? "")) {
        node.children![i] = { type: "break" };
      } else {
        walk(child);
      }
    });
  };
  return (tree: MdNode) => walk(tree);
}

const REMARK_PLUGINS = [remarkGfm, remarkBreakTags];

type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

const hastText = (node: HastNode): string =>
  node.type === "text" ? (node.value ?? "") : (node.children ?? []).map(hastText).join("");

const childElements = (node: HastNode, tags: string[]) =>
  (node.children ?? []).filter((c) => c.type === "element" && tags.includes(c.tagName ?? ""));

/** Codex's column widths: a size class from the longest cell text in the column. */
function sizeColumns(table: HastNode) {
  const rows = childElements(table, ["thead", "tbody"]).flatMap((s) => childElements(s, ["tr"]));
  const cells = rows.map((row) => childElements(row, ["th", "td"]));
  const longest: number[] = [];
  cells.forEach((row) =>
    row.forEach((cell, i) => {
      longest[i] = Math.max(longest[i] ?? 0, hastText(cell).length);
    }),
  );
  cells.forEach((row) =>
    row.forEach((cell, i) => {
      const n = longest[i] ?? 0;
      const size = n <= 40 ? "sm" : n <= 100 ? "md" : n <= 160 ? "lg" : "xl";
      cell.properties = { ...cell.properties, dataColSize: size };
      if (/^\d+$/.test(hastText(cell))) cell.properties.dataNumeric = "";
    }),
  );
}

function rehypeTableColumns() {
  const walk = (node: HastNode) => {
    if (node.type === "element" && node.tagName === "table") sizeColumns(node);
    else node.children?.forEach(walk);
  };
  return (tree: HastNode) => walk(tree);
}

const REHYPE_PLUGINS = [rehypeTableColumns];

const urlTransform: UrlTransform = (url, key, node) =>
  key === "src" && node.tagName === "img" && isInlineImageSrc(url) ? url : defaultUrlTransform(url);

function PreBlock({ end, children }: { end?: number; children?: React.ReactNode }) {
  const { userTone, openFenceEnd } = React.useContext(MarkdownContext);
  const [svgFailed, setSvgFailed] = React.useState(false);
  const child = React.Children.toArray(children).find(
    (c): c is React.ReactElement<{ className?: string; children?: React.ReactNode }> =>
      React.isValidElement(c),
  );
  const language = /language-([^\s]+)/.exec(child?.props.className ?? "")?.[1] ?? "";
  const code = extractCodeText(child?.props.children).replace(/\n$/, "");
  if (userTone) {
    return (
      <pre
        dir="ltr"
        className="my-2 overflow-x-auto whitespace-pre rounded-xl bg-current/10 p-2 text-left"
      >
        {code}
      </pre>
    );
  }
  if (language === DISPLAY_MATH_LANG) return <MathFormula tex={code} display />;
  const open = openFenceEnd !== null && end !== undefined && end >= openFenceEnd;
  // A half-streamed image or formula would flicker, so it shows as code until its fence closes.
  const preview = open ? undefined : isSvgFence(language, code) && !svgFailed ? (
    // An <img> never runs the SVG's scripts or loads its links, unlike inline markup.
    // SVG that does not parse shows its source, not a broken image.
    <img
      src={svgDataUrl(code)}
      alt="SVG"
      className="max-h-96 max-w-full"
      onError={() => setSvgFailed(true)}
    />
  ) : isMathFence(language) ? (
    <MathFormula tex={code} display />
  ) : undefined;
  // As in Codex, a fence with no tag gets a guessed language; text outside fences gets none.
  return (
    <CodeBlock
      language={language || (open ? "" : guessLanguage(code))}
      code={code}
      open={open}
      preview={preview}
    />
  );
}

const COMPONENTS: Components = {
  p: ({ node: _node, ...props }) => <p dir="auto" {...props} />,
  h1: ({ node: _node, ...props }) => <h1 dir="auto" {...props} />,
  h2: ({ node: _node, ...props }) => <h2 dir="auto" {...props} />,
  h3: ({ node: _node, ...props }) => <h3 dir="auto" {...props} />,
  h4: ({ node: _node, ...props }) => <h4 dir="auto" {...props} />,
  h5: ({ node: _node, ...props }) => <h5 dir="auto" {...props} />,
  h6: ({ node: _node, ...props }) => <h6 dir="auto" {...props} />,
  ul: ({ node: _node, ...props }) => <ul dir="auto" {...props} />,
  ol: ({ node: _node, ...props }) => <ol dir="auto" {...props} />,
  blockquote: ({ node: _node, ...props }) => <blockquote dir="auto" {...props} />,
  a: ({ node: _node, children, href, ...rest }) => (
    <a
      {...rest}
      href={href === undefined ? href : docsLinkHref(href)}
      target="_blank"
      rel="noreferrer noopener"
    >
      {children}
    </a>
  ),
  code: ({ node: _node, children, ...rest }) => {
    const text = typeof children === "string" ? children : "";
    if (text.startsWith(INLINE_MATH_MARK))
      return <MathFormula tex={text.slice(1)} display={false} />;
    return (
      <code dir="ltr" {...rest}>
        {children}
      </code>
    );
  },
  pre: ({ node, children }) => <PreBlock end={node?.position?.end.offset}>{children}</PreBlock>,
  table: ({ node, children }) => (
    <TableBlock start={node?.position?.start.offset} end={node?.position?.end.offset}>
      {children}
    </TableBlock>
  ),
  img: ({ node: _node, src, alt }) =>
    // A remote image would load with no click, so an injected reply could send chat data to
    // any host in its URL. A link would skip `allowLink` and can nest inside a Markdown link.
    typeof src === "string" && isInlineImageSrc(src) ? (
      <img src={src} alt={alt ?? ""} />
    ) : (
      <span>{alt}</span>
    ),
};

export interface MessageMarkdownProps {
  content: string;
  className?: string;
  onRunCode?: (code: string, language: string) => void;
  /** When set, a link whose href fails this check renders as plain text. */
  allowLink?: (href: string) => boolean;
  /** The user's own message: plain code fences, no paragraph margins. */
  tone?: "user-message";
  /** The reply is still streaming, so its half-typed tail gets repaired. */
  streaming?: boolean;
}

const LinkRenderer = COMPONENTS.a as React.ComponentType<React.ComponentProps<"a">>;

export function MessageMarkdown({
  content,
  className,
  onRunCode,
  allowLink,
  tone,
  streaming,
}: MessageMarkdownProps) {
  const userTone = tone === "user-message";
  const source = React.useMemo(() => {
    if (userTone) return content;
    return prepareMath(streaming ? repairStreamingMarkdown(content) : content);
  }, [content, streaming, userTone]);
  const openFenceEnd =
    streaming && !userTone && endsInOpenFence(source) ? source.trimEnd().length : null;
  const ctx = React.useMemo<MarkdownContextValue>(
    () => ({ onRunCode, source, userTone, openFenceEnd }),
    [onRunCode, source, userTone, openFenceEnd],
  );
  const { font, size } = useCodeAppearance();
  const codeFont = safeFontFamily(font);
  // Settings > Code: the font and size for code blocks, and the font for inline code.
  const codeStyle = {
    "--md-code-font": codeFont ? `${codeFont}, var(--font-mono)` : undefined,
    "--md-code-size": size > 0 ? `${size}px` : undefined,
  } as React.CSSProperties;
  const components = React.useMemo<Components>(
    () =>
      allowLink
        ? {
            ...COMPONENTS,
            a: ({ node: _node, ...props }) =>
              props.href !== undefined && allowLink(props.href) ? (
                <LinkRenderer {...props} />
              ) : (
                <>{props.children}</>
              ),
          }
        : COMPONENTS,
    [allowLink],
  );
  return (
    <MarkdownContext.Provider value={ctx}>
      {/* React hoists this into the head once, however many messages render. */}
      <style href="skynet-message-markdown" precedence="default">
        {MARKDOWN_CSS}
      </style>
      <div
        dir="auto"
        className={cn("md-root", userTone ? "md-user" : "md-fade", className)}
        style={codeStyle}
      >
        <ReactMarkdown
          remarkPlugins={REMARK_PLUGINS}
          rehypePlugins={REHYPE_PLUGINS}
          components={components}
          urlTransform={urlTransform}
        >
          {source}
        </ReactMarkdown>
      </div>
    </MarkdownContext.Provider>
  );
}
