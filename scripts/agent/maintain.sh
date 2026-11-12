#!/usr/bin/env bash
# Deterministic health report for the codebase. Safe to run again.
#
# Runs from `just maintain` and from the Claude Code Setup hook
# (`maintenance` matcher, fired by `claude -p --maintenance`). The /maintain
# prompt reads .claude/logs/maintain-latest.log and proposes fixes as a PR.
#
# The script only reports. Its one change is `git worktree prune`, which
# removes the records of worktrees whose folder no longer exists, and the
# removal of its own logs older than 30 days.

# shellcheck source=scripts/agent/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
start_log maintain
cd "$ROOT" || exit 1

# --- Security advisories -----------------------------------------------------
echo "--> npm audit (frontend)"
npm_json="$(cd frontend && npm audit --json 2>/dev/null)"
npm_counts="$(printf '%s' "$npm_json" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)["metadata"]["vulnerabilities"]
except Exception:
    print("unknown"); sys.exit()
total = v.get("total", 0)
print(" ".join(f"{k}={v[k]}" for k in ("critical", "high", "moderate", "low")), f"total={total}")
')"
case "$npm_counts" in
  unknown) record WARN "npm audit" "could not run (no network or no node_modules)" ;;
  *total=0) record OK "npm audit" "$npm_counts" ;;
  *) record WARN "npm audit" "$npm_counts — run: cd frontend && npm audit"
     printf '%s' "$npm_json" | python3 -c '
import json, sys
rank = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "info": 4}
items = sorted(json.load(sys.stdin).get("vulnerabilities", {}).items(), key=lambda kv: (rank.get(kv[1]["severity"], 9), kv[0]))
for name, v in items[:15]:
    sev = v["severity"]
    fix = "yes" if v.get("fixAvailable") else "no"
    print(f"    {sev:9} {name} (fix: {fix})")
if len(items) > 15:
    print(f"    (+{len(items) - 15} more)")
' ;;
esac

echo "--> pip-audit (backend, from uv.lock)"
reqs="$(mktemp)"
pip_out=""
pip_failed=0
pip_gaps=""
adv_re='^[A-Za-z0-9_.-]+ +[0-9][^ ]* +(GHSA|PYSEC|CVE)-'
# pyproject.toml declares the `prime` and `e2b` extras as a uv conflict, so
# one `--all-extras` export fails. Audit every other extra, then `prime`.
for extras in "--all-extras --no-extra prime" "--extra prime"; do
  out=""
  # shellcheck disable=SC2086  # $extras holds several flags on purpose.
  if ! (cd backend && uv export --frozen $extras --no-hashes --no-emit-project -q >"$reqs"); then
    pip_failed=1
    pip_gaps+="${pip_gaps:+; }could not export $extras"
  elif ! out="$(uvx --quiet pip-audit -r "$reqs" --no-deps --disable-pip --progress-spinner off 2>&1)"; then
    pip_failed=1
    pip_out+="$out"$'\n'
    # pip-audit also exits non-zero when it finds advisories; only a run
    # without advisory rows is a failure to audit.
    if ! printf '%s' "$out" | grep -qE "$adv_re"; then
      last="$(printf '%s' "$out" | grep . | tail -1)"
      pip_gaps+="${pip_gaps:+; }could not audit $extras (${last:-no output})"
    fi
  fi
done
rm -f "$reqs"
if [ "$pip_failed" -eq 0 ]; then
  record OK "pip-audit" "no known vulnerabilities"
else
  vulns="$(printf '%s' "$pip_out" | grep -E "$adv_re" | sort -u | grep -c . || true)"
  if [ "${vulns:-0}" -gt 0 ]; then
    record WARN "pip-audit" "$vulns advisories${pip_gaps:+; $pip_gaps} — see list below"
    printf '%s' "$pip_out" | grep -E '(GHSA|PYSEC|CVE)-' | sort -u | sed 's/^/    /'
  else
    record WARN "pip-audit" "$pip_gaps"
  fi
fi

echo "--> Dependabot alerts (GitHub)"
repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
if [ -n "$repo" ] && alerts="$(gh api "repos/$repo/dependabot/alerts?state=open&per_page=100" \
     --jq '.[] | "\(.security_advisory.severity) \(.dependency.package.ecosystem)/\(.dependency.package.name) \(.dependency.manifest_path)"' 2>/dev/null)"; then
  count="$(printf '%s' "$alerts" | grep -c . || true)"
  if [ "$count" -eq 0 ]; then
    record OK dependabot "0 open alerts on $repo"
  else
    record WARN dependabot "$count open alerts on $repo"
    printf '%s\n' "$alerts" | sort | uniq -c | sort -rn | sed 's/^/    /'
  fi
else
  record WARN dependabot "could not read alerts (gh not logged in, or no access)"
fi

# --- Outdated packages -------------------------------------------------------
echo "--> npm outdated (frontend, direct deps)"
if [ ! -d frontend/node_modules ]; then
  record WARN "npm outdated" "no frontend/node_modules — run just setup first"
else
  npm_major="$(cd frontend && npm outdated --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
major = lambda v: str(v).split(".")[0]
behind = sorted(n for n, v in d.items() if v.get("current") and major(v["current"]) != major(v.get("latest")))
print(f"{len(d)} outdated, {len(behind)} behind a major version")
for n in behind:
    cur, last = d[n]["current"], d[n]["latest"]
    print(f"    {n}: {cur} -> latest {last}")
')"
  record OK "npm outdated" "$(printf '%s\n' "$npm_major" | head -1)"
  printf '%s\n' "$npm_major" | tail -n +2
fi

echo "--> uv outdated (backend, direct deps)"
if uv_out="$(cd backend && uv tree --frozen --outdated --depth 1 --all-groups 2>/dev/null | grep 'latest:')"; then
  record WARN "uv outdated" "$(printf '%s\n' "$uv_out" | grep -c .) direct deps behind latest"
  printf '%s\n' "$uv_out" | sed 's/^[^A-Za-z0-9]*/    /'
else
  record OK "uv outdated" "all direct deps at latest (or uv could not check)"
fi

# --- Database migrations -----------------------------------------------------
echo "--> alembic heads"
heads="$(cd backend && uv run --frozen alembic heads 2>/dev/null | grep -c '(head)' || true)"
case "$heads" in
  1) record OK "alembic heads" "one head" ;;
  0) record WARN "alembic heads" "could not read heads" ;;
  *) record FAIL "alembic heads" "$heads heads — add a merge migration" ;;
esac

# --- Git worktrees -----------------------------------------------------------
echo "--> git worktrees"
pruned="$(git worktree prune -v 2>&1)"
record OK "worktree prune" "$( [ -n "$pruned" ] && printf '%s' "$pruned" | grep -c . || echo 0) stale records removed"
git fetch -q origin staging 2>/dev/null
stale=0
while read -r path branch; do
  [ "$path" = "$ROOT" ] && continue
  case "$branch" in ""|main|staging) continue ;; esac
  if git merge-base --is-ancestor "$branch" origin/staging 2>/dev/null; then
    stale=$((stale + 1))
    echo "    merged into staging: $path ($branch)"
  fi
done < <(git worktree list --porcelain | awk '/^worktree /{p=$2} /^branch /{sub("refs/heads/","",$2); print p, $2}')
if [ "$stale" -gt 0 ]; then
  record WARN "merged worktrees" "$stale worktrees hold merged branches — remove with: git worktree remove <path>"
else
  record OK "merged worktrees" "none"
fi

# --- Docker ------------------------------------------------------------------
if have docker && docker info >/dev/null 2>&1; then
  reclaim="$(docker system df --format '{{.Type}}: {{.Reclaimable}}' 2>/dev/null | tr '\n' ';')"
  record OK "docker" "reclaimable: $reclaim (prune by hand: docker system prune)"
else
  record SKIP "docker" "docker not running"
fi

# --- Leftover files ----------------------------------------------------------
echo "--> untracked files outside .gitignore"
untracked="$(git ls-files --others --exclude-standard | awk -F/ '{print ($2=="" ? $1 : $1"/"$2"/")}' | sort -u)"
if [ -n "$untracked" ]; then
  record WARN untracked "$(printf '%s\n' "$untracked" | grep -c .) paths — add to .gitignore or delete"
  printf '%s\n' "$untracked" | head -20 | sed 's/^/    /'
else
  record OK untracked "none"
fi
old_logs="$(find "$LOG_DIR" -name '*.log' -type f -mtime +30 2>/dev/null)"
if [ -n "$old_logs" ]; then
  printf '%s\n' "$old_logs" | xargs rm -f
  record OK "old logs" "$(printf '%s\n' "$old_logs" | grep -c .) logs older than 30 days removed"
fi

finish
