#!/bin/bash
# Generate /home/hunt/hunt/.env from SSM Parameter Store.
# Runs AS the hunt user (instance role provides AWS credentials).
cd /home/hunt/hunt
for k in HUNT_HELIUS_API_KEY HUNT_BIRDEYE_API_KEY HUNT_TELEGRAM_BOT_TOKEN HUNT_TELEGRAM_CHAT_ID HUNT_SOLANATRACKER_API_KEY HUNT_GMGN_API_KEY HUNT_WALLET_PRIVATE_KEY; do
  V=$(.venv/bin/aws ssm get-parameter --name /hunt/$k --with-decryption --query Parameter.Value --output text)
  echo "$k=$V"
done
echo 'HUNT_DRY_RUN=true'
echo 'HUNT_TRADE_SIZE_SOL=0.05'
echo 'HUNT_MAX_TRACKED_WALLETS=8'
echo 'HUNT_MAX_OPEN_POSITIONS=8'
echo 'HUNT_SLIPPAGE_BPS=2000'
echo 'HUNT_PRIORITY_FEE_MAX_LAMPORTS=1000000'
