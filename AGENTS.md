# AGENTS.md — hunt project notes for AI agents

Memecoin paper-trading bot ("moonshot hunter") for pump.fun. Python/asyncio.
Primary runtime: AWS EC2 Frankfurt. Mac = dev machine + fallback.
**Paper mode only (`HUNT_DRY_RUN=true`) — never place real trades. Never commit secrets.**

## Golden rules
- **Single instance**: only ONE hunt process runs the campaign at a time. Mac is standby.
- **Deploy flow**: edit/test on Mac → `git push` → SSH to AWS → `git pull` → `sudo systemctl restart hunt`.
- **Heavy files never in git**: DB, logs, state, backups go through S3 (`s3://hunt-state-362457597397-euc1`).
- **Strategy changes**: max ONE evidence-backed tweak per day, logged in the daily digest. No blind tuning.
- `.env` holds API keys — gitignored, never printed to logs.

## Where things run
- **AWS**: EC2 `i-0d567877feac30c13`, t3.micro, eu-central-1b (Frankfurt), Ubuntu 24.04, user `hunt`, repo `/home/hunt/hunt`
- **SSH** (no aws login needed):
  `ssh -i ~/Documents/aws/hunt -o ProxyCommand="aws --profile hunt-deploy --region eu-central-1 ec2-instance-connect open-tunnel --instance-id i-0d567877feac30c13" ubuntu@172.31.38.120`
- **Service**: `systemctl status hunt` (Restart=always) · logs: `/home/hunt/hunt/logs/hunt_YYYY-MM-DD.log` (UTC)
- **Status helper**: `.venv/bin/python /home/hunt/hunt/status.py` (decisions/open/closed/pnl)
- **Secrets**: SSM Parameter Store `/hunt/*` (SecureString) · state backups: S3 bucket above
- **Monitoring identity**: IAM user `hunt-deploy` (scoped: EICE tunnel + describe only), profile `[hunt-deploy]`, keys never expire. Root `aws login` sessions expire in ~15 min — don't rely on them.

## Architecture (key modules)
- `hunt/paper/run.py` — the campaign engine: discovery queue, gate chain, entry pricing, open/exit wiring
- `hunt/watch/price_feed.py` — real-time bonding-curve pricing (Helius `accountSubscribe`, one shared WS)
- `hunt/watch/discovery_ws.py` — PumpPortal `subscribeNewToken` stream (sub-second discovery, free)
- `hunt/paper/run.py::survival_filter` — gate chain (dust floor/ceiling, socials, model, intel, dev reputation)
- `hunt/paper/run.py::_process_exit` — exit ladder (see strategy below)
- `hunt/notify/` — Telegram alerts, control bot (`/status /positions /pnl /pause /kill`), daily digest
- `deploy/` — systemd units, AWS setup scripts, DEPLOY.md
- `audit/top_runners.py` — daily "did we miss runners" audit (run on AWS: needs network + live decisions DB)
- DB: SQLite at `hunt/data/hunt.sqlite3` — every decision stores intel (top10/holders/snipers/dev_pct/burst)

## Strategy (current, frozen until evidence says otherwise)
- **Discovery**: PumpPortal stream → 90s waitlist (coins are born ~28 SOL mcap; judge at 90s with live mcap)
- **Gates**: dust floor ≥50 SOL · ceiling ≤3000 SOL · top10>75% veto · snipers≥2 veto · socials + survival model · SolanaTracker risk · dev reputation (serial_rugger veto)
- **Exits**: SL −20% pre-tier · bank 50% @ +40% · 25% @ +60% · breakeven floor between tiers · moon bag (25%) laddered trail: 30% <3x → 20% @3x → 12% @10x → 8% @50x · max_hold 6h (24h for bags)
- **Pricing**: Helius curve ticks (exact, ~0–2% vs pump.fun indexer); GeckoTerminal fallback for graduated tokens

## Known traps — read before changing anything
- `SL_PCT` is ALREADY percent. The −2000% bug (multiplying by 100) made the stop-loss unreachable for months.
- Peak updates in `_process_exit` MUST commit immediately, else the trail rebases downward on hot tokens.
- `accountSubscribe` pushes only on CHANGE — no initial state. Silence from dead curves is normal.
- Helius free plan: `transactionSubscribe` paywalled; `accountSubscribe` fine; subscription cap ~40 (we manage).
- PumpPortal: `subscribeNewToken` free; trade streams + trade API need a 0.02 SOL-funded key.
- pump.fun API `market_cap` units: SOL for Solana coins, USD for EVM coins. Always verify.
- Coins are born ~28 SOL ($3K) and often graduate to PumpSwap within minutes (curve zeroes out, price moves to AMM).
- `base_decimals` varies (6 classic; stonkfun uses 9) — verify before price math on unknown programs.
- Safety-net poll must run on its own clock — it starved once during launch bursts and missed launches.
- Jupiter does NOT route bonding curves — curve entries need the pump program (deferred build).

## Audit tooling (`audit/`)
- `top_runners.py [hours]` — ranks today's ≥10x runners and classifies our response per coin:
  POSITION taken / seen+REJECTED (with reason) / never seen. Run on AWS (network + live DB):
  `cd /home/hunt/hunt && sudo -u hunt .venv/bin/python /tmp/top_runners.py 24`
- Methodology matters: a "missed moonshot" = seen at birth and dust-rejected. Giants first-seen above the
  ceiling are not misses. Zero gate-misses is the KPI (verified across 100+ runners so far).

## Monitoring
- ZCode automation every 2h: AWS health + funnel + PnL via SSH; Mac standby check; auto-fix clear bugs.
- 08:00 daily: full digest → Telegram (server-side timer also sends independently).
- Telegram: real-time alerts (open/TP/SL/close) + control commands.
- If SSH/monitoring fails with AWS auth errors: the bot runs fine on systemd — just note it.

## Current campaign state (update as things change)
- 7-day paper campaign on AWS, started Sep 6 ~23:59 local. Baseline: 4h clean window +50 SOL (USUR 1431x).
- Two runner species: A = classic curve rides (our edge), B = instant-mega launches (observed/logged, not playable yet).
