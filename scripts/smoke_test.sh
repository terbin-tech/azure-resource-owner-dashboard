#!/usr/bin/env bash
# Local smoke test after a deploy. Checks that the page refuses an unsigned request, starts one
# scheduled run by hand and waits for it to succeed in the logs. Prints only PASS or
# FAIL: <step>. Needs the maintainer's own login, and the class subscription ID as its one
# argument: `az login`, then `bash scripts/smoke_test.sh <subscription id>`.
# Do not run it around 06:00 or 16:00 UTC: a scheduled run would count as the one started here.
# Nothing is written to a file, and the master key lives in one shell variable for one request.
set -u
fail() { unset KEY; echo "FAIL: $1"; exit 1; }

SUB=${1:-}
[[ $SUB =~ ^[0-9A-Fa-f-]{36}$ ]] || fail "usage: bash scripts/smoke_test.sh <subscription id>"
# The CLI's saved default subscription can change between runs, so check it agrees. This one
# command is the exception to passing the subscription on every az command.
[ "$(az account show --query id -o tsv 2>/dev/null)" = "$SUB" ] \
  || fail "az account show does not match the subscription ID (run az login or az account set)"

OUT=$(az deployment sub show --name resource-owner-dashboard --subscription "$SUB" \
  --query properties.outputs -o json 2>/dev/null) || fail "deployment outputs"
output() {
  printf '%s' "$OUT" | python3 -c '
import json, re, sys
value = json.load(sys.stdin)[sys.argv[1]]["value"]
print(value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]+", value) else "")
' "$1" 2>/dev/null
}
HOST=$(output defaultHostName); [ -n "$HOST" ] || fail "output defaultHostName"
APP=$(output functionAppName); [ -n "$APP" ] || fail "output functionAppName"
RG=$(output resourceGroupName); [ -n "$RG" ] || fail "output resourceGroupName"
WORKSPACE=$(output workspaceCustomerId); [ -n "$WORKSPACE" ] || fail "output workspaceCustomerId"

# 1. No sign-in: refused. 2. A browser's request: sent on to the school sign-in. A browser-like
# User-Agent and Accept: text/html are both needed for the redirect; without them the answer is 401.
# curl's error text names the host, so each curl's stderr goes to /dev/null.
CODE=$(curl -q -s --max-time 30 -o /dev/null -w '%{http_code}' "https://$HOST/api/page" 2>/dev/null) \
  || fail "unsigned page request"
[ "$CODE" = "401" ] || fail "unsigned page request is not refused with 401"
BROWSER=$(curl -q -s --max-time 30 -o /dev/null -w '%{http_code} %{redirect_url}' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36' \
  -H 'Accept: text/html' "https://$HOST/api/page" 2>/dev/null) || fail "browser page request"
[[ $BROWSER == "302 https://login.microsoftonline.com/"* ]] \
  || fail "browser page request is not sent to login.microsoftonline.com"

# 3. Start one run with the admin call and the master key. The key goes in through stdin, so it is
# not in the process list, and curl is not told to follow redirects: a refused request answers
# with a redirect to the sign-in host, and a followed one would send the key on.
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
KEY=$(az functionapp keys list --name "$APP" --resource-group "$RG" --subscription "$SUB" \
  --query masterKey -o tsv 2>/dev/null) || fail "master key"
[ -n "$KEY" ] || fail "master key"
RUN=$(printf 'x-functions-key: %s\n' "$KEY" | curl -q -s --max-time 60 -o /dev/null -w '%{http_code}' -H @- -H 'Content-Type: application/json' -d '{}' -X POST "https://$HOST/admin/functions/scan" 2>/dev/null) \
  || fail "manual run request"
unset KEY
case $RUN in
  202) ;;
  401 | 302) fail "manual run refused by sign-in: wait for the next scheduled run" ;;
  *) fail "manual run request" ;;
esac

# 4. Wait for the run in the logs, every 60 seconds for up to 15 minutes. Ingestion lags a few
# minutes behind the run. A query that fails or answers oddly is retried, and when every one did,
# the failure is the query, not the run.
QUERY="AppRequests | where Name == \"scan\" and TimeGenerated > datetime($START) | summarize Runs = count(), Failed = countif(Success == false)"
BODY=$(python3 -c 'import json, sys; print(json.dumps({"query": sys.argv[1]}))' "$QUERY") \
  || fail "log query"
QUERY_ERRORS=0
for _ in $(seq 15); do
  sleep 60
  ROW=$(az rest --method post --uri "https://api.loganalytics.io/v1/workspaces/$WORKSPACE/query" \
    --resource https://api.loganalytics.io --subscription "$SUB" \
    --headers Content-Type=application/json --body "$BODY" \
    --query 'tables[0].rows' -o tsv 2>/dev/null) || { QUERY_ERRORS=$((QUERY_ERRORS + 1)); continue; }
  # tsv prints one line per row and one tab-separated column per value; the summarize returns one row.
  read -r RUNS FAILED <<< "$ROW"
  [[ $RUNS =~ ^[0-9]+$ && $FAILED =~ ^[0-9]+$ ]] || { QUERY_ERRORS=$((QUERY_ERRORS + 1)); continue; }
  if [ "$RUNS" -ge 1 ]; then
    [ "$FAILED" -eq 0 ] || fail "the scan run failed (see the function's logs)"
    echo PASS
    exit 0
  fi
done
[ "$QUERY_ERRORS" -lt 15 ] || fail "log query failed on every attempt"
fail "no scan run in the logs within 15 minutes"
