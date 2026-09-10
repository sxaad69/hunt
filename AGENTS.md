# AGENTS.md — hunt project notes for AI agents

Memecoin paper-trading bot ("moonshot hunter") for pump.fun. Python/asyncio.
Primary runtime: AWS EC2 Frankfurt. Mac = dev machine + fallback.
**Paper default (`HUNT_DRY_RUN=true`); live = `HUNT_DRY_RUN=false` + funded wallet key in `.env`. Never commit secrets.**

## Golden rules
- **Single instance**: only ONE hunt process runs the campaign at a time. Mac is standby.
- **Paper default**: `HUNT_DRY_RUN=true` — pure paper (default, always). **Live** = `HUNT_DRY_RUN=false` + `HUNT_WALLET_PRIVATE_KEY` in `.env` (from SSM `/hunt/*`). Live starts fail-closed: preflight requires the wallet key and `balance >= live_min_balance_sol` (0.2), else the service exits WITHOUT trading. Flips to live only via `.env` on AWS — never an unplanned default. Always verify what mode a process started in before hardening decisions.
- **Deploy flow (git ONLY)**: edit/test on Mac → `git push` → SSH to AWS → `sudo -iu hunt git pull` → `sudo systemctl restart hunt`.
  NO scp, NO ad-hoc `/tmp/*.py` scripts — every diagnostic/operator tool lives in the repo (`audit/`, `tools/`)
  and travels by git pull. (2026-09-10: 53 stray `/tmp` scripts archived to `/tmp/hunt_archive_20260910/`.)
- **Env parity**: `./deploy/sync_env_local.sh` pulls AWS `.env` (all API keys + wallet key) to local `.env`
  over the EICE tunnel and FORCES `HUNT_DRY_RUN=true` locally. Mac = dev/paper-only by construction;
  live trading happens only on AWS. `.env` is gitignored on both sides — never commit it.
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
- **Mac dev limits (2026-09-10, verified)**: this Mac's network NXDOMAINs `*.pump.fun`
  (safety-net poll + in-memory-coin enrichment fail locally); PumpPortal WS, Helius RPC,
  and DexScreener (with UA header) work. Local runs prove boot/gate/exit/shutdown + instruction
  builders — AWS remains the full-fidelity runner.

## Architecture (key modules)
- `hunt/paper/run.py` — the campaign engine: discovery queue, gate chain, entry pricing, open/exit wiring
- `hunt/exec/live.py` — REAL execution (dry_run=false only): `LiveExecutor` signs/sends curve + AMM fills, parses ACTUAL token/SOL deltas from confirmed tx post-balances, CLI `balance`/`smoke`
- `hunt/exec/pumpfun/` — VENDORED pumpfun-python (MIT, unsigned instructions only, keys never enter it) — the exact PumpFun curve v2 buy/sell bytes incl. the sell-account SWAP quirk
- `hunt/watch/price_feed.py` — real-time bonding-curve pricing (Helius `accountSubscribe`, one shared WS)
- `hunt/watch/discovery_ws.py` — PumpPortal `subscribeNewToken` stream (sub-second discovery, free)
- `hunt/paper/run.py::survival_filter` — gate chain (dust floor/ceiling, socials, model, intel, dev reputation)
- `hunt/paper/run.py::_process_exit` — exit ladder (see strategy below)
- `hunt/notify/` — Telegram alerts, control bot (`/status /positions /pnl /pause /kill`), daily digest
- `deploy/` — systemd units, AWS setup scripts, DEPLOY.md
- `tools/liquidate.py` — emergency sell-everything (AWS-only, needs `--confirm`)
- `deploy/sync_env_local.sh` — pull AWS `.env` to Mac (forces paper mode)
- `audit/top_runners.py` — daily "did we miss runners" audit (run on AWS: needs network + live decisions DB)
- DB: SQLite at `hunt/data/hunt.sqlite3` — every decision stores intel (top10/holders/snipers/dev_pct/burst)

## LIVE mode (HUNT_DRY_RUN=false)
- **One master switch**: `HUNT_DRY_RUN=true` = pure paper (default). `false` = the SAME
  engine in `hunt/paper/run.py` executes REAL buys/sells through `hunt/exec/live.py`.
  No other config flips trading. Never set live on Mac or unplanned.
- **Preflight (fail-closed)**: live start requires `HUNT_WALLET_PRIVATE_KEY` and
  `balance >= live_min_balance_sol` (0.2 SOL) — else the service exits WITHOUT trading.
- **Venue routing**: buy/sell try the PumpFun bonding curve first (species-A live),
  fall back to the AMM via Jupiter when the curve is complete/graduated (species-B AND
  any species-A that graduates mid-hold — mint never changes, balances carry over 1:1).
- **Real fills, real PnL**: every LIVE entry/exit parses ACTUAL token/SOL deltas from
  the confirmed tx (`_parse_fill`); the SQLite `positions` row stores mode=LIVE, actual
  tokens (`decimals` column) and size (actual SOL spent). Paper fee-sim is NOT used live.
- **Partial-sell rule**: SPL CloseAccount reverts the whole tx on a non-empty ATA, so tiered
  scale-out slices are sent WITHOUT the ATA close; only full-remainder sells (SL/breakeven/
  moon_bag_trail/force_close) include it (`close_ata=` in `LiveExecutor.sell`).
- **Live sell failure = position KEPT open** (never phantom-closed) + Telegram alert; retries
  next tick.
- **Emergency kill**: `touch hunt/data/kill_live` on AWS → next stops-poll force-closes ALL
  live positions and deletes the file. Restart to resume.
- **Guardrails**: per-open balance check `size + live_min_balance_sol`, daily-loss cap
  `live_daily_loss_cap_sol` (0.5 SOL/UTC day → auto force-close ALL live + halt new opens),
  kill-file emergency close (no Telegram `/live` — the deployed service has no control bot).
- **Deploy**: update `.env` on AWS (`deploy/gen_env.sh` pulls `HUNT_WALLET_PRIVATE_KEY` from
  SSM `/hunt/HUNT_WALLET_PRIVATE_KEY`) → `systemctl restart hunt`. Funding wallet is generated
  ON the instance (`python -m hunt.utils.solana gen-wallet`), never on Mac; the address is
  given to the operator to fund ~1–2 SOL. The Mac holds a SYNCED COPY of the key
  (`./deploy/sync_env_local.sh`) for parity only — local runs are paper-locked
  (`HUNT_DRY_RUN=true` forced by the sync script), so the key can never trade from Mac.
- **Emergency liquidation**: `sudo -u hunt .venv/bin/python tools/liquidate.py --confirm` on AWS
  sells EVERY nonzero token bag to SOL (full remainders, `close_ata=True`); without
  `--confirm` it only lists bags. Never scp one-off sell scripts.

## Strategy (current, frozen until evidence says otherwise)
- **Discovery**: PumpPortal stream → 90s waitlist (coins are born ~28 SOL mcap; judge at 90s with live mcap)
- **Gates**: dust floor ≥50 SOL · ceiling DISABLED 2026-09-08 (was ≤3000) · top10>75% veto · snipers≥2 veto · socials + survival model · SolanaTracker risk · dev reputation (serial_rugger veto)
- **Exits**: SL −20% pre-tier · bank 50% @ +40% · 25% @ +60% · breakeven floor between tiers · moon bag (25%) laddered trail: 30% <3x → 20% @3x → 12% @10x → 8% @50x · max_hold 6h (24h for bags)
- **Pricing**: Helius curve ticks (exact, ~0–2% vs pump.fun indexer); GeckoTerminal fallback for graduated tokens
- **Species-B REOPENED 2026-09-08 (decision)**: ceiling gate commented (`run.py`)
  so USUR-class moonshots are huntable again. Evidence on both sides: (1) the
  post-grad tail is REAL (USUR: 151,926 SOL @90s, +50.31 SOL in 9 min; specb
  audit 27/40 continued, 0/40 underwater; **ZDOG 09-09: +4.68 SOL moon_bag_trail
  from a 13.5k SOL entry**); (2) the class median is a dump-factory (6 full-stake
  SLs 09-05, ex-USUR avg −0.0156/slot) and the birth→listing ramp is atomic
  (ROBIN replay) so entries are at the post-grad plateau.
- **SL-death measurement IN (2026-09-09, `audit/specb_sl_death.py`)**: population =
  all 802 species-B mints we saw in 48h; sniper/top10 gates still veto 729 (91%),
  so only **73 are playable**. Of those 73: **dusted (fold<0.2) = 31.5%** ← the
  AGENTS number, plus 2.7% faded → 34% losing entries; 47.9% flat; **17.8%
  continued** (fold≥1.2, max 3.90x current). Median fold 0.99. Caveat: current
  mcap is a lower bound — peak-then-dump AND intraday-tier-before-recovery are
  both invisible, so 31.5% is a FLOOR on true SL-death and tails like ZDOG's
  realized +93x don't show in fold buckets. VERDICT: reopen STANDS with eyes
  open — burn rate ~1/3 of entries, funded by rare heavy tails; ZDOG already
  funds ~19 SL-deaths. Not frozen; keep monitoring, next refinement = peak-aware
  (replay) sim only if burn rate rises.

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
  `cd /home/hunt/hunt && sudo -u hunt .venv/bin/python audit/top_runners.py 24`
- Methodology matters: a "missed moonshot" = seen at birth and dust-rejected. Giants first-seen above the
  ceiling are not misses. Zero gate-misses is the KPI (verified across 100+ runners so far).
- `specb_mcap_audit.py [N] [hours] [min_x]` — specimen-B "real picture" monitor. Target list = pump.fun's
  OWN ranked gainers (NOT the decisions DB — the DB only shows what we saw; the gainer list is the
  population we'd actually enter). For each target: `fold = current SOL mcap / DB 90s snapshot` (the
  listing/entry point). Sorted by fold → buckets: <0.2 dusted · 0.2–0.8 faded · 0.8–1.2 flat · >1.2
  continued · ≥5 ran away. Deliberately mcap-only (no on-chain replay; ~0 extra RPC — current mcap
  already comes with the /coins fetch). Run on AWS:
  `cd /home/hunt/hunt && sudo -u hunt .venv/bin/python audit/specb_mcap_audit.py 40 24 10`
- Caveat (by design): gainer-ranked = survivor-biased — dusted coins fall off rankings, so the SL-death
  class is INVISIBLE to this script; it sizes the upside, not the downside. Current mcap is also a lower
  bound (peak-then-dump missed).

## Monitoring
- ZCode automation every 2h: AWS health + funnel + PnL via SSH; Mac standby check; auto-fix clear bugs.
- 08:00 daily: full digest → Telegram (server-side timer also sends independently).
- Telegram: real-time alerts (open/TP/SL/close) + control commands.
- If SSH/monitoring fails with AWS auth errors: the bot runs fine on systemd — just note it.

## Current campaign state (update as things change)
- ⛔ HALTED 2026-09-10: `hunt.service` stopped + disabled, `kill_live` in place, 0 open
  positions, wallet flat at 0.401027 SOL. Net live loss vs $49 top-up ≈ 0.089 SOL (~$8.90).
  Do NOT restart without an explicit operator order.
- 7-day paper campaign on AWS, started Sep 6 ~23:59 local. Baseline: 4h clean window +50 SOL (USUR 1431x).
- Two runner species: A = classic curve rides (our edge), B = instant-mega launches (REOPENED 2026-09-08:
  ceiling commented to chase USUR-class tails again — see species-B decision above; `specb_mcap_audit.py`
  kept as monitor; next step = survival-agnostic SL-death measurement).
