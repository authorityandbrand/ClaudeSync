#!/usr/bin/env bash
# session-start.sh — Bootstrap R2 storage tools on every web session
# Runs async so the session starts immediately while setup continues in background.
set -euo pipefail

# Only run in remote (web) sessions
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

echo '{"async": true, "asyncTimeout": 300000}'

# ─────────────────────────────────────────────────────────────────────────────
BIN="/usr/local/bin"
TOOLS_BUCKET="claude"
TOOLS_PREFIX="tools"
MOUNT_ROOT="/mnt/r2"
CACHE_DIR="/tmp/s3fs-cache"
LOG="/tmp/r2-bootstrap.log"

ACCESS="${Access_Key_ID:-${AWS_ACCESS_KEY_ID:-}}"
SECRET="${Secret_Access_Key:-${AWS_SECRET_ACCESS_KEY:-}}"
ENDPOINT="${jurisdiction:-${R2_ENDPOINT_URL:-}}"

log() { echo "[session-start] $*" | tee -a "$LOG"; }

log "=== Session start $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# ── 1. Python packages ────────────────────────────────────────────────────────
if ! python3 -c "import boto3" 2>/dev/null; then
    log "Installing boto3..."
    pip3 install boto3 --quiet
fi

# Install ctxsync project deps
if [ -f "${CLAUDE_PROJECT_DIR:-/home/user/ClaudeSync}/requirements.txt" ]; then
    pip3 install -r "${CLAUDE_PROJECT_DIR:-/home/user/ClaudeSync}/requirements.txt" --quiet 2>/dev/null || true
fi

# ── 2. s3fs ───────────────────────────────────────────────────────────────────
if ! which s3fs &>/dev/null; then
    log "Installing s3fs..."
    apt-get install -y --fix-missing s3fs 2>/dev/null || log "WARN: s3fs install failed"
fi

# ── 3. Restore r2 tools from R2 if missing ───────────────────────────────────
declare -A TOOL_MAP=(
    ["r2"]="${BIN}/r2"
    ["r2-mount.sh"]="${BIN}/r2-mount"
    ["r2-agent-attach.sh"]="${BIN}/r2-attach"
    ["r2-agent-push.sh"]="${BIN}/r2-push"
    ["r2-agent-context.py"]="${BIN}/r2-context"
    ["r2-catalog.py"]="${BIN}/r2-catalog"
)

restore_tools() {
    python3 - <<PYEOF
import boto3, os, stat
from botocore.config import Config
client = boto3.client("s3",
    endpoint_url=os.environ["jurisdiction"],
    aws_access_key_id=os.environ["Access_Key_ID"],
    aws_secret_access_key=os.environ["Secret_Access_Key"],
    region_name="auto",
    config=Config(signature_version="s3v4"),
)
tool_map = {
    "r2":                   "/usr/local/bin/r2",
    "r2-mount.sh":          "/usr/local/bin/r2-mount",
    "r2-agent-attach.sh":   "/usr/local/bin/r2-attach",
    "r2-agent-push.sh":     "/usr/local/bin/r2-push",
    "r2-agent-context.py":  "/usr/local/bin/r2-context",
    "r2-catalog.py":        "/usr/local/bin/r2-catalog",
}
for src, dest in tool_map.items():
    if not os.path.exists(dest):
        try:
            data = client.get_object(Bucket="claude", Key=f"tools/{src}")["Body"].read()
            with open(dest, "wb") as f: f.write(data)
            os.chmod(dest, 0o755)
            print(f"[session-start] Restored: {dest}")
        except Exception as e:
            print(f"[session-start] WARN: Could not restore {dest}: {e}")
PYEOF
}

MISSING=false
for src in "${!TOOL_MAP[@]}"; do
    dest="${TOOL_MAP[$src]}"
    [[ ! -x "$dest" ]] && MISSING=true && break
done
if $MISSING; then
    log "Restoring R2 tools..."
    restore_tools
fi

# ── 4. Mount all R2 buckets (parallel, with caching, read-only for evidence) ─

# Buckets that should be read-only (legal integrity)
declare -A READONLY_BUCKETS=(
    ["case-001-evidence-constitutional"]=1
    ["case-001-evidence-criminal"]=1
    ["case-001-evidence-criminal-fay-era"]=1
    ["case-001-evidence-criminal-pre-fay"]=1
    ["case-001-evidence-damages"]=1
    ["case-001-evidence-statutory"]=1
    ["evidence-consent-orders"]=1
    ["evidence-criminal-fay"]=1
    ["evidence-txhaf-fraud-170k"]=1
    ["qwr-requests-fay"]=1
    ["qwr-responses-60day"]=1
    ["qwr-responses-overdue"]=1
    ["fdcpa-30day-vod"]=1
    ["respa-60day-violations"]=1
    ["vod-responses-timeline"]=1
)

mkdir -p "$CACHE_DIR" "$MOUNT_ROOT"

mount_bucket() {
    local bucket="$1"
    local mp="${MOUNT_ROOT}/${bucket}"
    mkdir -p "$mp"

    local want_ro=0
    # Check if this bucket should be read-only (passed via env var list)
    if echo "$READONLY_LIST" | grep -qx "$bucket"; then
        want_ro=1
    fi

    # If already mounted, check if ro status matches; unmount if not
    if mountpoint -q "$mp" 2>/dev/null; then
        if [[ "$want_ro" -eq 1 ]]; then
            local current_mode
            current_mode=$(cat /proc/mounts | grep " ${mp} " | awk '{print $4}' | cut -d, -f1)
            if [[ "$current_mode" != "ro" ]]; then
                fusermount -u "$mp" 2>/dev/null || return 0
            else
                return 0  # already mounted ro
            fi
        else
            return 0  # already mounted rw
        fi
    fi

    local ro_flag=""
    [[ "$want_ro" -eq 1 ]] && ro_flag="-o ro"

    AWSACCESSKEYID="$ACCESS" AWSSECRETACCESSKEY="$SECRET" \
    s3fs "$bucket" "$mp" \
        -o url="$ENDPOINT" \
        -o use_path_request_style \
        -o allow_other \
        -o umask=0022 \
        -o use_cache="$CACHE_DIR" \
        -o stat_cache_expire=120 \
        -o stat_cache_interval_expire=30 \
        -o connect_timeout=10 \
        -o retries=3 \
        -o parallel_count=8 \
        -o multipart_size=64 \
        $ro_flag \
        2>/dev/null \
    && log "✓ ${bucket}$([[ $want_ro -eq 1 ]] && echo ' [ro]' || echo '')" \
    || log "✗ ${bucket}"
}

export -f mount_bucket
export ACCESS SECRET ENDPOINT CACHE_DIR MOUNT_ROOT
# Export readonly list as newline-delimited string (works across subshells)
export READONLY_LIST
READONLY_LIST=$(printf '%s\n' "${!READONLY_BUCKETS[@]}")

log "Mounting R2 buckets..."

# Mount in parallel (8 at a time)
r2 ls 2>/dev/null | awk '{print $3}' | \
    xargs -P 8 -I{} bash -c 'mount_bucket "$@"' _ {}

MOUNTED=$(find "$MOUNT_ROOT" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
RO_COUNT=${#READONLY_BUCKETS[@]}
log "Mounted ${MOUNTED} buckets (${RO_COUNT} read-only for legal integrity)"
log "Access: ls /mnt/r2/"
log "=== Bootstrap complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
