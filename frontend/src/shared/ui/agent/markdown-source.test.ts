import test from "node:test";
import assert from "node:assert/strict";

import {
  INLINE_MATH_MARK,
  codeBlockLabel,
  endsInOpenFence,
  isInlineImageSrc,
  prepareMath,
  repairStreamingMarkdown,
} from "./markdown-source.ts";

const inline = (tex: string) => "`` " + INLINE_MATH_MARK + tex + " ``";

test("prepareMath turns display math on its own lines into a math fence", () => {
  assert.equal(prepareMath("a\n$$\nx^2\n$$\nb"), "a\n```math\nx^2\n```\nb");
  assert.equal(prepareMath("\\[ \\frac{1}{2} \\]"), "```math\n\\frac{1}{2}\n```");
});

test("prepareMath turns \\(…\\) and mid-line $$…$$ into inline math", () => {
  assert.equal(prepareMath("so \\(a+b\\) holds"), `so ${inline("a+b")} holds`);
  assert.equal(prepareMath("see $$x$$ here"), `see ${inline("x")} here`);
});

test("prepareMath keeps single dollars, code spans and code fences as they are", () => {
  assert.equal(prepareMath("costs $5 or $6"), "costs $5 or $6");
  assert.equal(prepareMath("run `echo $$x$$` now"), "run `echo $$x$$` now");
  const fenced = "```sh\necho $$PID$$\n```\n";
  assert.equal(prepareMath(fenced), fenced);
});

test("repairStreamingMarkdown cuts a link or image that has no full target yet", () => {
  assert.equal(repairStreamingMarkdown("See [the do"), "See ");
  assert.equal(repairStreamingMarkdown("See [docs](https://exa"), "See ");
  assert.equal(repairStreamingMarkdown("Look ![alt"), "Look ");
  assert.equal(
    repairStreamingMarkdown("See [docs](https://a.b) now"),
    "See [docs](https://a.b) now",
  );
});

test("repairStreamingMarkdown closes a dangling emphasis and drops a bare opener", () => {
  assert.equal(repairStreamingMarkdown("This is **bold"), "This is **bold**");
  assert.equal(repairStreamingMarkdown("This is *it"), "This is *it*");
  assert.equal(repairStreamingMarkdown("Next **"), "Next ");
  assert.equal(repairStreamingMarkdown("**done** and *ok*"), "**done** and *ok*");
});

test("repairStreamingMarkdown ignores list bullets, code spans and earlier paragraphs", () => {
  assert.equal(repairStreamingMarkdown("* one\n* two"), "* one\n* two");
  assert.equal(repairStreamingMarkdown("use `a*b` here"), "use `a*b` here");
  assert.equal(repairStreamingMarkdown("an *open\n\nnext"), "an *open\n\nnext");
});

test("repairStreamingMarkdown leaves an open code fence alone", () => {
  const open = "```py\nx = [1, **kw";
  assert.equal(repairStreamingMarkdown(open), open);
});

test("codeBlockLabel shows a language's display name and keeps unknown fences", () => {
  assert.equal(codeBlockLabel("html"), "HTML");
  assert.equal(codeBlockLabel("python"), "Python");
  assert.equal(codeBlockLabel("ts"), "TypeScript");
  assert.equal(codeBlockLabel("C++"), "C++");
  assert.equal(codeBlockLabel("zsh"), "Bash");
  assert.equal(codeBlockLabel("yml"), "YAML");
  assert.equal(codeBlockLabel("text"), "");
  assert.equal(codeBlockLabel("plaintext"), "");
  assert.equal(codeBlockLabel("brainfuck"), "brainfuck");
});

test("endsInOpenFence finds a fence that is not closed yet", () => {
  assert.equal(endsInOpenFence("a\n```py\nx = 1"), true);
  assert.equal(endsInOpenFence("```py\nx = 1\n```\n"), false);
  assert.equal(endsInOpenFence("```py\nx\n```\nafter"), false);
  assert.equal(endsInOpenFence("no code"), false);
});

test("isInlineImageSrc allows only inline raster images", () => {
  assert.equal(isInlineImageSrc("data:image/png;base64,AAAA"), true);
  assert.equal(isInlineImageSrc("https://a.b/c.png"), false);
  assert.equal(isInlineImageSrc("data:image/svg+xml;base64,AAAA"), false);
  assert.equal(isInlineImageSrc("http://a.b/c.png"), false);
  assert.equal(isInlineImageSrc("javascript:alert(1)"), false);
});

test("repairStreamingMarkdown looks only at prose after the last code fence", () => {
  const text = "```\na\n\nb *c\n```\nend *x";
  assert.equal(repairStreamingMarkdown(text), "```\na\n\nb *c\n```\nend *x*");
});
