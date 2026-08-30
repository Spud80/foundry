#!/usr/bin/env bash
# _preflight.sh - classify a filehub-cleanup result into the operator's message.
#
# Sourced by run.sh. Split out so the classification can be tested without
# running the job, and so the message the operator actually receives is a named
# thing rather than a string built inline at the call site.
#
# Why this exists at all: the pre-flight used to treat EVERY non-zero exit as
# "Syncthing conflicts unresolved". On 2026-08-28 and 2026-08-29 a tailnet
# policy change made `ssh filehub-cleanup` return 255 before the remote command
# ever ran, and both nights were reported as a conflict problem. Nobody looked
# at the network, and memory-extract lost two nights to an alert that pointed
# the wrong way. The check answers "are there conflicts?" only when it actually
# ran; ssh's own failures arrive on the same channel as the answer, and
# collapsing the two is what made the log unreadable.
#
# The exit codes are sync-conflict-cleanup.py's own contract, from its header:
#   0  success - no remaining conflicts in --require-clean paths
#   1  conflicts in --require-clean paths landed in quarantine (manual review)
#   2  unexpected error inside the cleanup itself
# Anything else means the check never ran. 255 is ssh's own transport failure.
#
# A run that could not be checked still stops. Continuing would extract from a
# corpus nobody has confirmed is conflict-free, and a conflict folded into the
# memory corpus is expensive to unpick again; a skipped night is cheap, because
# capture is idempotent per session. The failure that cost two nights was never
# the abort - it was the message.

# classify_cleanup_result <exit-status> <combined-output>
#
# Prints the notify text for a run that must stop; prints nothing for a clean
# run. Returns 0 when the job may continue, 1 when it must stop.
classify_cleanup_result() {
    local status="$1"
    local out="$2"
    local first_line
    first_line="$(printf '%s' "$out" | head -n 1)"

    case "$status" in
        0)
            return 0
            ;;
        1)
            printf 'pre-flight blocked: Syncthing conflicts in protected paths are quarantined and need manual review (exit 1). See logs.'
            ;;
        2)
            printf 'pre-flight blocked: filehub-cleanup hit an unexpected error (exit 2): %s. See logs.' "$first_line"
            ;;
        *)
            # The transport case, and the whole point of the split: say what
            # actually failed and quote it, so the first reader can act on the
            # real cause instead of hunting for conflicts that do not exist.
            printf 'pre-flight could NOT run: ssh filehub-cleanup failed with exit %s: %s. This is a transport failure, not a conflict report - conflict state is unknown and extract is skipped.' "$status" "$first_line"
            ;;
    esac
    return 1
}
