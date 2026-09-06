from __future__ import annotations
import httpx
from loguru import logger
from hunt.config import get_settings
from hunt.utils.ratelimit import RateLimiter

_tracker_bucket = RateLimiter(2.5, 5)  # 3 rps free, stay under

async def check_risk(mint: str, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Return (pass, reason). Uses SolanaTracker risk 1-10. True if safe."""
    s = get_settings()
    key = s.solanatracker_api_key
    if not key:
        return True, "no_key"
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=10)
        close_client = True
    try:
        await _tracker_bucket.acquire()
        r = await client.get(
            f"https://data.solanatracker.io/tokens/{mint}",
            headers={"x-api-key": key},
            timeout=10,
        )
        if r.status_code != 200:
            logger.debug("tracker {} status {}", mint[:8], r.status_code)
            return True, f"tracker_{r.status_code}"
        data = r.json() or {}
        risk = data.get("risk") or {}
        score = risk.get("score", 0) or 0
        rugged = risk.get("rugged", False)
        if rugged:
            return False, "rugged"
        if score >= 7:
            return False, f"risk_{score}"
        # extra sniper/bundler gates like zostaff's top5≥80% / dev≥25% hard veto
        snipers_pct = (risk.get("snipers") or {}).get("totalPercentage", 0) or 0
        if snipers_pct > 20:
            return False, f"snipers_{snipers_pct:.1f}%"
        bundlers_pct = (risk.get("bundlers") or {}).get("totalPercentage", 0) or 0
        if bundlers_pct > 15:
            return False, f"bundlers_{bundlers_pct:.1f}%"
        dev_pct = (risk.get("dev") or {}).get("percentage", 0) or 0
        if dev_pct >= 25:
            return False, f"dev_{dev_pct:.1f}%"
        top10 = risk.get("top10", 0) or 0
        if top10 >= 80:
            return False, f"top10_{top10:.1f}%"
        return True, f"risk_{score}"
    except Exception as e:
        logger.debug("tracker check fail {}: {}", mint[:8], e)
        return True, "tracker_error"
    finally:
        if close_client:
            await client.aclose()
