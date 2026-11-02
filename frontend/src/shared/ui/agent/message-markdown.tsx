"use client";

import * as React from "react";
import { Play } from "@/shared/ui/icons";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { docsLinkHref } from "@/shared/lib/docs-link";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CopyButton } from "@/shared/ui/copy-button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/primitives/table";

interface RunCodeContextValue {
  onRunCode?: (code: string, language: string) => void;
}

const RunCodeContext = React.createContext<RunCodeContextValue>({});

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

interface CodeBlockProps {
  language: string;
  rawCode: string;
  children: React.ReactNode;
}

function CodeBlock({ language, rawCode, children }: CodeBlockProps) {
  const { onRunCode } = React.useContext(RunCodeContext);
  const langLabel = language || "code";
  return (
    <div
      dir="ltr"
      className="my-2 overflow-hidden rounded-xl bg-black/[0.06] text-left ring-1 ring-black/[0.04] dark:bg-white/[0.06] dark:ring-white/[0.06]"
    >
      <div className="flex items-center justify-between gap-2 border-b border-black/[0.06] dark:border-white/[0.08] px-3 py-1.5">
        <span className="text-[10px] uppercase tracking-wider font-mono text-foreground/50">
          {langLabel}
        </span>
        <div className="flex items-center gap-0.5">
          {onRunCode && (
            <button
              type="button"
              onClick={() => onRunCode(rawCode, language)}
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] text-foreground/50 hover:text-foreground/80 hover:bg-black/[0.04] dark:hover:bg-white/[0.06] transition-colors cursor-pointer"
              aria-label={msg("shared.agent.run_code")}
            >
              <Play className="size-3" />
              <span>{msg("shared.agent.run_code")}</span>
            </button>
          )}
          <CopyButton text={rawCode} ariaLabel={msg("shared.agent.copy_code")} />
        </div>
      </div>
      <pre className="overflow-x-auto p-3">{children}</pre>
    </div>
  );
}

const COMPONENTS: Components = {
  p: ({ children, ...rest }) => (
    <p dir="auto" className="whitespace-pre-wrap [&:not(:first-child)]:mt-2" {...rest}>
      {children}
    </p>
  ),
  strong: ({ children, ...rest }) => (
    <strong className="font-semibold" {...rest}>
      {children}
    </strong>
  ),
  em: ({ children, ...rest }) => (
    <em className="italic" {...rest}>
      {children}
    </em>
  ),
  del: ({ children, ...rest }) => (
    <del className="line-through opacity-70" {...rest}>
      {children}
    </del>
  ),
  a: ({ children, href, ...rest }) => (
    <a
      {...rest}
      href={href === undefined ? href : docsLinkHref(href)}
      target="_blank"
      rel="noreferrer noopener"
      className="underline underline-offset-2 hover:opacity-80 transition-opacity"
    >
      {children}
    </a>
  ),
  ul: ({ children, ...rest }) => (
    <ul dir="auto" className="my-2 list-disc ps-5 space-y-1 marker:text-current/60" {...rest}>
      {children}
    </ul>
  ),
  ol: ({ children, ...rest }) => (
    <ol dir="auto" className="my-2 list-decimal ps-5 space-y-1 marker:text-current/60" {...rest}>
      {children}
    </ol>
  ),
  li: ({ children, ...rest }) => (
    <li className="leading-relaxed" {...rest}>
      {children}
    </li>
  ),
  blockquote: ({ children, ...rest }) => (
    <blockquote
      dir="auto"
      className="my-2 ps-3 border-s-2 border-current/20 opacity-90 italic"
      {...rest}
    >
      {children}
    </blockquote>
  ),
  h1: ({ children, ...rest }) => (
    <h3 dir="auto" className="text-base font-semibold [&:not(:first-child)]:mt-3 mb-1" {...rest}>
      {children}
    </h3>
  ),
  h2: ({ children, ...rest }) => (
    <h4 dir="auto" className="text-sm font-semibold [&:not(:first-child)]:mt-3 mb-1" {...rest}>
      {children}
    </h4>
  ),
  h3: ({ children, ...rest }) => (
    <h5 dir="auto" className="text-sm font-semibold [&:not(:first-child)]:mt-2 mb-0.5" {...rest}>
      {children}
    </h5>
  ),
  h4: ({ children, ...rest }) => (
    <h6 dir="auto" className="text-sm font-medium [&:not(:first-child)]:mt-2 mb-0.5" {...rest}>
      {children}
    </h6>
  ),
  code: ({ className, children, ...rest }) => {
    const isBlock = /language-/.test(className ?? "");
    if (isBlock) {
      return (
        <code className={cn("font-mono text-[0.8125rem] leading-relaxed", className)} {...rest}>
          {children}
        </code>
      );
    }
    return (
      <code
        dir="ltr"
        className="inline-block rounded-md bg-black/[0.06] dark:bg-white/[0.08] px-1.5 py-px font-mono text-[0.8125em] align-baseline"
        {...rest}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => {
    const child = React.Children.toArray(children).find(
      (c): c is React.ReactElement<{ className?: string; children?: React.ReactNode }> =>
        React.isValidElement(c),
    );
    const className = child?.props.className ?? "";
    const langMatch = /language-([\w-]+)/.exec(className);
    const language = langMatch?.[1] ?? "";
    const rawCode = extractCodeText(child?.props.children).replace(/\n$/, "");
    return (
      <CodeBlock language={language} rawCode={rawCode}>
        {children}
      </CodeBlock>
    );
  },
  hr: ({ ...rest }) => <hr className="my-3 border-current/15" {...rest} />,
  table: ({ children, ...rest }) => (
    <div className="my-2 overflow-hidden rounded-lg border border-border/60">
      <Table dir="auto" className="text-[0.8125rem]" {...rest}>
        {children}
      </Table>
    </div>
  ),
  thead: ({ children, ...rest }) => <TableHeader {...rest}>{children}</TableHeader>,
  tbody: ({ children, ...rest }) => <TableBody {...rest}>{children}</TableBody>,
  tr: ({ children, ...rest }) => <TableRow {...rest}>{children}</TableRow>,
  th: ({ children, ...rest }) => (
    <TableHead className="h-auto py-1.5" {...rest}>
      {children}
    </TableHead>
  ),
  td: ({ children, ...rest }) => (
    <TableCell className="px-2 py-1.5 whitespace-normal break-words" {...rest}>
      {children}
    </TableCell>
  ),
};

export interface MessageMarkdownProps {
  content: string;
  className?: string;
  onRunCode?: (code: string, language: string) => void;
  /** When set, a link whose href fails this check renders as plain text. */
  allowLink?: (href: string) => boolean;
}

const LinkRenderer = COMPONENTS.a as React.ComponentType<React.ComponentProps<"a">>;

export function MessageMarkdown({ content, className, onRunCode, allowLink }: MessageMarkdownProps) {
  const ctx = React.useMemo<RunCodeContextValue>(() => ({ onRunCode }), [onRunCode]);
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
    <RunCodeContext.Provider value={ctx}>
      <div className={cn("break-words", className)}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {content}
        </ReactMarkdown>
      </div>
    </RunCodeContext.Provider>
  );
}
