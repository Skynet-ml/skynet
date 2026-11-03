import {
  NextResponse,
  type NextFetchEvent,
  type NextMiddleware,
  type NextRequest,
} from "next/server";
import { auth } from "@/shared/lib/auth";
import { proxyRoute } from "@/shared/lib/proxy-route";

// ``auth`` runs as the proxy itself when called with the request, as the old
// ``export default auth`` did; its typings declare only the wrapper forms.
const authProxy = auth as unknown as NextMiddleware;

export default function proxy(request: NextRequest, event: NextFetchEvent) {
  switch (proxyRoute(request.headers.get("host"), request.nextUrl.pathname)) {
    case "refuse":
      return new NextResponse("Forbidden", { status: 403 });
    case "pass":
      return passThrough(request);
    case "auth":
      return authProxy(request, event);
  }
}

// Must equal DOCS_PATH_HEADER in features/docs/lib/docs-answer.ts (a unit test
// checks this). The proxy does not
// import the docs feature, so its pages and content stay out of this bundle.
const DOCS_PATH_HEADER = "x-docs-path";

/**
 * Pass a request on, and give the root layout the path of a docs page.
 *
 * The docs page takes its language from the URL, but the root layout built
 * the shared UI catalog from the cookie. So /docs/he showed the docs agent's
 * shared strings and times in the cookie's language. A client copy of the
 * header is dropped here. On login-gated routes it can only pick that
 * client's own UI language, as the locale cookie can.
 */
function passThrough(request: NextRequest) {
  const headers = new Headers(request.headers);
  headers.delete(DOCS_PATH_HEADER);
  const { pathname } = request.nextUrl;
  if (pathname === "/docs" || pathname.startsWith("/docs/")) headers.set(DOCS_PATH_HEADER, pathname);
  return NextResponse.next({ request: { headers } });
}

export const config = {
  // Build output only. Every other route runs the proxy for the local-mode
  // Host check; AUTH_GATED_PATTERN (shared/lib/proxy-route.ts) decides which
  // of them need a login.
  matcher: ["/((?!_next/static|_next/image).*)"],
};
