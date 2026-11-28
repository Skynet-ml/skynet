import test from "node:test";
import assert from "node:assert/strict";

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
} from "./markdown-source.ts";

const inline = (tex: string) => "`` " + INLINE_MATH_MARK + tex + " ``";

test("prepareMath turns display math on its own lines into a math fence", () => {
  const fence = "```" + DISPLAY_MATH_LANG;
  assert.equal(prepareMath("a\n$$\nx^2\n$$\nb"), `a\n${fence}\nx^2\n\`\`\`\nb`);
  assert.equal(prepareMath("\\[ \\frac{1}{2} \\]"), `${fence}\n\\frac{1}{2}\n\`\`\``);
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

test("repairStreamingMarkdown closes a half-typed code span", () => {
  assert.equal(repairStreamingMarkdown("run `npm te"), "run `npm te`");
  assert.equal(repairStreamingMarkdown("run `a*b"), "run `a*b`");
  assert.equal(repairStreamingMarkdown("run `"), "run ");
  assert.equal(repairStreamingMarkdown("run `x` and `y`"), "run `x` and `y`");
  assert.equal(repairStreamingMarkdown("use ``a`b`` here"), "use ``a`b`` here");
  assert.equal(repairStreamingMarkdown("run ``a` b"), "run ``a` b``");
  assert.equal(repairStreamingMarkdown("type ``foo"), "type ``foo``");
  assert.equal(repairStreamingMarkdown("type ``"), "type ");
  assert.equal(repairStreamingMarkdown("a \\` b"), "a \\` b");
  assert.equal(repairStreamingMarkdown("a \\\\`b"), "a \\\\`b`");
  assert.equal(repairStreamingMarkdown("use ``a*b`` and *it"), "use ``a*b`` and *it*");
});

test("isMathFence and isSvgFence pick the fences that get a preview", () => {
  assert.equal(isMathFence("latex"), true);
  assert.equal(isMathFence("TeX"), true);
  assert.equal(isMathFence("math"), true);
  assert.equal(isMathFence(DISPLAY_MATH_LANG), false);
  assert.equal(isSvgFence("svg", "anything"), true);
  assert.equal(isSvgFence("xml", '  <svg xmlns="http://www.w3.org/2000/svg"/>'), true);
  assert.equal(isSvgFence("html", "<svg>"), true);
  assert.equal(isSvgFence("html", "<div><svg></svg></div>"), false);
  assert.equal(isSvgFence("xml", "<svgx/>"), false);
  assert.equal(isSvgFence("js", "<svg>"), false);
});

test("svgDataUrl encodes the markup so it cannot leave the data URL", () => {
  const url = svgDataUrl(' <svg><a href="#">x</a></svg>\n');
  assert.ok(url.startsWith("data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns"), url);
  assert.ok(!/["<> ]/.test(url.slice(url.indexOf(","))), url);
});

test("svgDataUrl adds the SVG namespace only when the root has none", () => {
  const svg = (code: string) => decodeURIComponent(svgDataUrl(code).split(",")[1]!);
  assert.equal(
    svg("<svg><circle r='1'/></svg>"),
    `<svg xmlns="http://www.w3.org/2000/svg"><circle r='1'/></svg>`,
  );
  const own = '<svg viewBox="0 0 1 1" xmlns="http://www.w3.org/2000/svg"></svg>';
  assert.equal(svg(own), own);
});

test("guessLanguage names an untagged fence only on a strong sign", () => {
  assert.equal(guessLanguage('{"a": [1, 2]}'), "json");
  assert.equal(guessLanguage("{not json"), "");
  assert.equal(guessLanguage("diff --git a/x b/x\n@@ -1 +1 @@"), "diff");
  assert.equal(guessLanguage("npm install\nnpm test"), "bash");
  assert.equal(guessLanguage("$ ls -la"), "bash");
  assert.equal(guessLanguage("def f(x):\n    return x"), "python");
  assert.equal(guessLanguage("from a.b import c"), "python");
  assert.equal(guessLanguage("interface A {\n  x: number;\n}"), "typescript");
  assert.equal(guessLanguage('import x from "y";\nconst a = 1;'), "javascript");
  assert.equal(guessLanguage("package main\n\nfunc main() {}"), "go");
  assert.equal(guessLanguage("fn main() {\n    let mut x = 1;\n}"), "rust");
  assert.equal(guessLanguage("SELECT id FROM users;"), "sql");
  assert.equal(guessLanguage(".a {\n  color: red;\n}"), "css");
  assert.equal(guessLanguage("name: app\nsteps:\n  - run: x"), "yaml");
  assert.equal(guessLanguage("<!DOCTYPE html>\n<html></html>"), "html");
  assert.equal(guessLanguage('<?xml version="1.0"?>\n<a/>'), "xml");
  assert.equal(guessLanguage("plain"), "");
  assert.equal(guessLanguage("Note: read this first.\nThen go on."), "");
  assert.equal(guessLanguage("make sure you select the files from the list"), "");
  assert.equal(guessLanguage(""), "");
  for (const prose of [
    "Select the rows from the table you want.",
    "cd into the folder and run it",
    "just run the tests again",
    "export the data please",
    "echo is a sound",
    "let x = 1 means x is one",
    "type Foo = bar",
    "Please let me know: number of rows",
    "function of x: string theory",
    "Error: file not found\nHint: check path",
    "TODO: fix\nNOTE: later",
    "Q: why?\nA: because",
    "Usage: foo\nOptions:",
  ])
    assert.equal(guessLanguage(prose), "", prose);
  assert.equal(guessLanguage("cd ./app\nls -la"), "bash");
  assert.equal(guessLanguage("export FOO=1"), "bash");
  assert.equal(guessLanguage("type Id = string;"), "typescript");
  assert.equal(guessLanguage("const id: string = f();"), "typescript");
  assert.equal(guessLanguage("function f(): number {}"), "typescript");
  assert.equal(guessLanguage("let x = f(1);"), "javascript");
});
