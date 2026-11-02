import NextAuth, { CredentialsSignin } from "next-auth";
import type { Provider } from "next-auth/providers";
import Credentials from "next-auth/providers/credentials";
import Google from "next-auth/providers/google";
import GitHub from "next-auth/providers/github";
import { createHmac, randomUUID } from "crypto";
import { cookies } from "next/headers";
import { ACCOUNT_LINK_COOKIE, LEGACY_ACCOUNT_LINK_COOKIE, decodeAccountLink } from "./account-link";
import { saveSessionPicture, savablePicture } from "./session-picture";

/**
 * Authentication configuration.
 *
 *   Social sign-in — Google (AUTH_GOOGLE_ID/SECRET) and GitHub
 *   (AUTH_GITHUB_ID/SECRET), each shown only when its credentials are present —
 *   plus email/password accounts ("create an account in Skynet"), verified by
 *   the backend /auth endpoints over the shared BACKEND_AUTH_SECRET.
 *
 * Identity is the email across every provider, so one person maps to one backend
 * identity however they sign in (see signBackendToken).
 *
 * Optional env vars:
 *   AUTH_ADMINS           — comma-separated admin emails/usernames
 *   BACKEND_AUTH_SECRET   — shared secret for backend bearer tokens and the
 *                           internal /auth/register|login calls
 *   API_URL               — backend base URL the credentials provider calls
 *   AUTH_COOKIE_PREFIX    — renames every Auth.js cookie (local dev only; see
 *                           authCookies)
 */

// A deploy can set AUTH_ADMINS="" explicitly, and `??` would never reach the
// fallback for it, so an empty var is treated as unset.
function envOrFallback(value: string | undefined, fallback: string) {
  return value && value.trim() ? value : fallback;
}

const ADMIN_LIST = new Set(
  envOrFallback(process.env.AUTH_ADMINS, "admin")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean),
);

const backendAuthSecret = process.env.BACKEND_AUTH_SECRET ?? process.env.AUTH_SECRET;
const backendTokenTtlSeconds = Number.parseInt(
  process.env.BACKEND_AUTH_TOKEN_TTL_SECONDS ?? "900",
  10,
);

// The credentials provider reaches the backend over this base URL; API_URL is
// the runtime-overridable server-side value, with the build-time
// NEXT_PUBLIC_API_URL as the fallback (mirrors shared/lib/api.ts).
const backendBaseUrl =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const googleConfigured = !!process.env.AUTH_GOOGLE_ID && !!process.env.AUTH_GOOGLE_SECRET;
const githubConfigured = !!process.env.AUTH_GITHUB_ID && !!process.env.AUTH_GITHUB_SECRET;

type BackendAccount = { email: string; name: string; role: string; first_login?: boolean };

type CredentialsOutcome =
  | { kind: "ok"; account: BackendAccount }
  | { kind: "2fa_required"; methods: string }
  | { kind: "2fa_invalid" }
  | { kind: "email_unverified" }
  | { kind: "failed" };

/**
 * Verify email/password (+ optional second-factor code) against the backend's
 * internal /auth/login. Distinguishes the two 2FA outcomes — code missing
 * (with the account's usable methods) vs code wrong — plus an unverified email,
 * so the login form can pivot to its code or verify step; every other failure
 * collapses into a generic "failed" that never leaks which case occurred.
 */
async function verifyBackendCredentials(
  email: string,
  password: string,
  codes: { totpCode: string; emailCode: string; recoveryCode: string },
): Promise<CredentialsOutcome> {
  if (!backendAuthSecret) return { kind: "failed" };
  try {
    const res = await fetch(`${backendBaseUrl}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Auth": backendAuthSecret },
      body: JSON.stringify({
        email,
        password,
        totp_code: codes.totpCode,
        email_code: codes.emailCode,
        recovery_code: codes.recoveryCode,
      }),
    });
    if (res.ok) return { kind: "ok", account: (await res.json()) as BackendAccount };
    const body = (await res.json().catch(() => ({}))) as {
      code?: unknown;
      params?: { methods?: unknown };
    };
    if (body.code === "accounts.two_factor_required") {
      return { kind: "2fa_required", methods: String(body.params?.methods ?? "totp") };
    }
    if (body.code === "accounts.invalid_second_factor") return { kind: "2fa_invalid" };
    if (body.code === "accounts.email_not_verified") return { kind: "email_unverified" };
    return { kind: "failed" };
  } catch {
    return { kind: "failed" };
  }
}

/**
 * Verify a WebAuthn assertion against the backend's internal /auth/webauthn/verify.
 * The backend checks the signature against the stored credential and resolves
 * the owning identity; null on any failure.
 */
async function verifyBackendPasskey(assertion: string): Promise<BackendAccount | null> {
  if (!backendAuthSecret) return null;
  let credential: unknown;
  try {
    credential = JSON.parse(assertion);
  } catch {
    return null;
  }
  try {
    const res = await fetch(`${backendBaseUrl}/auth/webauthn/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Auth": backendAuthSecret },
      body: JSON.stringify({ credential }),
    });
    if (!res.ok) return null;
    return (await res.json()) as BackendAccount;
  } catch {
    return null;
  }
}

/** The local desktop build's single account, from the backend's /auth/local/session. */
async function resolveLocalAccount(): Promise<BackendAccount | null> {
  if (!backendAuthSecret) return null;
  try {
    const res = await fetch(`${backendBaseUrl}/auth/local/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Auth": backendAuthSecret },
      body: "{}",
    });
    if (!res.ok) return null;
    return (await res.json()) as BackendAccount;
  } catch {
    return null;
  }
}

/**
 * Mirror a provider-authenticated (Google/GitHub) identity into the
 * backend's users table via the internal /auth/oauth/provision, so it gets the
 * same first-sign-in signal as a local account. The provider's other verified
 * emails ride along so an account registered under one of them is reused
 * instead of forking a second identity, and the provider's stable account id
 * lets an account linked from Settings win over any email. Resolves to the
 * account the backend chose, or null on any failure, which never blocks the
 * sign-in itself — the provider has already authenticated the user.
 */
async function provisionBackendAccount(
  user: {
    email?: string | null;
    name?: string | null;
    image?: string | null;
    otherEmails?: string[];
  },
  provider: string,
  providerAccountId: string,
): Promise<BackendAccount | null> {
  if (!backendAuthSecret || !user.email) return null;
  const image = savablePicture(user.image) ?? "";
  try {
    const res = await fetch(`${backendBaseUrl}/auth/oauth/provision`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Auth": backendAuthSecret },
      body: JSON.stringify({
        email: user.email,
        name: user.name ?? "",
        other_emails: user.otherEmails ?? [],
        provider,
        provider_account_id: providerAccountId,
        image,
      }),
    });
    if (!res.ok) return null;
    return (await res.json()) as BackendAccount;
  } catch {
    return null;
  }
}

/** Save the session picture on the backend account; true when the backend took the call. */
async function saveBackendPicture(email: string, image: string): Promise<boolean> {
  if (!backendAuthSecret) return false;
  try {
    const res = await fetch(`${backendBaseUrl}/auth/oauth/picture`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Auth": backendAuthSecret },
      body: JSON.stringify({ email, image }),
      // The jwt callback awaits this on gated requests; a hung backend must not hold the page.
      signal: AbortSignal.timeout(3000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Link a provider account to the signed-in ``username`` through the backend's
 * internal /auth/oauth/link. Resolves to null on success, else the i18n code
 * to show back in Settings.
 */
async function linkBackendIdentity(
  username: string,
  provider: string,
  providerAccountId: string,
  providerEmail: string,
): Promise<string | null> {
  if (!backendAuthSecret) return "accounts.link_failed";
  try {
    const res = await fetch(`${backendBaseUrl}/auth/oauth/link`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Auth": backendAuthSecret },
      body: JSON.stringify({
        username,
        provider,
        provider_account_id: providerAccountId,
        provider_email: providerEmail,
      }),
    });
    if (res.ok) return null;
    const body = (await res.json().catch(() => ({}))) as { code?: unknown };
    return typeof body.code === "string" ? body.code : "accounts.link_failed";
  } catch {
    return "accounts.link_failed";
  }
}

/**
 * Consume the Settings link cookie for ``provider``, returning the signed-in
 * account it names, or null when this sign-in is not a link.
 */
async function pendingAccountLink(provider: string): Promise<string | null> {
  const jar = await cookies();
  const value = jar.get(ACCOUNT_LINK_COOKIE)?.value ?? jar.get(LEGACY_ACCOUNT_LINK_COOKIE)?.value;
  if (!value) return null;
  jar.delete(ACCOUNT_LINK_COOKIE);
  jar.delete(LEGACY_ACCOUNT_LINK_COOKIE);
  return decodeAccountLink(value, provider);
}

type GitHubEmail = { email: string; primary: boolean; verified: boolean };

/**
 * List the verified emails on a GitHub account, primary first. GitHub lets a
 * user add any address without confirming it, so only verified ones may name
 * a Skynet identity.
 */
async function githubVerifiedEmails(accessToken: string | undefined): Promise<string[]> {
  if (!accessToken) return [];
  try {
    const res = await fetch("https://api.github.com/user/emails", {
      headers: { Authorization: `Bearer ${accessToken}`, "User-Agent": "app" },
    });
    if (!res.ok) return [];
    const emails = (await res.json()) as GitHubEmail[];
    return emails
      .filter((e) => e.verified)
      .sort((a, b) => Number(b.primary) - Number(a.primary))
      .map((e) => e.email.toLowerCase());
  } catch {
    return [];
  }
}

/** Build a CredentialsSignin whose ``code`` survives to the client signIn result. */
function credentialsError(code: string): CredentialsSignin {
  const error = new CredentialsSignin();
  error.code = code;
  return error;
}

const providers: Provider[] = [];

function normalizeStringList(value: unknown): string[] {
  if (Array.isArray(value))
    return value
      .map(String)
      .map((s) => s.trim())
      .filter(Boolean);
  if (typeof value === "string") {
    return value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return [];
}

function isAdmin(identifier: string) {
  return ADMIN_LIST.has(identifier.toLowerCase());
}

function base64url(value: Buffer | string) {
  return Buffer.from(value).toString("base64url");
}

function signBackendToken(token: {
  name?: unknown;
  email?: unknown;
  role?: unknown;
  groups?: unknown;
}) {
  if (!backendAuthSecret) return undefined;
  const displayName = typeof token.name === "string" ? token.name : undefined;
  const email = typeof token.email === "string" ? token.email : undefined;
  // Identity across the backend (job / dataset / share ownership) is the email,
  // never the display name: two OAuth users can share a name but never an email.
  // The backend reads its identity from the `name` claim first, so the stable
  // subject is sent there; the human-facing display name rides the session JWT.
  const subject = email || displayName;
  if (!subject) return undefined;
  const now = Math.floor(Date.now() / 1000);
  const groups = normalizeStringList(token.groups);
  const header = { alg: "HS256", typ: "JWT" };
  const payload = {
    // Still the old names: the backend accepts both these and the neutral ones,
    // and the frontend and backend deploy separately, so switching what is
    // emitted waits until every backend in service accepts the new values.
    aud: "skynet-backend",
    iss: "skynet-frontend",
    sub: subject,
    name: subject,
    email,
    role: typeof token.role === "string" ? token.role : "user",
    groups,
    iat: now,
    exp: now + Math.max(60, backendTokenTtlSeconds || 900),
    jti: randomUUID(),
  };
  const encodedHeader = base64url(JSON.stringify(header));
  const encodedPayload = base64url(JSON.stringify(payload));
  const signature = createHmac("sha256", backendAuthSecret)
    .update(`${encodedHeader}.${encodedPayload}`)
    .digest("base64url");
  return `${encodedHeader}.${encodedPayload}.${signature}`;
}

if (googleConfigured) {
  providers.push(
    Google({
      clientId: process.env.AUTH_GOOGLE_ID,
      clientSecret: process.env.AUTH_GOOGLE_SECRET,
      // One person, one identity: if they later sign in with another provider
      // asserting the same verified email, link to the existing account
      // instead of forking a second one.
      allowDangerousEmailAccountLinking: true,
    }),
  );
}
if (githubConfigured) {
  providers.push(
    GitHub({
      clientId: process.env.AUTH_GITHUB_ID,
      clientSecret: process.env.AUTH_GITHUB_SECRET,
      allowDangerousEmailAccountLinking: true,
    }),
  );
}
providers.push(
  Credentials({
    id: "credentials",
    name: "Email and password",
    credentials: {
      email: { label: "Email", type: "email" },
      password: { label: "Password", type: "password" },
      totpCode: { label: "Authenticator code", type: "text" },
      emailCode: { label: "Email code", type: "text" },
      recoveryCode: { label: "Recovery code", type: "text" },
    },
    async authorize(credentials) {
      const email = (credentials?.email as string)?.trim().toLowerCase();
      const password = credentials?.password as string;
      if (!email || !password) return null;
      const str = (v: unknown): string => (typeof v === "string" ? v : "");
      const result = await verifyBackendCredentials(email, password, {
        totpCode: str(credentials?.totpCode),
        emailCode: str(credentials?.emailCode),
        recoveryCode: str(credentials?.recoveryCode),
      });
      // The 2FA outcomes ride the CredentialsSignin ``code`` so the login
      // form can pivot to (or stay on) its verification-code step; the
      // methods list travels inside the code string.
      if (result.kind === "2fa_required") {
        throw credentialsError(`2fa_required:${result.methods}`);
      }
      if (result.kind === "2fa_invalid") throw credentialsError("2fa_invalid");
      // Surfaced so the login form can pivot to its email-confirmation step
      // instead of showing a dead-end "sign-in failed".
      if (result.kind === "email_unverified") throw credentialsError("email_unverified");
      if (result.kind !== "ok") return null;
      return {
        id: result.account.email,
        name: result.account.name,
        email: result.account.email,
        groups: [],
        role: result.account.role,
        firstLogin: result.account.first_login === true,
      };
    },
  }),
);
providers.push(
  Credentials({
    id: "passkey",
    name: "Passkey",
    credentials: {
      assertion: { label: "Assertion", type: "text" },
    },
    async authorize(credentials) {
      const assertion = credentials?.assertion;
      if (typeof assertion !== "string" || !assertion) return null;
      const account = await verifyBackendPasskey(assertion);
      if (!account) return null;
      return {
        id: account.email,
        name: account.name,
        email: account.email,
        groups: [],
        role: account.role,
        firstLogin: account.first_login === true,
      };
    },
  }),
);

// The local desktop build has one user and no sign-in screen: the backend
// resolves its fixed local account over the internal secret. Hosted builds
// never set APP_LOCAL_MODE, so the provider does not exist there.
if (process.env.APP_LOCAL_MODE === "1") {
  providers.push(
    Credentials({
      id: "local",
      name: "Local",
      credentials: {},
      async authorize() {
        const account = await resolveLocalAccount();
        if (!account) return null;
        return {
          id: account.email,
          name: account.name,
          email: account.email,
          groups: [],
          role: account.role,
          firstLogin: account.first_login === true,
        };
      },
    }),
  );
}

// Browsers scope cookies by host, not port, so two checkouts on localhost:3000
// and :3001 overwrite each other's session cookie. With different AUTH_SECRETs
// each then fails to decrypt the other's ("no matching decryption secret") and
// signs the user out. A per-checkout prefix keeps their cookies apart.
function authCookies(prefix: string | undefined) {
  if (!prefix) return undefined;
  const named = (suffix: string) => ({ name: `${prefix}.${suffix}` });
  return {
    sessionToken: named("session-token"),
    callbackUrl: named("callback-url"),
    csrfToken: named("csrf-token"),
    pkceCodeVerifier: named("pkce.code_verifier"),
    state: named("state"),
    nonce: named("nonce"),
    webauthnChallenge: named("challenge"),
  };
}

export const { handlers, auth } = NextAuth({
  providers,
  session: { strategy: "jwt" },
  cookies: authCookies(process.env.AUTH_COOKIE_PREFIX),
  pages: { signIn: "/login" },
  callbacks: {
    authorized({ auth: session }) {
      return !!session?.user;
    },
    // Identity is the email, so a provider email nobody proved must never name
    // an account: it would let anyone who types a victim's address at the
    // provider sign in as them.
    async signIn({ user, account, profile }) {
      // Linking from Settings goes by the provider's account id, so the
      // provider's emails don't have to match or even be verified.
      if (account && (account.provider === "google" || account.provider === "github")) {
        const linkTo = await pendingAccountLink(account.provider);
        if (linkTo) {
          const error = await linkBackendIdentity(
            linkTo,
            account.provider,
            account.providerAccountId,
            user.email ?? "",
          );
          if (error) return `/?settings=security&link_error=${encodeURIComponent(error)}`;
          user.email = linkTo;
          user.otherEmails = [];
          return true;
        }
      }
      if (account?.provider === "google") return profile?.email_verified === true;
      if (account?.provider === "github") {
        const verified = await githubVerifiedEmails(account.access_token ?? undefined);
        if (!verified.length) return false;
        const asserted = user.email?.toLowerCase();
        user.email = asserted && verified.includes(asserted) ? asserted : verified[0];
        user.otherEmails = verified.filter((e) => e !== user.email);
      }
      return true;
    },
    async jwt({ token, user, account }) {
      if (user) {
        token.name = user.name ?? token.name;
        token.email = user.email ?? token.email;
        token.groups = normalizeStringList(user.groups);
        // OAuth providers carry no role; derive admin from the email
        // allowlist so AUTH_ADMINS grants admin for Google/GitHub exactly as
        // it does for email/password accounts.
        const identity = String(user.email ?? user.name ?? "").toLowerCase();
        token.role = user.role ?? (isAdmin(identity) ? "admin" : "user");
        // The backend-verified providers (credentials, passkey) already report
        // whether this is the account's first sign-in. Every other provider
        // authenticates at the IdP, so its identity is mirrored to the backend
        // here to get the same signal.
        if (account && account.type !== "credentials") {
          const linked = await provisionBackendAccount(
            user,
            account.provider,
            account.providerAccountId,
          );
          // The backend may pick an account under another verified email.
          if (linked) {
            token.email = linked.email;
            token.name = linked.name || token.name;
            // Provision saved the picture already.
            const image = savablePicture(user.image);
            if (image) token.pictureSaved = image;
          }
          token.firstLogin = linked?.first_login === true;
        } else {
          token.firstLogin = user.firstLogin === true;
        }
      } else {
        // A session from before the backend saved pictures shows one that the
        // backend lacks, and the Mac app shows only the backend's picture.
        await saveSessionPicture(token, saveBackendPicture);
      }
      return token;
    },
    session({ session, token }) {
      if (typeof token.name === "string") session.user.name = token.name;
      if (typeof token.email === "string") session.user.email = token.email;
      session.user.role = token.role ?? "user";
      session.user.groups = normalizeStringList(token.groups);
      session.user.firstLogin = token.firstLogin === true;
      session.backendAccessToken = signBackendToken(token);
      return session;
    },
  },
  // Auth.js refuses every request when trustHost is false; it has no mode that
  // compares Host with AUTH_URL. Local mode refuses a foreign Host in the proxy
  // (shared/lib/proxy-route.ts), and AUTH_URL fixes the URLs Auth.js builds.
  trustHost: true,
  // Google and GitHub accept only registered callback URLs, and every PR
  // preview has its own host. Previews send their OAuth callback to staging
  // (preview.sh sets AUTH_REDIRECT_PROXY_URL), and staging forwards it to the
  // host sealed in the state. Previews copy staging's AUTH_SECRET, so staging
  // can open that state.
  redirectProxyUrl:
    process.env.AUTH_REDIRECT_PROXY_URL ??
    (process.env.APP_ENVIRONMENT === "staging" && process.env.AUTH_URL
      ? `${process.env.AUTH_URL}/api/auth`
      : undefined),
});
