# AGENTS.md — hunt project notes for AI agents

Memecoin paper-trading bot ("moonshot hunter") for pump.fun. Python/asyncio.
Primary runtime: **Linode** (replaces AWS). Mac = dev machine + paper-only.
**Paper default (`HUNT_DRY_RUN=true`); live = `HUNT_DRY_RUN=false` + funded wallet key in `.env`. Never commit secrets.**
**No live trading** until feeds/pricing are fixed and a Linode host exists. Operator order 2026-09-11.

## Golden rules
- **Single instance**: only ONE hunt process runs the campaign at a time. Mac is standby.
- **Paper default**: `HUNT_DRY_RUN=true` — pure paper (default, always). **Live** = `HUNT_DRY_RUN=false` + `HUNT_WALLET_PRIVATE_KEY` in `.env` on the Linode host. Live starts fail-closed: preflight requires the wallet key and `balance >= live_min_balance_sol` (0.2), else the service exits WITHOUT trading. Flips to live only via `.env` on Linode — never an unplanned default. Always verify what mode a process started in before hardening decisions.
- **Deploy flow (git ONLY)**: edit/test on Mac → `git push` → SSH to Linode → `git pull` → restart hunt.
  NO scp, NO ad-hoc `/tmp/*.py` scripts — every diagnostic/operator tool lives in the repo (`audit/`, `tools/`)
  and travels by git pull.
- **Env parity**: Mac `.env` is paper-locked (`HUNT_DRY_RUN=true`). Live trading happens only on Linode once a host exists and feeds are fixed. `.env` is gitignored — never commit it. Linode API token and wallet keys never go in this file or git.
- **Heavy files never in git**: DB, logs, state stay on the Linode host (not git). Old AWS S3 (`s3://hunt-state-362457597397-euc1`) is historical only.
- **Strategy changes**: max ONE evidence-backed tweak per day, logged in the daily digest. No blind tuning.
- `.env` holds API keys — gitignored, never printed to logs.

## Where things run
- **Linode (primary, 2026-09-11+)**: account `saadsuri67`. Instance `hunt` id `104905707`, **eu-central** (Frankfurt), type **g6-standard-1** (2GB/1 vCPU — AWS t3.micro was 1GB; 2GB so Python+Helius WS does not OOM). IPv4 `172.105.93.126`. SSH: `ssh -i ~/.ssh/linode_hermes root@172.105.93.126` (then `sudo -iu hunt`). Ubuntu 24.04, user `hunt`, repo `/home/hunt/hunt`, systemd `hunt`, **paper** (`HUNT_DRY_RUN=true`). Fresh DB on host — do not scp Mac sqlite/logs.
- **AWS (DEAD)**: EC2 `i-0d567877feac30c13` **terminated 2026-09-11** (eu-central-1b). Do not SSH, restart, or deploy there. IAM `hunt-deploy` / SSM `/hunt/*` / S3 state are leftover — not the runtime.
- **Mac**: dev + paper-only. `HUNT_DRY_RUN=true` always. VPN may be needed for `*.pump.fun`.
- **Service** (once Linode exists): systemd `hunt` · logs under the repo `logs/hunt_YYYY-MM-DD.log` (UTC)
- **Secrets**: `.env` on the Linode host (gitignored). Linode API token = operator-held, never in git / AGENTS.md / logs.
- **Mac network (2026-09-10, verified; VPN can change this)**: without VPN this Mac NXDOMAINs `*.pump.fun`; PumpPortal WS, Helius RPC, and DexScreener (with UA header) work. Local runs prove boot/gate/exit/shutdown — Linode will be the full-fidelity runner.

## Architecture (key modules)
- `hunt/paper/run.py` — the campaign engine: discovery queue, gate chain, entry pricing, open/exit wiring
- `hunt/exec/live.py` — REAL execution (dry_run=false only): `LiveExecutor` signs/sends curve + AMM fills, parses ACTUAL token/SOL deltas from confirmed tx post-balances, CLI `balance`/`smoke`
- `hunt/exec/pumpfun/` — VENDORED pumpfun-python (MIT, unsigned instructions only, keys never enter it) — the exact PumpFun curve v2 buy/sell bytes incl. the sell-account SWAP quirk
- `hunt/watch/price_feed.py` — real-time bonding-curve pricing (Helius `accountSubscribe`, one shared WS)
- `hunt/watch/discovery_ws.py` — PumpPortal `subscribeNewToken` stream (sub-second discovery, free)
- `hunt/paper/run.py::survival_filter` — gate chain (dust floor/ceiling, socials, model, intel, dev reputation)
- `hunt/paper/run.py::_process_exit` — exit ladder (see strategy below)
- `hunt/notify/` — Telegram alerts, control bot (`/status /positions /pnl /pause /kill`), daily digest
- `deploy/` — systemd units; AWS setup scripts are historical (EC2 gone)
- `tools/liquidate.py` — emergency sell-everything (Linode-only once live, needs `--confirm`)
- `audit/top_runners.py` — daily "did we miss runners" audit (run on the campaign host: needs network + live decisions DB)
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
- **Curve sell echo rule (trial-proven 2026-09-10)**: sell slot14 MUST equal the slot16
  our buy used = derived `bonding_curve_v2` (builder derives it; the old 4Rut3 constant
  was HB2r4H-specific and fails with `InvalidBondingCurveV2`/6074 on any other coin).
  Slot15 = one of the fee program's 8 vaults (any works; must be WRITABLE).
  Router/bot buys pass their own slot16 scheme (e.g. 62mabQu3…) — only OUR buy→sell
  pair must echo.
- **Partial-sell rule**: SPL CloseAccount reverts the whole tx on a non-empty ATA, so tiered
  scale-out slices are sent WITHOUT the ATA close; only full-remainder sells (SL/breakeven/
  moon_bag_trail/force_close) include it (`close_ata=` in `LiveExecutor.sell`).
- **Live sell failure = position KEPT open** (never phantom-closed) + Telegram alert; retries
  next tick.
- **Emergency kill**: `touch hunt/data/kill_live` on Linode → next stops-poll force-closes ALL
  live positions and deletes the file. Restart to resume.
- **Guardrails**: per-open balance check `size + live_min_balance_sol`, daily-loss cap
  `live_daily_loss_cap_sol` (0.5 SOL/UTC day → auto force-close ALL live + halt new opens),
  kill-file emergency close (no Telegram `/live` — the deployed service has no control bot).
- **Deploy**: update `.env` on Linode (wallet key lives only there) → restart hunt.
  Funding wallet is generated ON the instance (`python -m hunt.utils.solana gen-wallet`), never on Mac.
  Mac stays paper-locked (`HUNT_DRY_RUN=true`). Do not arm live until feeds/pricing are fixed.
- **Emergency liquidation**: `tools/liquidate.py --confirm` on Linode
  sells EVERY nonzero token bag to SOL (full remainders, `close_ata=True`); without
  `--confirm` it only lists bags. Never scp one-off sell scripts.

## Strategy (current, frozen until evidence says otherwise)
- **Discovery**: PumpPortal stream → 90s waitlist (coins are born ~28 SOL mcap; judge at 90s with live mcap)
- **Gates**: dust floor ≥50 SOL · ceiling RE-ENABLED 2026-09-11 ≤3000 SOL species-A only (operator order for supervised live; was disabled 09-08 chasing B tails) · top10>75% veto · snipers≥2 veto · socials + survival model · SolanaTracker risk · dev reputation (serial_rugger veto)
- **Exits**: SL −20% pre-tier · bank 50% @ +40% · 25% @ +60% · breakeven floor between tiers · moon bag (25%) laddered trail: 30% <3x → 20% @3x → 12% @10x → 8% @50x · max_hold 6h (24h for bags)
- **Pricing**: Helius WS for BOTH stages — bonding-curve PDA pre-grad, PumpSwap vault ATAs after. Gecko/Jupiter are HTTP last-resort only.
- **Species-B REOPENED 2026-09-08 (decision, historical)**: ceiling gate commented (`run.py`)
  so USUR-class moonshots are huntable again. Evidence on both sides: (1) the
  post-grad tail is REAL (USUR: 151,926 SOL @90s, +50.31 SOL in 9 min; specb
  audit 27/40 continued, 0/40 underwater; **ZDOG 09-09: +4.68 SOL moon_bag_trail
  from a 13.5k SOL entry**); (2) the class median is a dump-factory (6 full-stake
  SLs 09-05, ex-USUR avg −0.0156/slot) and the birth→listing ramp is atomic
  (ROBIN replay) so entries are at the post-grad plateau.
- **Species-B OUT of the LIVE entry universe since 2026-09-11** (operator order):
  the ≤3000 SOL mcap ceiling (species-A only) is back ON in `run.py` for the supervised
  live session, so instant-mega/USUR-class entries are rejected with `mcap_ceiling`.
  The 09-08 reopen decision is NOT reversed permanently — the evidence review stands
  (see below), but for THIS live session only species-A curve-stage rides ≤3000 SOL
  are huntable. `specb_mcap_audit.py` keeps monitoring the class.
- **Paper 2026-09-11 BOTH entry classes**: curve 50–3000 SOL **and** post-grad AMM
  (vault FDV ≥50, no ceiling). Live still frozen. Tracker `risk_7+` still vetoes.
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
- **Single valuation truth (2026-09-11)**: dust/ceiling use on-chain curve FDV
  (`(virtual_sol/1e9)*(supply/virtual_token)`). pump.fun API `market_cap` is log-only
  (`[MCAPRELI]`). Missing curve → `mcap_unavailable`. Graduated/drained → `graduated`.
  Never mix DexScreener USD mcap with SOL gates. API units: SOL for Solana coins, USD for EVM.
- **Single intel truth (2026-09-11)**: top-10% of CIRCULATING supply via
  `getTokenLargestAccounts` minus bonding-curve ATA (`hunt/paper/onchain_intel.py`).
  `advanced-indexer.pump.fun/in-memory-coin` is `_dev` only — its top10/snipers/dev_pct
  zeros were the Rufus false-clean. Missing measurement → `intel_unavailable`. Empty
  holder list → None, never 0. Indexer `sniperCount` is NOT a gate (cannot see snipers
  on-chain). Smart-boost cannot override `intel_unavailable` / `mcap_unavailable` /
  `top10_heavy` / `graduated`.
- **Tracker (2026-09-11)**: sniper gate = `snipers.totalPercentage > 20` (residual
  holdings, not wallet count). Missing tracker/snipers object → `snipers_unavailable`.
  Their 1–10 score is log-only (punishes un-graduated coins). `rugged` still vetoes.
  Concentration = on-chain circulating top10 only.
- Helius `price_usd` uses mint decimals from `getTokenSupply` (not hardcoded 6).
  `mcap_sol` never needed decimals. `SOL_USD=150` last-resort FX is stale.
  DexScreener `$0` on un-graduated coins is expected (no pair). Analysis of a signal
  is at decision-time on-chain, never post-hoc Dex.
- **NO LIVE** until paper soak proves gates match on-chain and operator orders it on Linode.
- Coins are born ~28 SOL ($3K) and often graduate to PumpSwap within minutes (curve zeroes out, price moves to AMM).
- `base_decimals` varies (6 classic; stonkfun uses 9) — verify before price math on unknown programs.
- Safety-net poll must run on its own clock — it starved once during launch bursts and missed launches.
- Jupiter DOES route un-graduated bonding curves (2026-09-11 — see trap below); the old
  "curve needs the pump program" rule is stale for NEW quotes. Curve entries still use the
  pump program for the sell-echo path, but Jupiter-first is the entry route.
- **`advanced-indexer.pump.fun/in-memory-coin` is NOT a gate** (2026-09-11): it returned
  clean `top10=0 / snipers=0 / dev_pct=0` for the first live-window ACCEPTs while on-chain
  circulating top-10 (curve ATA excluded) is the only number we trust. Gates now fail-closed
  (`intel_unavailable`) instead of treating indexer zeros as clean.
- **Live sell must NEVER convert coins into an untracked quote mint mid-path** (2026-09-11, Rufus):
  the coin→quote→SOL chained sell left proceeds parked in the quote token when leg2 reverted
  (`Custom 6024`), then the engine's `raw_bal==0` shortcut closed the position at a fabricated full
  stop-loss while the value was still in the wallet. FIXED: `_sell_amm` now sells directly coin→WSOL
  in adaptive slices, verifying each slice's real SOL inflow before reporting; `sell_now` also refuses
  to close a position whose whole-balance exit left coins behind (partial slice). Keep the invariant:
  a LIVE position closes ONLY when verified SOL arrived and the coin bag is truly flat.
- **Jupiter (the AMM router) DOES route un-graduated bonding curves now (2026-09-11)** — the old
  "curve needs the pump program" rule is stale for NEW quotes: SOL→coin quotes exist for every
  quote-mint family (WSOL/USDC/exotic) and the router emits the 26-account quote-aware pump CPI
  (buy disc `5df6823ce7e940b2`, 24-byte data). BUT full-bag coin→SOL sell routes often FAIL at the
  same sizes that buy routes pass — sell sizes must be sliced until the route fits the 1232-byte
  wire cap with all ATAs pre-created. Combined with the safety-net trap above: Jupiter
  routes curves now, the platform just caps tx size.

## Audit tooling (`audit/`)
- `top_runners.py [hours]` — ranks today's ≥10x runners and classifies our response per coin:
  POSITION taken / seen+REJECTED (with reason) / never seen. Run on the campaign host (network + live DB):
  `.venv/bin/python audit/top_runners.py 24`
- Methodology matters: a "missed moonshot" = seen at birth and dust-rejected. Giants first-seen above the
  ceiling are not misses. Zero gate-misses is the KPI (verified across 100+ runners so far).
- `specb_mcap_audit.py [N] [hours] [min_x]` — specimen-B "real picture" monitor. Target list = pump.fun's
  OWN ranked gainers (NOT the decisions DB — the DB only shows what we saw; the gainer list is the
  population we'd actually enter). For each target: `fold = current SOL mcap / DB 90s snapshot` (the
  listing/entry point). Sorted by fold → buckets: <0.2 dusted · 0.2–0.8 faded · 0.8–1.2 flat · >1.2
  continued · ≥5 ran away. Deliberately mcap-only (no on-chain replay; ~0 extra RPC — current mcap
  already comes with the /coins fetch). Run on the campaign host:
  `.venv/bin/python audit/specb_mcap_audit.py 40 24 10`
- Caveat (by design): gainer-ranked = survivor-biased — dusted coins fall off rankings, so the SL-death
  class is INVISIBLE to this script; it sizes the upside, not the downside. Current mcap is also a lower
  bound (peak-then-dump missed).

## Monitoring
- Campaign host = Linode (once created). Mac = standby / paper.
- 08:00 daily: full digest → Telegram (server-side timer also sends independently).
- Telegram: real-time alerts (open/TP/SL/close) + control commands.

## Current campaign state (update as things change)
- ⛔ **AWS GONE 2026-09-11**: EC2 `i-0d567877feac30c13` terminated. Runtime moves to **Linode** (no instance yet).
- ⛔ **NO LIVE TRADING** until feeds/pricing/intel are fixed once and for all, then operator order on a Linode host. Mac stays `HUNT_DRY_RUN=true`.
- 📄 **This paper run (Mac, 2026-09-11)**: KPI = which ACCEPTs fire under on-chain mcap+top10 + Helius marks. So far 0 ACCEPT — coins that clear those gates die on SolanaTracker `risk_7+`. Log: `/tmp/hunt_paper_qa.log`.
- ⛔ PAUSED 2026-09-11 (last AWS live session, historical): operator-paused after validation: all 6
  species-A ACCEPTs in the 23:16 window (Rufus/Karen/RKC/GROK/CRISPE/TEDDY) were false-clean
  dumps (see intel trap above). Wallet was flat 0.3243 SOL, 0 open, 0 bags. First live entry
  (Rufus, ven=amm, buy filled $5.58e-06/0.0545 SOL) proved the Jupiter-first buy fix;
  its sell failed 12× then phantom-closed (now fixed — sell trap above).
- ✅ SELL FIX DEPLOYED 2026-09-11 (`f2db50e`): `_sell_amm` = adaptive verified coin→WSOL
  slice sell (halve until Jupiter routes a ≤1232-byte tx; count a slice ONLY if SOL balance
  rose AND coins fell — never park value in quote, never phantom-close); `sell_now` refuses
  to close a whole-balance exit that leaves coins behind. Tested: 25/25 local.
- ✅ HARDENED 2026-09-10 (local, ready-to-test, NOT deployed): tier counts LIVE
  realized PnL · DexScreener last-resort exit for blind graduates (Apple −98% class) ·
  loss-cap guard screams on failure · buy/sell within-tick retry + fill-gap alert ·
  single-instance lock + live_armed gate + Telegram control (/status /positions /pnl
  /pause /resume /close_all /kill) + status --reconcile + regression tests.
  NOTE: the deployed live service has NO control bot — Telegram commands are dev-side only.
- ✅ TRIAL-PROVEN 2026-09-10 (`tools/trial_roundtrip.py`, mmrich, 0.002 SOL):
  curve BUY (venue=curve) + curve SELL (venue=curve) round-trip executed with real fills,
  flat after. Sell path needed two fixes (see echo rule below); trial net ≈ −0.0005 SOL.
- 7-day paper campaign (was on AWS), started Sep 6 ~23:59 local. Baseline: 4h clean window +50 SOL (USUR 1431x).
- Two runner species: A = classic curve rides (our edge; the ONLY class in the live entry
  universe this session, ≤3000 SOL by operator order) · B = instant-mega launches (REOPENED
  2026-09-08, then OUT of live entries 2026-09-11 via the re-enabled mcap ceiling — see
  species-B section above; `specb_mcap_audit.py` kept as monitor; next step = survival-agnostic
  SL-death measurement).
