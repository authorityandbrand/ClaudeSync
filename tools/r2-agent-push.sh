#!/usr/bin/env bash
# r2-agent-push.sh — Push agent workspace results back to R2
# Usage: r2-agent-push.sh [local_dir] [bucket] [prefix]
#
# Falls back to $R2_WORKSPACE / $R2_ATTACH_BUCKET / $R2_ATTACH_PREFIX
# set by r2-agent-attach.sh if args are omitted.

LOCAL="${1:-${R2_WORKSPACE:?R2_WORKSPACE not set. Run r2-agent-attach.sh first or pass args.}}"
BUCKET="${2:-${R2_ATTACH_BUCKET:?R2_ATTACH_BUCKET not set.}}"
PREFIX="${3:-${R2_ATTACH_PREFIX:-}}"

echo "[r2-push] Syncing ${LOCAL}/ → r2://${BUCKET}/${PREFIX}"
r2 sync "${LOCAL}/" "r2://${BUCKET}/${PREFIX}" --workers 8
echo "[r2-push] Done."
