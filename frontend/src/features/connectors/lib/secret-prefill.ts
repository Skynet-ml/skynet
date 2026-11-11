/** The backend's REPO_SECRET_NAME_PATTERN: an environment variable name. */
export const SECRET_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

/**
 * Read the secret name that a remote MCP `request_secret` link fills in
 * (`?settings=connectors&secret=NAME`, SKY-641). A link carries a name only;
 * anything that is not a valid name is ignored.
 */
export function secretNameFromSearch(search: string): string | null {
  const name = new URLSearchParams(search).get("secret");
  return name && SECRET_NAME.test(name) ? name : null;
}

/** Drop the `secret` parameter from a URL, so a reload does not fill the name in again. */
export function withoutSecretParam(href: string): string {
  const url = new URL(href);
  url.searchParams.delete("secret");
  return `${url.pathname}${url.search}${url.hash}`;
}
