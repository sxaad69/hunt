from __future__ import annotations
import httpx
from loguru import logger
from hunt.config import get_settings
from hunt.utils.ratelimit import RateLimiter

_tracker_bucket = RateLimiter(2.5, 5)  # 3 rps free, stay under

def verdict_from_risk(risk: dict | None) -> tuple[bool, str]:
    """Sniper gate = residual % still held. Score 1-10 is log-only.

    No snipers object → fail-closed (missing feed does not drop the gate).
    snipers.count is NOT a veto — first-block bots are normal on pump.fun.
    """
    if not isinstance(risk, dict):
        return False, "snipers_unavailable"
    if risk.get("rugged"):
        return False, "rugged"
    sn = risk.get("snipers")
    if not isinstance(sn, dict) or "totalPercentage" not in sn:
        return False, "snipers_unavailable"
    try:
        pct = float(sn["totalPercentage"])
    except (TypeError, ValueError):
        return False, "snipers_unavailable"
    if pct > 20:
        return False, f"snipers_{pct:.1f}%"
    try:
        score = int(risk.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return True, f"risk_{score}"


async def check_risk(mint: str, client: httpx.AsyncClient | None = None) -> tuple[bool, str, dict | None]:
    """Snipers fail-closed. Returns (ok, reason, risk_dict_or_None)."""
    s = get_settings()
    key = s.solanatracker_api_key
    if not key:
        return False, "snipers_unavailable", None
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
            return False, "snipers_unavailable", None
        risk = (r.json() or {}).get("risk")
        ok, reason = verdict_from_risk(risk)
        return ok, reason, risk if isinstance(risk, dict) else None
    except Exception as e:
        logger.debug("tracker check fail {}: {}", mint[:8], e)
        return False, "snipers_unavailable", None
    finally:
        if close_client:
            await client.aclose()
