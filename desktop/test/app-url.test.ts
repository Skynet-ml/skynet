import { describe, expect, it } from "vitest";
import { appUrlFromArgv, appWindowUrl, hostedAppOrigin, LINK_CALLBACK, parseLinkCallback, webDocsUrl } from "../src/main/app-url";

describe("parseLinkCallback", () => {
  it("reads the code and the state of an approved link", () => {
    const state = "gAAAAABn_state+with/odd=chars==";
    const url = `${LINK_CALLBACK}?${new URLSearchParams({ code: "abc_-1", state })}`;
    expect(parseLinkCallback(url)).toEqual({ kind: "code", code: "abc_-1", state });
  });

  it("reads a declined link", () => {
    expect(parseLinkCallback("skynet://link/callback?error=access_denied&state=s")).toEqual({ kind: "denied", state: "s" });
  });

  it("reports other errors and a missing code or state", () => {
    expect(parseLinkCallback("skynet://link/callback?error=server_error")).toEqual({ kind: "error", error: "server_error", state: null });
    expect(parseLinkCallback("skynet://link/callback?code=c")).toEqual({ kind: "error", error: "invalid_request", state: null });
    expect(parseLinkCallback("skynet://link/callback?state=s")).toEqual({ kind: "error", error: "invalid_request", state: "s" });
  });

  it("ignores every other URL", () => {
    for (const url of [
      "skynet://link/callback/?code=c&state=s",
      "skynet://link/other?code=c&state=s",
      "skynet://evil/callback?code=c&state=s",
      "skynet://evil@link/callback?code=c&state=s",
      "skynet://link:1/callback?code=c&state=s",
      "skynet:link/callback?code=c&state=s",
      "https://link/callback?code=c&state=s",
      "not a url",
      "",
    ]) {
      expect(parseLinkCallback(url), url).toBeNull();
    }
  });
});

describe("appUrlFromArgv", () => {
  it("finds the skynet:// URL a second instance received", () => {
    expect(appUrlFromArgv(["/Applications/Skynet.app/Contents/MacOS/Skynet", "skynet://link/callback?code=c"])).toBe(
      "skynet://link/callback?code=c",
    );
    expect(appUrlFromArgv(["/Applications/Skynet.app/Contents/MacOS/Skynet", "--flag"])).toBeNull();
  });
});

describe("appWindowUrl", () => {
  const app = "http://127.0.0.1:48120";

  it("keeps a URL that is already on the app's origin", () => {
    expect(appWindowUrl("http://127.0.0.1:48120/login?x=1", app)).toBe("http://127.0.0.1:48120/login?x=1");
  });

  it("moves a localhost URL on the app's port to the app's origin", () => {
    expect(appWindowUrl("http://localhost:48120/login", app)).toBe("http://127.0.0.1:48120/login");
    expect(appWindowUrl("http://localhost:48120/runs?a=b#c", app)).toBe("http://127.0.0.1:48120/runs?a=b#c");
  });

  it("refuses other ports, hosts, schemes and bad URLs", () => {
    expect(appWindowUrl("http://localhost:48100/health", app)).toBeNull();
    expect(appWindowUrl("http://example.com:48120/", app)).toBeNull();
    expect(appWindowUrl("https://127.0.0.1:48120/", app)).toBeNull();
    expect(appWindowUrl("not a url", app)).toBeNull();
  });
});

describe("hostedAppOrigin", () => {
  it("links a staging stamp to staging and everything else to production", () => {
    expect(hostedAppOrigin("staging\n")).toBe("https://staging.skynetml.com");
    expect(hostedAppOrigin(" Staging ")).toBe("https://staging.skynetml.com");
    expect(hostedAppOrigin("production")).toBe("https://skynetml.com");
    expect(hostedAppOrigin(null)).toBe("https://skynetml.com");
    expect(hostedAppOrigin("")).toBe("https://skynetml.com");
  });
});

describe("webDocsUrl", () => {
  const app = "http://127.0.0.1:48120";
  const prod = "https://skynetml.com";

  it("sends the app's docs pages to the web docs", () => {
    expect(webDocsUrl("http://127.0.0.1:48120/docs", app, prod)).toBe("https://skynetml.com/docs");
    expect(webDocsUrl("http://127.0.0.1:48120/docs/he/mcp?x=1#setup", app, prod)).toBe("https://skynetml.com/docs/he/mcp?x=1#setup");
    expect(webDocsUrl("http://localhost:48120/docs/quickstart", app, prod)).toBe("https://skynetml.com/docs/quickstart");
    expect(webDocsUrl("http://127.0.0.1:48120/docs.md", app, prod)).toBe("https://skynetml.com/docs.md");
  });

  it("uses the build's docs origin", () => {
    expect(webDocsUrl("http://127.0.0.1:48120/docs/api", app, "https://staging.skynetml.com")).toBe("https://staging.skynetml.com/docs/api");
  });

  it("leaves other app pages and other origins alone", () => {
    expect(webDocsUrl("http://127.0.0.1:48120/", app, prod)).toBeNull();
    expect(webDocsUrl("http://127.0.0.1:48120/docs-search?q=a", app, prod)).toBeNull();
    expect(webDocsUrl("http://127.0.0.1:48120/runs/docs", app, prod)).toBeNull();
    expect(webDocsUrl("http://127.0.0.1:48100/docs", app, prod)).toBeNull();
    expect(webDocsUrl("https://skynetml.com/docs", app, prod)).toBeNull();
  });
});
