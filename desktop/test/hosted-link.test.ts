import crypto from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HostedLinkClient, LOCAL_USER_EMAIL, mintSessionToken } from "../src/main/hosted-link";

const decode = (part: string) => JSON.parse(Buffer.from(part, "base64url").toString("utf8"));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("mintSessionToken", () => {
  it("signs the HS256 JWT the backend verifies", () => {
    const token = mintSessionToken("s3cret", LOCAL_USER_EMAIL, 1_700_000_000_000);
    const [header, payload, signature] = token.split(".");
    expect(decode(header)).toEqual({ alg: "HS256", typ: "JWT" });
    expect(decode(payload)).toMatchObject({
      email: LOCAL_USER_EMAIL,
      aud: "skynet-backend",
      iss: "skynet-frontend",
      iat: 1_700_000_000,
      exp: 1_700_000_300,
    });
    const expected = crypto.createHmac("sha256", "s3cret").update(`${header}.${payload}`).digest("base64url");
    expect(signature).toBe(expected);
  });
});

describe("HostedLinkClient.finish", () => {
  it("reports a timeout as unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new DOMException("The operation was aborted due to timeout", "TimeoutError"); }));
    expect(await new HostedLinkClient("http://127.0.0.1:1", "s").finish("c", "s")).toMatchObject({ ok: false, reason: "unreachable" });
  });

  it("reports a 503 from the local API as unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "x" }), { status: 503 })));
    expect(await new HostedLinkClient("http://127.0.0.1:1", "s").finish("c", "s")).toMatchObject({ ok: false, reason: "unreachable" });
  });
});

describe("HostedLinkClient.start", () => {
  const respond = (status: number, body: unknown) =>
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));

  it("returns the hosted consent URL", async () => {
    respond(200, { authorize_url: "https://skynetml.com/desktop/link?state=x" });
    expect(await new HostedLinkClient("http://127.0.0.1:1", "s").start()).toEqual({
      ok: true,
      url: "https://skynetml.com/desktop/link?state=x",
    });
  });

  it("reports a build without hosted sign-in as unavailable", async () => {
    respond(503, { code: "hosted_link.not_configured", detail: "not configured" });
    expect(await new HostedLinkClient("http://127.0.0.1:1", "s").start()).toMatchObject({ ok: false, reason: "unavailable" });
  });

  it("refuses a non-https consent URL", async () => {
    respond(200, { authorize_url: "file:///etc/passwd" });
    expect(await new HostedLinkClient("http://127.0.0.1:1", "s").start()).toMatchObject({ ok: false, reason: "failed" });
  });
});

describe("HostedLinkClient start and finish requests", () => {
  it("asks for the skynet:// callback and posts the code and state to finish", async () => {
    const fetchMock = vi.fn(async (..._args: unknown[]) => new Response(JSON.stringify({ authorize_url: "https://skynetml.com/x", linked: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new HostedLinkClient("http://127.0.0.1:1", "s");

    await client.start();
    expect(await client.finish("the-code", "the-state")).toEqual({ ok: true });

    const [startUrl, startInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(startUrl).toBe("http://127.0.0.1:1/local/hosted-link/start");
    expect(JSON.parse(startInit.body as string)).toEqual({ app_callback: true });
    const [finishUrl, finishInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(finishUrl).toBe("http://127.0.0.1:1/local/hosted-link/finish");
    expect(finishInit.method).toBe("POST");
    expect((finishInit.headers as Record<string, string>)["content-type"]).toBe("application/json");
    expect(JSON.parse(finishInit.body as string)).toEqual({ code: "the-code", state: "the-state" });
  });

  it("reports a refused finish with the backend detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Start the link again." }), { status: 400 })));
    expect(await new HostedLinkClient("http://127.0.0.1:1", "s").finish("c", "s")).toEqual({ ok: false, reason: "failed", detail: "Start the link again." });
  });
});
