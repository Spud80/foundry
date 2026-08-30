#!/usr/bin/env bash
# Pins the four pre-flight outcomes of jobs/memory-extract/_preflight.sh.
#
# The bug this locks down: every non-zero exit used to be reported as
# "Syncthing conflicts unresolved". On 2026-08-28 and 2026-08-29 the real cause
# was ssh exit 255 from a tailnet policy change, and the alert sent the reader
# looking for conflicts that did not exist. Two nights of memory-extract were
# lost to a message that pointed the wrong way.
#
# The assertions are about the MESSAGE, not just the return code, because the
# message is the deliverable here - a correct abort with a wrong reason is the
# exact failure being fixed.
#
# Run: bash jobs/memory-extract/_test/smoke_preflight_classification.sh
set -uo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=jobs/memory-extract/_preflight.sh
. "${JOB_DIR}/_preflight.sh"

pass=0
fail=0

check() {
    local name="$1" expect_rc="$2" status="$3" out="$4" must_contain="$5" must_not="${6:-}"
    local msg rc
    msg="$(classify_cleanup_result "$status" "$out")"
    rc=$?
    if [ "$rc" -ne "$expect_rc" ]; then
        printf '  FAIL  [%s] return %s, expected %s\n' "$name" "$rc" "$expect_rc"
        fail=$((fail + 1))
        return
    fi
    if [ -n "$must_contain" ] && [[ "$msg" != *"$must_contain"* ]]; then
        printf '  FAIL  [%s] message lacks %q\n        got: %s\n' "$name" "$must_contain" "$msg"
        fail=$((fail + 1))
        return
    fi
    if [ -n "$must_not" ] && [[ "$msg" == *"$must_not"* ]]; then
        printf '  FAIL  [%s] message must not contain %q\n        got: %s\n' "$name" "$must_not" "$msg"
        fail=$((fail + 1))
        return
    fi
    printf '  PASS  [%s]\n' "$name"
    pass=$((pass + 1))
}

# The verbatim output of the two lost nights, from ~/foundry/logs/memory-extract.log.1.
TRANSPORT_OUT='tailscale: tailnet policy does not permit you to SSH to this node
Connection closed by 100.68.141.121 port 22'

check "clean run continues, says nothing" \
    0 0 '2026-08-27 18:30:23 INFO Lock acquired; scanning /data/sync' '' ''

check "conflicts stop the run and name conflicts" \
    1 1 'WARNING --require-clean breach' 'conflicts' ''

# The regression case. Before the split this returned the conflicts message.
check "transport failure names transport, not conflicts" \
    1 255 "$TRANSPORT_OUT" 'transport failure' 'conflicts unresolved'

check "transport failure quotes the actual cause" \
    1 255 "$TRANSPORT_OUT" 'tailnet policy does not permit' ''

check "transport failure carries the exit code" \
    1 255 "$TRANSPORT_OUT" 'exit 255' ''

# Only the FIRST line is quoted: the second is ssh noise, and a two-line
# Telegram message buries the cause under the connection teardown.
check "transport failure quotes one line only" \
    1 255 "$TRANSPORT_OUT" '' 'Connection closed by'

check "cleanup internal error is its own outcome" \
    1 2 'Unexpected error: boom' 'exit 2' 'transport failure'

# An unrecognised code must fall in the safe direction: unknown state, stop.
check "unknown exit code reads as could-not-run" \
    1 7 'something else entirely' 'could NOT run' ''

printf '\nResults: %s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
