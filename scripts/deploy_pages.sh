#!/usr/bin/env bash
# Redeploy GitHub Pages after the bot's commit, and confirm that the deploy worked.
#
# Why this exists: a push made with GITHUB_TOKEN does not start other workflows, so the daily job starts
# pages.yml itself. That used to be a single `gh workflow run` that was allowed to fail silently. If it
# failed, index.html on main was new but the live site stayed old, and nothing said so.
#
# What it does:  1. start pages.yml (up to DEPLOY_ATTEMPTS tries)
#                2. wait for the run that start created, and require that it finished with "success"
# Any failure exits 1 with a ::error:: line, so the daily job turns red and GitHub reports it.
# The listings are already committed by then; only the redeploy is in question.
#
# Usage: scripts/deploy_pages.sh <branch>        (needs GH_TOKEN; "gh" must be on PATH)
# Tunables (environment, mainly for tests): PAGES_WORKFLOW, DEPLOY_ATTEMPTS, DEPLOY_RETRY_WAIT,
#                                           DEPLOY_POLL_WAIT, DEPLOY_MAX_POLLS (default 36 x 10 s = 6 min)
set -u

REF="${1:-}"
if [ -z "$REF" ]; then
  echo "usage: $0 <branch>" >&2
  exit 2
fi
WORKFLOW="${PAGES_WORKFLOW:-pages.yml}"
ATTEMPTS="${DEPLOY_ATTEMPTS:-3}"
RETRY_WAIT="${DEPLOY_RETRY_WAIT:-20}"
POLL_WAIT="${DEPLOY_POLL_WAIT:-10}"
MAX_POLLS="${DEPLOY_MAX_POLLS:-36}"

fail() {
  echo "::error title=GitHub Pages was not redeployed::$1"
  echo "The listings were saved to $REF, but the live site may still show the previous version." >&2
  echo "Fix: Actions tab > \"$WORKFLOW\" > Run workflow (branch $REF)." >&2
  exit 1
}

# Highest run id of this workflow's manual runs before we start one; our run is the first one above it.
before="$(gh run list --workflow "$WORKFLOW" --event workflow_dispatch --branch "$REF" --limit 1 \
            --json databaseId --jq '.[0].databaseId // 0' 2>/dev/null)" || before=""
case "$before" in ''|*[!0-9]*) before=0 ;; esac

started=""
for i in $(seq 1 "$ATTEMPTS"); do
  if gh workflow run "$WORKFLOW" --ref "$REF"; then
    started=yes
    echo "Pages deploy started (attempt $i of $ATTEMPTS)."
    break
  fi
  echo "Could not start the Pages deploy (attempt $i of $ATTEMPTS)." >&2
  [ "$i" -lt "$ATTEMPTS" ] && sleep "$RETRY_WAIT"
done
[ -n "$started" ] || fail "could not start $WORKFLOW after $ATTEMPTS attempts."

for p in $(seq 1 "$MAX_POLLS"); do
  sleep "$POLL_WAIT"
  # Lines "<id> <status> <conclusion>" for runs newer than $before; a failed call is just retried.
  line="$(gh run list --workflow "$WORKFLOW" --event workflow_dispatch --branch "$REF" --limit 10 \
            --json databaseId,status,conclusion \
            --jq ".[] | select(.databaseId > $before) | \"\\(.databaseId) \\(.status) \\(.conclusion)\"" 2>/dev/null \
          | sort -n | head -n 1)" || line=""
  [ -n "$line" ] || continue
  read -r id status conclusion <<EOF
$line
EOF
  if [ "$status" = "completed" ]; then
    if [ "$conclusion" = "success" ]; then
      echo "Pages deploy finished: success (run $id)."
      exit 0
    fi
    fail "run $id of $WORKFLOW ended with \"$conclusion\"."
  fi
done
fail "$WORKFLOW did not finish within $((MAX_POLLS * POLL_WAIT)) s (or no run appeared)."
