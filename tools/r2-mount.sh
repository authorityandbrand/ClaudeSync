#!/usr/bin/env bash
# r2-mount.sh — Mount all R2 buckets under /mnt/r2/<bucket>/
# Credentials loaded from environment — nothing written to disk.
# Usage: r2-mount.sh [--unmount] [--bucket NAME]

set -euo pipefail

ENDPOINT="${jurisdiction:-${R2_ENDPOINT_URL:-}}"
ACCESS="${Access_Key_ID:-${AWS_ACCESS_KEY_ID:-}}"
SECRET="${Secret_Access_Key:-${AWS_SECRET_ACCESS_KEY:-}}"
MOUNT_ROOT="/mnt/r2"

log() { echo "[r2-mount] $*"; }

if [[ -z "$ENDPOINT" || -z "$ACCESS" || -z "$SECRET" ]]; then
    echo "ERROR: R2 credentials not in environment." >&2; exit 1
fi

unmount_all() {
    log "Unmounting all R2 buckets..."
    for mp in "${MOUNT_ROOT}"/*/; do
        mountpoint -q "$mp" 2>/dev/null && fusermount -u "$mp" && log "Unmounted: $mp" || true
    done
}

mount_bucket() {
    local bucket="$1"
    local mp="${MOUNT_ROOT}/${bucket}"
    mkdir -p "$mp"
    if mountpoint -q "$mp" 2>/dev/null; then
        log "Already mounted: $mp"; return
    fi
    AWSACCESSKEYID="$ACCESS" AWSSECRETACCESSKEY="$SECRET" \
    s3fs "$bucket" "$mp" \
        -o url="$ENDPOINT" \
        -o use_path_request_style \
        -o allow_other \
        -o umask=0022 \
        -o connect_timeout=10 \
        -o retries=3 \
        2>/dev/null && log "✓ r2://${bucket} → ${mp}" \
        || log "✗ Failed: ${bucket}"
}

UNMOUNT=false; SINGLE_BUCKET=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --unmount|-u) UNMOUNT=true ;;
        --bucket|-b)  SINGLE_BUCKET="$2"; shift ;;
        *) echo "Usage: r2-mount.sh [--unmount] [--bucket NAME]"; exit 1 ;;
    esac; shift
done

if $UNMOUNT; then unmount_all; exit 0; fi
mkdir -p "$MOUNT_ROOT"

if [[ -n "$SINGLE_BUCKET" ]]; then
    mount_bucket "$SINGLE_BUCKET"; exit 0
fi

log "Mounting all R2 buckets → ${MOUNT_ROOT}/"
COUNT=0
while read -r _ _ bucket; do
    [[ -z "$bucket" ]] && continue
    mount_bucket "$bucket"
    ((COUNT++))
done < <(r2 ls)

log "Done — ${COUNT} buckets mounted under ${MOUNT_ROOT}/"
