# Sign in to Skynet staging or a PR preview with a test account.
# Usage (bash or zsh): export the credentials as <PREFIX>_EMAIL and
# <PREFIX>_PASSWORD, set the variables below, then source the script:
#   export PREFIX=SKYNET_POOL APP=https://staging.skynetml.com API=https://api-staging.skynetml.com
#   . ./login.sh
# Sets TOKEN and nothing else. Prints only the HTTP status of the check.
if [ -z "$APP" ] || [ -z "$API" ] || [ -z "$PREFIX" ]; then
  echo 'login: set APP, API and PREFIX first, then source this script' >&2
  # exit runs only when the script is executed instead of sourced.
  # shellcheck disable=SC2317
  return 1 2>/dev/null || exit 1
fi
_DL_PREFIX="$PREFIX"
_DL_JAR=$(mktemp); _DL_DOH=(--doh-url https://cloudflare-dns.com/dns-query)
_DL_CSRF=$(curl -s "${_DL_DOH[@]}" -c "$_DL_JAR" -b "$_DL_JAR" "$APP/api/auth/csrf" | python3 -c 'import sys,json;print(json.load(sys.stdin)["csrfToken"])')
# The password goes to curl on stdin, so it does not show in the process list.
# The email and the CSRF token go through file descriptors, for the same reason.
printenv "${_DL_PREFIX}_PASSWORD" | tr -d '\n' | curl -s "${_DL_DOH[@]}" -o /dev/null -c "$_DL_JAR" -b "$_DL_JAR" -H 'X-Auth-Return-Redirect: 1' \
  --data-urlencode email@<(printenv "${_DL_PREFIX}_EMAIL" | tr -d '\n') --data-urlencode "password@-" \
  --data-urlencode csrfToken@<(printf %s "$_DL_CSRF") --data-urlencode "callbackUrl=$APP" "$APP/api/auth/callback/credentials"
TOKEN=$(curl -s "${_DL_DOH[@]}" -c "$_DL_JAR" -b "$_DL_JAR" "$APP/api/auth/session" | python3 -c 'import sys,json;print((json.load(sys.stdin) or {}).get("backendAccessToken",""))')
rm -f "$_DL_JAR"
# The bearer header goes to curl on stdin, so the token does not show in the process list.
printf 'authorization: Bearer %s\n' "$TOKEN" | curl -s "${_DL_DOH[@]}" -o /dev/null -w 'login check: HTTP %{http_code}\n' -H @- "$API/account/experience"
unset _DL_PREFIX _DL_JAR _DL_DOH _DL_CSRF
