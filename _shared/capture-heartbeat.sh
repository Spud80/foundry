#!/usr/bin/env bash
# capture-heartbeat.sh - daily heartbeat against filehub memory-capture cron.
#
# Reads /var/lib/memory-capture/last-run.json on filehub (written by
# memory-capture.py at end-of-run, per PLAN-memory-pipeline-hardening
# Phase 100). Alarms via Telegram if the marker is older than 25 hours,
# missing, malformed, or if filehub is unreachable.
#
# Activated by cron.d/capture-heartbeat.cron at 19:00 (must run >=1h
# after filehub capture-cron at 18:00 - cross-ref-kommentar i cron-fila).
#
# Requires GNU coreutils (date -d ISO8601) - verified on Debian-based
# foundry host. Will exit 1 under set -euo pipefail on macOS/busybox if
# someone deploys the script elsewhere.
#
# Exit codes: always 0 (failures are alarms, not job failures). Lets cron
# keep firing daily during an outage - "no alarm dedupe" is intentional
# nag-behavior per SPEC (the previous outage went 24h+ unnoticed).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# notify-core.sh source's secrets.env and gives us enqueue_msg + drain_queue.
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify-core.sh"

FILEHUB_HOST="${FILEHUB_HOST:-claude@filehub}"
MARKER_PATH="${MARKER_PATH:-/var/lib/memory-capture/last-run.json}"
STALE_THRESHOLD_HOURS="${STALE_THRESHOLD_HOURS:-25}"

alarm() {
    enqueue_msg "$1" >/dev/null
    drain_queue || true
}

# Read marker via SSH. Two retry-classes:
#   ssh_exit == 255: network/auth-fail (Tailscale brief disconnect) -> retry once
#   anything else  : marker missing / read error                    -> no retry
#
# StrictHostKeyChecking=accept-new lets a freshly-deployed foundry CT learn
# filehub's host key on first call without an out-of-band manual `ssh filehub`.
# Auth itself is via Tailscale SSH (RunSSH on filehub), so identity is
# verified at the WireGuard layer; the system-OpenSSH host-key is just used
# for the post-tailnet handshake.
read_marker_once() {
    ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        "$FILEHUB_HOST" "cat ${MARKER_PATH}" 2>/dev/null
}

marker_content=""
ssh_exit=0
marker_content="$(read_marker_once)" || ssh_exit=$?

if [ "$ssh_exit" -eq 255 ]; then
    sleep 60
    ssh_exit=0
    marker_content="$(read_marker_once)" || ssh_exit=$?
    if [ "$ssh_exit" -eq 255 ]; then
        alarm "capture-heartbeat WARNING: capture status unknown (filehub unreachable after retry, last ssh exit 255)"
        exit 0
    fi
fi

if [ "$ssh_exit" -ne 0 ] || [ -z "$marker_content" ]; then
    alarm "capture-heartbeat WARNING: capture stale (marker missing at ${FILEHUB_HOST}:${MARKER_PATH})"
    exit 0
fi

completed_at="$(printf '%s' "$marker_content" | jq -r '.completed_at // empty' 2>/dev/null || true)"
sessions_written="$(printf '%s' "$marker_content" | jq -r '.sessions_written // empty' 2>/dev/null || true)"

if [ -z "$completed_at" ]; then
    alarm "capture-heartbeat WARNING: capture stale (marker malformed at ${FILEHUB_HOST}:${MARKER_PATH} - completed_at missing)"
    exit 0
fi

completed_epoch="$(date -d "$completed_at" +%s 2>/dev/null || echo "")"
if [ -z "$completed_epoch" ]; then
    alarm "capture-heartbeat WARNING: capture stale (marker completed_at unparseable: '${completed_at}')"
    exit 0
fi

now_epoch="$(date +%s)"
age_h=$(( (now_epoch - completed_epoch) / 3600 ))

if [ "$age_h" -ge "$STALE_THRESHOLD_HOURS" ]; then
    alarm "capture-heartbeat WARNING: capture stale (last ran ${age_h}h ago, ${sessions_written:-?} sessions written; threshold ${STALE_THRESHOLD_HOURS}h)"
    exit 0
fi

# Silent exit when marker is fresh.
exit 0
