"use client";

import { useState } from "react";
import { readOnlyEditorHeight } from "@/shared/ui/code-editor-height";
import { LazyCodeEditor as CodeEditor } from "@/shared/ui/lazy-code-editor";
import { LangPicker } from "./ui-primitives";

export type ServeSnippetLang = "curl" | "python" | "javascript" | "go";

const LABELS: Record<ServeSnippetLang, string> = {
  curl: "cURL",
  python: "Python",
  javascript: "JavaScript",
  go: "Go",
};

const SAMPLE_INPUT = "Hello";

/** The ready-to-run call of a run's serve endpoint, in each snippet language. */
export function buildServeSnippets(url: string): Record<ServeSnippetLang, string> {
  const body = JSON.stringify({ inputs: { input: SAMPLE_INPUT } });
  const tokenHint = "Generate a token in Settings → API and set APP_API_TOKEN in your env";
  return {
    curl: [
      `# ${tokenHint}`,
      `curl -X POST ${url} \\`,
      `  -H "Authorization: Bearer $APP_API_TOKEN" \\`,
      `  -H "Idempotency-Key: $(uuidgen)" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d '${body}'`,
    ].join("\n"),
    python: [
      `import os`,
      `import uuid`,
      `import requests`,
      ``,
      `# ${tokenHint}`,
      `token = os.environ["APP_API_TOKEN"]`,
      ``,
      `response = requests.post(`,
      `    "${url}",`,
      `    headers={`,
      `        "Authorization": f"Bearer {token}",`,
      `        "Idempotency-Key": str(uuid.uuid4()),`,
      `    },`,
      `    json={"inputs": {"input": ${JSON.stringify(SAMPLE_INPUT)}}},`,
      `)`,
      `print(response.json()["outputs"])`,
    ].join("\n"),
    javascript: [
      `// ${tokenHint}`,
      `const token = process.env.APP_API_TOKEN;`,
      ``,
      `const response = await fetch("${url}", {`,
      `  method: "POST",`,
      `  headers: {`,
      `    "Authorization": "Bearer " + token,`,
      `    "Idempotency-Key": crypto.randomUUID(),`,
      `    "Content-Type": "application/json",`,
      `  },`,
      `  body: JSON.stringify({ inputs: { input: ${JSON.stringify(SAMPLE_INPUT)} } }),`,
      `});`,
      `console.log((await response.json()).outputs);`,
    ].join("\n"),
    go: [
      `package main`,
      ``,
      `import (`,
      `\t"bytes"`,
      `\t"crypto/rand"`,
      `\t"encoding/hex"`,
      `\t"encoding/json"`,
      `\t"fmt"`,
      `\t"net/http"`,
      `\t"os"`,
      `)`,
      ``,
      `func main() {`,
      `\t// ${tokenHint}`,
      `\ttoken := os.Getenv("APP_API_TOKEN")`,
      ``,
      `\tpayload, _ := json.Marshal(map[string]any{`,
      `\t\t"inputs": map[string]any{"input": ${JSON.stringify(SAMPLE_INPUT)}},`,
      `\t})`,
      `\treq, _ := http.NewRequest("POST", "${url}", bytes.NewReader(payload))`,
      `\treq.Header.Set("Authorization", "Bearer "+token)`,
      `\t// A new key for each call: the same key returns the first reply again.`,
      `\tkey := make([]byte, 16)`,
      `\trand.Read(key)`,
      `\treq.Header.Set("Idempotency-Key", hex.EncodeToString(key))`,
      `\treq.Header.Set("Content-Type", "application/json")`,
      `\tresp, _ := http.DefaultClient.Do(req)`,
      `\tdefer resp.Body.Close()`,
      ``,
      `\tvar result map[string]any`,
      `\tjson.NewDecoder(resp.Body).Decode(&result)`,
      `\tfmt.Println(result["outputs"])`,
      `}`,
    ].join("\n"),
  };
}

/** Read-only code that calls a repository run's serve endpoint, with a language picker. */
export function ServeCodeSnippets({ url }: { url: string }) {
  const [lang, setLang] = useState<ServeSnippetLang>("curl");
  const snippet = buildServeSnippets(url)[lang];
  return (
    <CodeEditor
      value={snippet}
      onChange={() => {}}
      height={readOnlyEditorHeight(snippet)}
      readOnly
      label={<LangPicker value={lang} onChange={setLang} labels={LABELS} />}
    />
  );
}
