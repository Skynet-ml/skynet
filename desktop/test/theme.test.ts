import { describe, expect, it } from "vitest";
import { isOpenAppUrl, parseTheme, pickTheme } from "../src/main/theme";

describe("parseTheme", () => {
  it("accepts only light and dark", () => {
    expect(parseTheme("light")).toBe("light");
    expect(parseTheme("dark")).toBe("dark");
    for (const value of ["Dark", "system", "", " dark", null, undefined, 1, { theme: "dark" }]) {
      expect(parseTheme(value)).toBeNull();
    }
  });
});

describe("pickTheme", () => {
  it("lets the reported theme win over the system appearance", () => {
    expect(pickTheme("dark", false)).toBe("dark");
    expect(pickTheme("light", true)).toBe("light");
  });

  it("falls back to the system appearance without a reported theme", () => {
    expect(pickTheme(null, true)).toBe("dark");
    expect(pickTheme(null, false)).toBe("light");
  });

  it("ignores an invalid report", () => {
    expect(pickTheme(parseTheme("purple"), true)).toBe("dark");
    expect(pickTheme(parseTheme("purple"), false)).toBe("light");
  });
});

describe("isOpenAppUrl", () => {
  it("matches skynet://open", () => {
    expect(isOpenAppUrl("skynet://open")).toBe(true);
    expect(isOpenAppUrl("skynet://open/")).toBe(true);
  });

  it("rejects other URLs", () => {
    for (const raw of ["skynet://link/callback?code=c&state=s", "skynet://open/extra", "skynet://opener", "https://open", "other://open", "not a url", ""]) {
      expect(isOpenAppUrl(raw)).toBe(false);
    }
  });
});
