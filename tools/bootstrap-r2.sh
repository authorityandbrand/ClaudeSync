#!/usr/bin/env bash
# bootstrap-r2.sh — Cold-start setup for R2 storage tools
# Run automatically via SessionStart hook, or manually: bash bootstrap-r2.sh
set -euo pipefail

TOOLS_BUCKET="claude"
TOOLS_PREFIX="tools"
BIN="/usr/local/bin"

log() { echo "[r2-bootstrap] $*"; }

# ── 1. Ensure boto3 is installed ─────────────────────────────────────────────
if ! python3 -c "import boto3" 2>/dev/null; then
    log "Installing boto3..."
    pip3 install boto3 --quiet
fi

# ── 2. Bootstrap r2 tool if missing (using raw Python) ───────────────────────
if [[ ! -x "${BIN}/r2" ]]; then
    log "r2 tool missing — fetching from R2..."
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
for name in ["r2", "r2-agent-attach.sh", "r2-agent-push.sh", "r2-agent-context.py"]:
    dest = f"${BIN}/{name.replace('.sh','').replace('.py','') if name != 'r2' else name}"
    # map to short names
    dest_map = {
        "r2": "${BIN}/r2",
        "r2-agent-attach.sh": "${BIN}/r2-attach",
        "r2-agent-push.sh": "${BIN}/r2-push",
        "r2-agent-context.py": "${BIN}/r2-context",
    }
    dest = dest_map[name]
    data = client.get_object(Bucket="${TOOLS_BUCKET}", Key="${TOOLS_PREFIX}/" + name)["Body"].read()
    with open(dest, "wb") as f:
        f.write(data)
    os.chmod(dest, 0o755)
    print(f"[r2-bootstrap] Restored: {dest}")
PYEOF
else
    log "r2 tool present ✓"
fi

# ── 3. Restore remaining agent tools if missing ───────────────────────────────
declare -A TOOL_MAP=(
    ["r2-agent-attach.sh"]="${BIN}/r2-attach"
    ["r2-agent-push.sh"]="${BIN}/r2-push"
    ["r2-agent-context.py"]="${BIN}/r2-context"
)

for src_name in "${!TOOL_MAP[@]}"; do
    dest="${TOOL_MAP[$src_name]}"
    if [[ ! -x "$dest" ]]; then
        log "Restoring ${dest}..."
        r2 cp "r2://${TOOLS_BUCKET}/${TOOLS_PREFIX}/${src_name}" "$dest"
        chmod +x "$dest"
    fi
done

# ── 4. Verify connectivity ────────────────────────────────────────────────────
BUCKET_COUNT=$(r2 ls 2>/dev/null | wc -l | tr -d ' ')
log "R2 connected — ${BUCKET_COUNT} buckets available ✓"
log "Tools ready: r2, r2-attach, r2-push, r2-context"
