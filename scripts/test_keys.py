import asyncio
import sys

import httpx

from hunt.config import WSOL, get_settings


async def test_helius(client: httpx.AsyncClient) -> bool:
    s = get_settings()
    r = await client.post(
        s.rpc_http,
        json={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": [{"commitment": "finalized"}]},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  FAIL http {r.status_code}: {r.text[:120]}")
        return False
    slot = r.json().get("result")
    print(f"  OK   current slot: {slot}")
    return slot is not None


async def test_helius_ws() -> bool:
    import json as _json

    import websockets

    s = get_settings()
    try:
        async with websockets.connect(s.rpc_ws, open_timeout=10) as ws:
            await ws.send(_json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "logsSubscribe",
                "params": [{"mentions": [WSOL]}, {"commitment": "processed"}],
            }))
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = _json.loads(raw)
            if isinstance(msg.get("result"), int):
                print(f"  OK   websocket connected + logsSubscribe accepted (sub {msg['result']})")
                return True
            err = (msg.get("error") or {}).get("message", str(msg)[:120])
            print(f"  FAIL {err}")
            return False
    except Exception as e:
        print(f"  FAIL {e}")
        return False


async def test_birdeye(client: httpx.AsyncClient) -> bool:
    s = get_settings()
    r = await client.get(
        "https://public-api.birdeye.so/defi/price",
        params={"address": WSOL},
        headers={"X-API-KEY": s.birdeye_api_key, "x-chain": "solana"},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  FAIL http {r.status_code}: {r.text[:120]}")
        return False
    price = (r.json().get("data") or {}).get("value")
    print(f"  OK   SOL price: ${price}" if price else f"  WARN unexpected body: {r.text[:120]}")
    return price is not None


async def test_top_traders(client: httpx.AsyncClient) -> bool:
    s = get_settings()
    mint = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    headers = {"X-API-KEY": s.birdeye_api_key, "x-chain": "solana"}
    params = {"address": mint, "time_frame": "24h", "offset": 0, "limit": 5}
    for attempt in range(4):
        r = await client.get(
            "https://public-api.birdeye.so/defi/v2/tokens/top_traders",
            params=params, headers=headers, timeout=15,
        )
        if r.status_code == 200:
            items = ((r.json().get("data") or {}).get("items")) or []
            print(f"  OK   top_traders returned {len(items)} wallets for WIF")
            return len(items) > 0
        if r.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"  … rate limited (429), retrying in {wait}s")
            await asyncio.sleep(wait)
            continue
        print(f"  FAIL http {r.status_code}: {r.text[:150]}")
        return False
    print("  FAIL kept hitting 429 — free tier per-second limit is tight; the bot's")
    print("       built-in rate limiter (0.8 req/s + daily CU budget) handles this in production.")
    return False


async def test_telegram(client: httpx.AsyncClient) -> bool:
    s = get_settings()
    r = await client.get(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe", timeout=15)
    if r.status_code != 200:
        print(f"  FAIL getMe: {r.text[:120]}")
        return False
    name = r.json().get("result", {}).get("username")
    r2 = await client.get(
        f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
        params={"chat_id": s.telegram_chat_id, "text": "✅ hunt bot connectivity test — you're all set."},
        timeout=15,
    )
    if r2.status_code != 200 or not r2.json().get("ok"):
        print(f"  FAIL sendMessage: {r2.text[:200]}")
        print("  -> did you message the bot once so it can DM you?")
        return False
    print(f"  OK   bot @{name} sent you a test DM")
    return True


async def main() -> int:
    results = {}
    async with httpx.AsyncClient() as client:
        results["helius rpc"] = await test_helius(client)
        results["helius ws"] = await test_helius_ws()
        results["birdeye price"] = await test_birdeye(client)
        results["birdeye top_traders"] = await test_top_traders(client)
        results["telegram"] = await test_telegram(client)

    print()
    failed = [k for k, v in results.items() if not v]
    for k, v in results.items():
        print(("PASS " if v else "FAIL ") + k)
    if failed:
        print("\nfix the failures above; everything else is ready.")
        return 1
    print("\nall systems go — run: python -m hunt")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
