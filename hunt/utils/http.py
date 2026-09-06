from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx
from loguru import logger


async def fetch_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: Any | None = None,
    headers: dict | None = None,
    retries: int = 3,
    backoff: float = 1.5,
) -> Optional[Any]:
    for attempt in range(1, retries + 1):
        try:
            resp = await client.request(
                method, url, params=params, json=json_body, headers=headers, timeout=20
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(f"status {resp.status_code}", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt == retries:
                logger.debug("fetch failed {} {}: {}", method, url, e)
                return None
            await asyncio.sleep(backoff * attempt)


async def post_json_rpc(
    client: httpx.AsyncClient, url: str, rpc_method: str, params: Any, rpc_id: int | str = 1
) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": rpc_method, "params": params}
    return await fetch_json(client, "POST", url, json_body=payload)
