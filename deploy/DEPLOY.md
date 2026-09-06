# Hunt bot — AWS deployment (t3.micro / Ubuntu 24.04)

## 1. Create the instance
- Ubuntu Server 24.04 LTS, t3.micro (or t3.small if free-tier allows)
- Security group: inbound **port 22 only**, restricted to your IP. Everything else outbound.
- 8GB gp3 root volume is fine.

## 2. One-time setup (SSH in)
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
sudo adduser --disabled-password --gecos "" hunt
sudo -iu hunt
git clone <repo-url> hunt && cd hunt
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env   # fill keys (NEVER commit this)
mkdir -p logs state hunt/data
```

## 3. Install the service
```bash
sudo cp deploy/hunt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hunt
systemctl status hunt          # should be active
journalctl -u hunt -f          # live logs
```

## 4. Daily digest (server-side, Telegram)
```bash
sudo cp deploy/hunt-digest.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hunt-digest.timer
```

## 5. Cutover rules
- **ONE live instance** — stop the Mac run before enabling the AWS one (`pkill -f hunt.paper.run` locally, disarm local watchdog: `pkill -f watchdog.sh`).
- Copy `state/run_meta.json` to preserve the campaign clock, or start a fresh window.
- Backtest / heavy analysis stays on the Mac (t3.micro CPU credits are for the hunt only).

## 6. Key hygiene (before dust-live)
- `.env` is chmod 600, owned by `hunt` user.
- When real money loads: dedicated wallet with only the operating balance; consider AWS Secrets Manager over plaintext.
- SSH: key-only, password auth disabled (`PasswordAuthentication no` in sshd_config).
