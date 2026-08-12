#!/usr/bin/env bash
# Fail loudly if any real credential value has landed in the repo (working tree OR git
# history). This exists because the exfiltration probe in this repo deliberately handles
# live credentials, and a committed artifact is public forever.
#
# Usage:  ./scripts/verify_no_secrets.sh
# Exit 0 = clean, exit 1 = something real is in the repo.

set -uo pipefail
cd "$(dirname "$0")/.."

FAIL=0

# 1. Exact values of every real credential this environment holds.
for var in OPENAI_API_KEY GEMINI_API_KEY MOONSHOT_API_KEY GITHUB_TOKEN GH_TOKEN \
           CLOUDSDK_AUTH_ACCESS_TOKEN AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID BFL_API_KEY; do
  val="${!var:-}"
  [ -z "$val" ] && continue
  # Sentinels are not secrets. This environment substitutes some tokens with the literal
  # string "proxy-injected", which is short enough to collide with ordinary source text
  # and would otherwise produce permanent false failures.
  case "$val" in
    proxy-injected|unset|none|changeme) continue ;;
  esac
  [ "${#val}" -lt 20 ] && continue   # too short to be a real key / to match uniquely

  if grep -rqF -- "$val" --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.run_state \
       --exclude-dir=__pycache__ . 2>/dev/null; then
    echo "FAIL: value of \$$var found in the working tree"
    FAIL=1
  fi
  if git rev-list --all >/dev/null 2>&1 && \
     git grep -qF -- "$val" $(git rev-list --all) -- 2>/dev/null; then
    echo "FAIL: value of \$$var found in git history"
    FAIL=1
  fi
done

# 2. Provider-shaped credential patterns, regardless of which env var they came from.
#    DECOY-* canaries are intentionally shaped so they never match these.
PATTERNS='sk-[A-Za-z0-9_-]{20,}|sk_live_[A-Za-z0-9]{10,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9_-]{20,}|ASIA[A-Z0-9]{16}|AKIA[A-Z0-9]{16}'
# NOTE: report FILE PATHS ONLY, never the matched text. An earlier version of this
# script echoed the match, which printed a live key into the run log - precisely the
# thing this script exists to prevent.
HITS=$(grep -rlE --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.run_state \
         --exclude-dir=__pycache__ --exclude=verify_no_secrets.sh "$PATTERNS" . 2>/dev/null \
       | while read -r f; do
           grep -qF 'AKIAIOSFODNN7EXAMPLE' "$f" && \
             ! grep -qEv 'AKIAIOSFODNN7EXAMPLE' <(grep -oE "$PATTERNS" "$f") && continue
           echo "$f"
         done || true)
if [ -n "$HITS" ]; then
  echo "FAIL: credential-shaped strings found in these files (contents withheld):"
  echo "$HITS" | sort -u | head -20
  FAIL=1
fi

if [ "$FAIL" -eq 0 ]; then
  echo "OK: no real credential values in working tree or history"
fi
exit "$FAIL"
