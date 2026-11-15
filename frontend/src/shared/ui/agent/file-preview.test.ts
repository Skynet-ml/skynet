/** Tests for picking how an attached file's text is shown. */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { filePreview } from "./file-preview.ts";

void describe("filePreview", () => {
  void it("reads a CSV file as one table, with quoted commas kept in their cell", () => {
    assert.deepEqual(filePreview("csv", false, 'name,note\nAda,"a, b"\n'), {
      kind: "tables",
      tables: [
        {
          name: "",
          rows: [
            ["name", "note"],
            ["Ada", "a, b"],
          ],
        },
      ],
      note: "",
    });
  });

  void it("reads the backend's workbook text as one table per sheet", () => {
    const preview = filePreview("xlsx", false, "Sheet: Q1\na\tb\n1\t2\n\nSheet: Q2\nc");
    assert.deepEqual(preview, {
      kind: "tables",
      tables: [
        {
          name: "Q1",
          rows: [
            ["a", "b"],
            ["1", "2"],
          ],
        },
        { name: "Q2", rows: [["c"]] },
      ],
      note: "",
    });
  });

  void it("keeps the backend's truncation line as a note, out of the table", () => {
    const preview = filePreview("csv", false, "a,b\n1,2\n\n[Truncated: 900 more rows]");
    assert.equal(preview?.kind, "tables");
    assert.equal(preview?.note, "[Truncated: 900 more rows]");
    assert.deepEqual(preview?.kind === "tables" && preview.tables[0]?.rows, [
      ["a", "b"],
      ["1", "2"],
    ]);
  });

  void it("shows source code as code in its language", () => {
    assert.deepEqual(filePreview("py", true, "print(1)\n"), {
      kind: "code",
      language: "py",
      text: "print(1)\n",
      note: "",
    });
  });

  void it("shows a Markdown file as Markdown", () => {
    assert.deepEqual(filePreview("md", false, "# Title"), {
      kind: "markdown",
      text: "# Title",
      note: "",
    });
  });

  void it("gives no preview for an empty file or one the backend could not read", () => {
    assert.equal(filePreview("txt", false, "  \n"), null);
    assert.equal(filePreview("pdf", false, "[Attached file report.pdf could not be read]"), null);
  });
});
