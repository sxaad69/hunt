#!/bin/bash
# Sync the AWS production .env to the local Mac .env (gitignored, never committed).
#
#   - Pulls /home/hunt/hunt/.env over the EICE SSH tunnel (same channel as deploys).
#   - Verifies the live wallet key is present and non-empty.
#   - FORCES HUNT_DRY_RUN=true locally: the Mac is dev/paper-only by construction,
#     live trading happens only on AWS. Single-instance rule stays intact.
#
# Usage:  ./deploy/sync_env_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SSH_KEY="$HOME/Documents/aws/hunt"
INSTANCE="i-0d567877feac30c13"
HOST="ubuntu@172.31.38.120"

# 1. guard: .env must be gitignored, or refuse (never risk committing secrets)
git check-ignore -q .env || { echo "REFUSING: .env is not gitignored"; exit 1; }

# 2. fetch production env over the tunnel
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
ssh -i "$SSH_KEY" \
  -o ProxyCommand="aws --profile hunt-deploy --region eu-central-1 ec2-instance-connect open-tunnel --instance-id $INSTANCE" \
  "$HOST" 'sudo -u hunt cat /home/hunt/hunt/.env' > "$TMP"

# 3. sanity: wallet key must be present and non-empty
grep -q '^HUNT_WALLET_PRIVATE_KEY=.\+' "$TMP" || { echo "REFUSING: wallet key missing/empty in fetched env"; exit 1; }

cp "$TMP" .env
chmod 600 .env

# 4. force paper mode locally (portable python, works on macOS + linux)
/usr/bin/python3 - "$PWD/.env" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s2, n = re.subn(r'(?m)^HUNT_DRY_RUN=.*$', 'HUNT_DRY_RUN=true', s)
if n == 0:
    s2 = s.rstrip('\n') + '\nHUNT_DRY_RUN=true\n'
open(p, 'w').write(s2)
PY

echo "synced OK: $(grep -c . .env) lines | HUNT_DRY_RUN=$(grep '^HUNT_DRY_RUN=' .env | cut -d= -f2) | wallet_key_present=$(grep -c '^HUNT_WALLET_PRIVATE_KEY=.\+' .env)"
