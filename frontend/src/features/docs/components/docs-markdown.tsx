import * as React from "react";
import Link from "next/link";
import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Info, Lightbulb, Warning } from "@/shared/ui/icons";
import { CopyButton } from "@/shared/ui/copy-button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { DocsTabs } from "./docs-tabs";
import type { DocsChrome } from "../lib/chrome";
import { highlightDocsCode } from "../lib/highlight";
import { remarkDocs, type CalloutKind } from "../lib/markdown";
import type { DocsLocale } from "../lib/nav";
import {
  MCP_CLIENTS,
  mcpClientMarkdown,
  mcpClientsCheckedLine,
  mcpText,
} from "@/shared/lib/mcp-clients";
import { serviceUrls } from "@/shared/lib/site-config";

/** The text inside rendered Markdown children. */
function textOf(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (React.isValidElement(node))
    return textOf((node.props as { children?: React.ReactNode }).children);
  return "";
}

/** A code block with server-side colours and a copy button. */
async function CodeBlock({
  language,
  code,
  copyLabel,
}: {
  language: string;
  code: string;
  copyLabel: string;
}) {
  const lines = await highlightDocsCode(language, code);
  return (
    <div
      dir="ltr"
      className="group relative my-4 overflow-hidden rounded-lg border border-border bg-muted/50 text-start"
    >
      <div className="absolute end-1.5 top-1.5 opacity-70 transition-opacity group-hover:opacity-100">
        <Tooltip>
          <TooltipTrigger asChild>
            <CopyButton text={code} ariaLabel={copyLabel} />
          </TooltipTrigger>
          <TooltipContent>{copyLabel}</TooltipContent>
        </Tooltip>
      </div>
      <pre className="overflow-x-auto py-3 ps-4 pe-12 font-mono text-[0.8125rem] leading-relaxed">
        <code>
          {lines
            ? lines.map((line, i) => (
                <React.Fragment key={i}>
                  {i > 0 && "\n"}
                  {line.map((token, j) =>
                    token.spec ? (
                      <span
                        key={j}
                        style={{
                          color: token.spec.color,
                          fontWeight: token.spec.fontWeight,
                          fontStyle: token.spec.fontStyle,
                        }}
                      >
                        {token.text}
                      </span>
                    ) : (
                      <React.Fragment key={j}>{token.text}</React.Fragment>
                    ),
                  )}
                </React.Fragment>
              ))
            : code}
        </code>
      </pre>
    </div>
  );
}

const CALLOUT_STYLE: Record<CalloutKind, { Icon: typeof Info; className: string }> = {
  note: {
    Icon: Info,
    className: "border-sky-500/40 bg-sky-500/[0.06] [&_svg]:text-sky-600 dark:[&_svg]:text-sky-400",
  },
  tip: {
    Icon: Lightbulb,
    className:
      "border-emerald-500/40 bg-emerald-500/[0.06] [&_svg]:text-emerald-600 dark:[&_svg]:text-emerald-400",
  },
  warning: {
    Icon: Warning,
    className:
      "border-amber-500/50 bg-amber-500/[0.08] [&_svg]:text-amber-600 dark:[&_svg]:text-amber-400",
  },
};

/** Render one docs page body (or one MCP client panel) from Markdown. */
export function DocsMarkdown({
  source,
  headingIds = [],
  locale,
  chrome,
}: {
  source: string;
  headingIds?: readonly string[];
  locale: DocsLocale;
  chrome: DocsChrome;
}) {
  const components: Components = {
    a: ({ href = "", children }) => {
      if (href.startsWith("/") && !href.startsWith("//")) {
        return (
          <Link href={href} className="font-medium text-primary underline-offset-4 hover:underline">
            {children}
          </Link>
        );
      }
      const external = /^https?:/.test(href);
      return (
        <a
          href={href}
          className="font-medium text-primary underline-offset-4 hover:underline"
          {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
        >
          {children}
        </a>
      );
    },
    pre: ({ children }) => {
      const child = React.Children.only(children) as React.ReactElement<{
        className?: string;
        children?: React.ReactNode;
      }>;
      const language = child.props.className?.match(/language-([\w-]+)/)?.[1] ?? "";
      const code = textOf(child.props.children).replace(/\n$/, "");
      return <CodeBlock language={language} code={code} copyLabel={chrome.copyCode} />;
    },
    blockquote: ({ children, node, ...rest }) => {
      const kind = (rest as Record<string, unknown>)["data-callout"] as CalloutKind | undefined;
      void node;
      if (!kind) {
        return (
          <blockquote className="my-4 border-s-2 border-border ps-4 text-muted-foreground">
            {children}
          </blockquote>
        );
      }
      const { Icon, className } = CALLOUT_STYLE[kind];
      return (
        <aside
          className={`my-5 flex gap-3 rounded-lg border px-4 py-3 text-[0.9375rem] ${className}`}
        >
          <Icon className="mt-1 size-4 shrink-0" aria-label={chrome[kind]} />
          <div className="min-w-0 [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">{children}</div>
        </aside>
      );
    },
    div: ({ children, node, ...rest }) => {
      void node;
      const props = rest as Record<string, unknown>;
      if (typeof props["data-tabs"] === "string") {
        const labels = JSON.parse(props["data-tabs"]) as string[];
        return (
          <DocsTabs
            labels={labels}
            panels={React.Children.toArray(children).filter(React.isValidElement)}
          />
        );
      }
      if (props["data-mcp-clients"] !== undefined) {
        return (
          <>
            <p className="text-sm text-muted-foreground">{mcpClientsCheckedLine(locale)}</p>
            <DocsTabs
              labels={MCP_CLIENTS.map((client) => mcpText(client.name, locale))}
              panels={MCP_CLIENTS.map((client) => (
                <DocsMarkdown
                  key={client.id}
                  source={mcpClientMarkdown(client, locale, serviceUrls().mcp)}
                  locale={locale}
                  chrome={chrome}
                />
              ))}
            />
          </>
        );
      }
      return <div>{children}</div>;
    },
    table: ({ children }) => (
      <div className="my-5 overflow-x-auto rounded-lg border border-border">
        <table className="w-full border-collapse text-sm">{children}</table>
      </div>
    ),
  };

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, [remarkDocs, { headingIds }]]}
      components={components}
      // Keep Cursor's one-click install link; the default drops unknown schemes.
      urlTransform={(url) => (url.startsWith("cursor://") ? url : defaultUrlTransform(url))}
    >
      {source}
    </ReactMarkdown>
  );
}
