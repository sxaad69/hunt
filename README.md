# hunt — Autonomous Solana Copy-Trading Bot (Python)

Discovers profitable Solana wallets on its own, scores them by realized PnL / win rate,
tracks them in real time, and mirrors their trades (paper mode first, live optional).
Zero manual wallet picking. Runs on free-tier APIs only.

```
scout ──▶ candidate pool ──▶ scorer ──▶ leaderboard ──▶ selector (auto promote/drop)
                                                              │
        Helius WS watcher ◀──── tracked wallets ◀─────────────┘
              │ buy/sell signals
              ▼
   risk filters ──▶ executor (Jupiter) ──▶ SQLite ledger + Telegram alerts
```

## Quick start

```bash
cd hunt
python3 -m venv .venv                 # venv is the way
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                  # then fill in keys (below)
python -m hunt                        # run
```

Run tests:

```bash
pytest tests/ -q
```

## Getting the free keys (one-time, ~10 minutes)

### 1. Helius (required — streaming + history)
1. Open https://dashboard.helius.dev
2. Sign up (free plan = 1M credits/month)
3. Left menu → **API Keys** → **New API Key** → copy
4. Put into `.env`: `HUNT_HELIUS_API_KEY=...`

### 2. Birdeye (required — top-trader extraction)
1. Open https://bds.birdeye.so (or https://birdeye.so → Developers)
2. Sign up free tier → create an API key
3. Put into `.env`: `HUNT_BIRDEYE_API_KEY=...`

### 3. Telegram (required — alerts + remote control)
1. In Telegram, message **@BotFather** → send `/newbot` → follow prompts → copy the token
2. `HUNT_TELEGRAM_BOT_TOKEN=...` in `.env`
3. Send any message to your new bot, then open:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Find `"chat":{"id":123456789}` → `HUNT_TELEGRAM_CHAT_ID=123456789` in `.env`

### 4. Jupiter API key (optional — higher rate limits)
- https://dev.jup.ag → free key → `HUNT_JUP_API_KEY=...`
- Works without a key too (lower rate limit).

## Going live (only after paper trading proves out)

```bash
python -m hunt.utils.solana gen-wallet     # dedicated bot wallet ONLY
# put private key into .env as HUNT_WALLET_PRIVATE_KEY
# fund the address with a small amount of SOL
# set HUNT_DRY_RUN=false
```

Never use your main wallet. Never commit `.env`.

## Telegram commands

| Command | Action |
|---|---|
| `/status` | engine state, open positions, PnL |
| `/positions` | open positions |
| `/pnl` | today + all-time |
| `/wallets` | tracked wallets + scores |
| `/pause` | halt new buys |
| `/resume` | resume (clears pause + kill switch) |
| `/kill` | KILL SWITCH — halt all buys |

## How selection works

Scout finds tokens that pumped or just launched (DexScreener, free). For each hot token it
pulls top traders (Birdeye) and early buyers (pump.fun). Every candidate's last ~300 swaps are
replayed on-chain and scored:

- win rate ≥ 40% · ≥ 30 closed trades · ≥ 5 distinct tokens
- realized PnL ≥ threshold (computed from actual SOL in/out — no price oracle needed)
- instant-sell ratio ≤ 15% (filters copier-farmer wallets)

Top scoring wallets get tracked automatically (max `HUNT_MAX_TRACKED_WALLETS`). Tracked wallets
whose rolling stats decay get demoted automatically. When a tracked wallet buys, the bot copies
(fixed size); when it sells, the bot sells the same fraction. Positions also have independent
TP (+100%), SL (−30%), trailing stop (20%) and max-hold (48h).

All thresholds are tunable via `.env` (`HUNT_*` vars, see `hunt/config.py`).

## Deploying to a VPS later

Same codebase. On Ubuntu:

```bash
sudo apt install python3-venv
git clone <your-repo> && cd hunt
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill keys
```

Run persistently with systemd:

```ini
# /etc/systemd/system/hunt.service
[Unit]
Description=hunt solana copy trader
After=network-online.target

[Service]
WorkingDirectory=/opt/hunt
ExecStart=/opt/hunt/.venv/bin/python -m hunt
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now hunt
journalctl -u hunt -f
```

## Honest risk notes

You always enter after the whale (worse fills). Expect ~30–40% of copied trades to lose.
Memecoins can go to zero before a stop-loss fills. Unofficial endpoints (pump.fun frontend)
may break — the bot degrades gracefully but discovery narrows. Start small. The kill switch
exists for a reason.
