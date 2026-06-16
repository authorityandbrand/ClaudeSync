#!/usr/bin/env bash
# r2-agent-attach.sh — Pull files from R2 into a local workspace for an agent
# Usage: source r2-agent-attach.sh <bucket> <prefix> [local_dir]
#
# Sets $R2_WORKSPACE so the agent knows where its files landed.
# After work is done, call r2-agent-push.sh to sync results back.

BUCKET="${1:?Usage: r2-agent-attach.sh <bucket> <prefix> [local_dir]}"
PREFIX="${2:-}"
LOCAL="${3:-/tmp/r2-workspace-$$}"

export R2_WORKSPACE="$LOCAL"
export R2_ATTACH_BUCKET="$BUCKET"
export R2_ATTACH_PREFIX="$PREFIX"

mkdir -p "$LOCAL"
echo "[r2-attach] Pulling r2://${BUCKET}/${PREFIX} → ${LOCAL}"
r2 sync "r2://${BUCKET}/${PREFIX}" "${LOCAL}/" --workers 8
echo "[r2-attach] Workspace ready: ${LOCAL}"
echo "[r2-attach] $(ls "$LOCAL" | wc -l) files"
