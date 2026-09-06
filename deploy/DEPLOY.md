# Hunt — AWS deployment (t3.micro, eu-central-1 Frankfurt)

**Live instance:** `i-0d567877feac30c13` · Ubuntu 24.04 · eu-central-1b · IP 172.31.38.120 (private only)
**Access:** SSH via EC2 Instance Connect Endpoint `eice-0abfa9d16fb346813` — key at `~/Documents/aws/hunt`
**Service:** systemd `hunt.service` (Restart=always) — paper campaign, 7-day window
**Digest:** systemd `hunt-digest.timer` → Telegram daily 08:00 UTC

## Connect
```bash
ssh -i ~/Documents/aws/hunt \
  -o ProxyCommand="aws --region eu-central-1 ec2-instance-connect open-tunnel --instance-id i-0d567877feac30c13" \
  ubuntu@172.31.38.120
```
(Requires a valid `aws login` session — grants expire ~15 min, re-run `aws login` when needed.)

## Deploy flow (code changes)
```
# on Mac
git add -A && git commit -m "..." && git push
# on AWS (via SSH)
sudo -iu hunt bash -c 'cd /home/hunt/hunt && git pull'
sudo systemctl restart hunt
```
Heavy files (DB, logs, state) NEVER go through git — DB backups live in S3
(`hunt-state-362457597397-euc1`), secrets in SSM Parameter Store (`/hunt/*`, SecureString).

## Service
```
systemctl status hunt        # active = hunting
journalctl -u hunt -f        # live logs
tail -f /home/hunt/hunt/logs/hunt_$(date +%F).log
```
Config: `/home/hunt/hunt/.env` (chmod 600) · DB: `/home/hunt/hunt/hunt/data/hunt.sqlite3`
Campaign clock: `/home/hunt/hunt/state/run_meta.json` (7-day window)

## Security posture
- Zero inbound ports — access only via SSM/EICE (AWS IAM-gated)
- Secrets in SSM SecureString; instance role limited to `/hunt/*` params + campaign S3 bucket
- Wallet key: never stored (paper mode) — when going live, use a dedicated dust wallet + Secrets Manager
