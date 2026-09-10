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

## Local dev + env parity (Mac)
```bash
./deploy/sync_env_local.sh   # pull AWS .env (keys + wallet) -> local .env, forces HUNT_DRY_RUN=true
.venv/bin/python -m hunt.paper.run 60   # paper smoke: proves boot/gate/exit/shutdown
```
- Mac = develop/test only. **Never run live on Mac** (`HUNT_DRY_RUN=true` is forced by the sync script).
- Mac network limits (verified 2026-09-10): `*.pump.fun` does not resolve here, so the
  safety-net poll is degraded locally; PumpPortal WS, Helius RPC, and DexScreener work.
  Local runs prove boot/gate/exit/shutdown + instruction builders — AWS is the full-fidelity runner.

## Deploy flow (code changes — git ONLY)
```
# on Mac
git add -A && git commit -m "..." && git push
# on AWS (via SSH)
sudo -iu hunt bash -c 'cd /home/hunt/hunt && git pull'
sudo systemctl restart hunt
```
- **No scp, no ad-hoc `/tmp/*.py` scripts, ever.** Every diagnostic/operator tool lives in
  the repo (`audit/`, `tools/`) and travels by `git pull`. Strays archived 2026-09-10
  to `/tmp/hunt_archive_20260910/` on the instance.
Heavy files (DB, logs, state) NEVER go through git — DB backups live in S3
(`hunt-state-362457597397-euc1`), secrets in SSM Parameter Store (`/hunt/*`, SecureString).

## Live start (deliberate act only — starting the service is NOT consent)
The engine refuses `HUNT_DRY_RUN=false` unless `hunt/data/live_armed` exists, and
refuses to run twice (`state/hunt.lock`). To arm live on AWS:
```
# on AWS, ONLY on explicit operator order, service stopped:
sudo -u hunt touch /home/hunt/hunt/hunt/data/live_armed
sudo systemctl start hunt   # NOT restart-into-live blindly
```
- Remove the marker to disarm: `sudo rm /home/hunt/hunt/hunt/data/live_armed`
- `/pause` (Telegram) or `touch hunt/data/paused` stops ALL new opens, live or paper.
- Emergency: `touch hunt/data/kill_live` force-closes live next tick; `tools/liquidate.py
  --confirm` sells every bag to SOL. Telegram `/kill` and `/close_all` do the same remotely.

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
- Live wallet key generated on the instance; Mac holds a synced copy (`sync_env_local.sh`)
  that is paper-locked and gitignored. Live trading happens only on AWS.
- Emergency: `sudo -u hunt .venv/bin/python tools/liquidate.py --confirm` sells all bags to SOL

## Monitoring identity (IAM)
- Scoped IAM user `hunt-deploy`: only EC2 Instance Connect (SSH tunnel) + instance describe.
- Keys in `~/.aws/credentials` profile `[hunt-deploy]` — **never expire**, no `aws login` needed.
- Cannot read Parameter Store secrets, cannot touch trading or wallets.
- Revoke anytime: `aws iam delete-access-key --user-name hunt-deploy --access-key-id <key>` (root).
