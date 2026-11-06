import crypto from "node:crypto";

/** Matches backend settings.local_user_email; the backend signs in this one local user. */
export const LOCAL_USER_EMAIL = "local@skynet.local";

const b64url = (data: string | Buffer) => Buffer.from(data).toString("base64url");

/**
 * Mint the short-lived HS256 session JWT the local backend accepts, the same
 * shape the web app's frontend mints. The secret never leaves this process.
 */
export function mintSessionToken(secret: string, email: string, now = Date.now()): string {
  const iat = Math.floor(now / 1000);
  const header = b64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const payload = b64url(
    JSON.stringify({ sub: email, email, name: email, aud: "skynet-backend", iss: "skynet-frontend", iat, exp: iat + 300 }),
  );
  const signature = crypto.createHmac("sha256", secret).update(`${header}.${payload}`).digest();
  return `${header}.${payload}.${b64url(signature)}`;
}

export type StartResult = { ok: true; url: string } | { ok: false; reason: "unavailable" | "failed"; detail: string };
export type FinishResult = { ok: true } | { ok: false; reason: "unreachable" | "failed"; detail: string };

/** Talks to the local backend's /local/hosted-link routes as the local user. */
export class HostedLinkClient {
  constructor(
    readonly apiOrigin: string,
    private readonly secret: string,
  ) {}

  private async call(method: "GET" | "POST", route: string, body?: unknown): Promise<Response> {
    const headers: Record<string, string> = { authorization: `Bearer ${mintSessionToken(this.secret, LOCAL_USER_EMAIL)}` };
    if (body !== undefined) headers["content-type"] = "application/json";
    return fetch(`${this.apiOrigin}${route}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      // The finish call waits for the hosted code exchange (15 s timeout in the backend).
      signal: AbortSignal.timeout(20_000),
    });
  }

  /** Create the local user row; the link routes need it to exist. */
  async ensureLocalUser(): Promise<void> {
    const res = await fetch(`${this.apiOrigin}/auth/local/session`, {
      method: "POST",
      headers: { "X-Internal-Auth": this.secret },
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) throw new Error(`Local sign-in failed (HTTP ${res.status})`);
  }

  async linked(): Promise<boolean> {
    const res = await this.call("GET", "/local/hosted-link/status");
    if (!res.ok) throw new Error(`Account status check failed (HTTP ${res.status})`);
    const body = (await res.json()) as { linked?: boolean };
    return body.linked === true;
  }

  /**
   * Whether the Mac is linked, checked with hosted. The local link row can
   * outlive a link that hosted revoked or let expire; hosted whoami then
   * answers 401 (the local API deletes the link) or 409 (no link). Any other
   * failure means hosted did not answer, so the stored link stands.
   */
  async verified(): Promise<boolean> {
    if (!(await this.linked())) return false;
    try {
      const res = await this.call("GET", "/local/hosted-link/whoami");
      return res.status !== 401 && res.status !== 409;
    } catch {
      return true;
    }
  }

  async start(): Promise<StartResult> {
    // The consent page opens in the default browser and hands the result back through skynet://.
    const res = await this.call("POST", "/local/hosted-link/start", { app_callback: true });
    const body = (await res.json().catch(() => ({}))) as { authorize_url?: string; code?: string; detail?: string };
    if (res.ok && body.authorize_url?.startsWith("https://")) return { ok: true, url: body.authorize_url };
    if (res.status === 503 || body.code === "hosted_link.not_configured") {
      return { ok: false, reason: "unavailable", detail: body.detail ?? "" };
    }
    return { ok: false, reason: "failed", detail: body.detail ?? `HTTP ${res.status}` };
  }

  /** Trade the code and state from the skynet:// URL for the link (the local API checks the state). */
  async finish(code: string, state: string): Promise<FinishResult> {
    let res: Response;
    try {
      res = await this.call("POST", "/local/hosted-link/finish", { code, state });
    } catch (err) {
      // A DNS lookup that never answers holds the local API past this timeout.
      if (err instanceof Error && err.name === "TimeoutError") return { ok: false, reason: "unreachable", detail: err.message };
      throw err;
    }
    if (res.ok) return { ok: true };
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    return { ok: false, reason: res.status === 503 ? "unreachable" : "failed", detail: body.detail ?? `HTTP ${res.status}` };
  }
}

/**
 * Check the link every `everyMs` and call `onLost` once when it is gone. A
 * failed check is skipped: the crash watcher handles a stopped API.
 * Returns the function that stops the checks.
 */
export function watchLink(isLinked: () => Promise<boolean>, onLost: () => void, everyMs = 30_000): () => void {
  let checking = false;
  const timer = setInterval(async () => {
    if (checking) return;
    checking = true;
    try {
      if (!(await isLinked())) {
        clearInterval(timer);
        onLost();
      }
    } catch {
      // Try again on the next tick.
    } finally {
      checking = false;
    }
  }, everyMs);
  return () => clearInterval(timer);
}
