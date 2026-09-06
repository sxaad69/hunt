#!/bin/bash
# Hunt AWS instance setup — run as root on the EC2 instance via SSM.
# Idempotent: safe to re-run. Contains NO secrets (fetches from Parameter Store).
set -e
export AWS_DEFAULT_REGION=eu-central-1
APP=/home/hunt/hunt

echo "[1/6] stop hunt (if running)"
systemctl stop hunt 2>/dev/null || true

echo "[2/6] awscli into venv"
sudo -iu hunt bash -c "cd $APP && .venv/bin/pip install -q awscli"

echo "[3/6] .env from Parameter Store"
sudo -u hunt bash $APP/deploy/gen_env.sh

echo "[4/6] campaign state from S3"
sudo -iu hunt bash -c "cd $APP && mkdir -p logs state hunt/data && \
  .venv/bin/aws s3 cp s3://hunt-state-362457597397-euc1/hunt.sqlite3 hunt/data/hunt.sqlite3 --no-progress && \
  .venv/bin/aws s3 cp s3://hunt-state-362457597397-euc1/run_meta.json state/run_meta.json --no-progress"

echo "[5/6] ownership + unit file duration"
chown -R hunt:hunt $APP
REM=$(sudo -iu hunt bash -c "cd $APP && .venv/bin/python -c \"import json,time; m=json.load(open('state/run_meta.json')); print(max(int(m['duration_s']-(time.time()-m['started_ts'])),600))\"")
sed -i "s/ 604800/ $REM/" /etc/systemd/system/hunt.service
systemctl daemon-reload

echo "[6/6] start hunt"
systemctl start hunt
echo "SETUP-OK remaining=$REM"
