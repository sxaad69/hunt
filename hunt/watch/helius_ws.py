from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Optional

import websockets
from loguru import logger


class HeliusTxStream:
    def __init__(self, ws_url: str, http_url: str, wallets: list[str]) -> None:
        self.ws_url = ws_url
        self.http_url = http_url
        self.wallets = list(wallets)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._sub_id: Optional[int] = None
        self._id_gen = itertools.count(1)
        self._reload_flag = asyncio.Event()
        self.mode: str = "auto"
        self._seen_sigs: set[str] = set()
        import httpx

        self.http = httpx.AsyncClient()

    def request_reload(self) -> None:
        self._reload_flag.set()

    async def run(self) -> None:
        backoff = 2
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    backoff = 2
                    await self._subscribe(ws)
                    recv_task = asyncio.create_task(ws.recv())
                    reload_task = asyncio.create_task(self._reload_flag.wait())
                    while True:
                        done, pending = await asyncio.wait(
                            {recv_task, reload_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if recv_task in done:
                            msg = recv_task.result()
                            await self._handle_message(msg)
                            recv_task = asyncio.create_task(ws.recv())
                        if reload_task in done:
                            self._reload_flag.clear()
                            await self._resubscribe(ws)
                            recv_task.cancel()
                            recv_task = asyncio.create_task(ws.recv())
                            reload_task = asyncio.create_task(self._reload_flag.wait())
            except Exception as e:
                logger.warning("ws down: {} — reconnecting in {}s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _subscribe(self, ws) -> None:
        if not self.wallets:
            return
        if self.mode in ("auto", "tx"):
            req = {
                "jsonrpc": "2.0",
                "id": next(self._id_gen),
                "method": "transactionSubscribe",
                "params": [
                    {"accountInclude": self.wallets, "failed": False},
                    {
                        "commitment": "processed",
                        "encoding": "jsonParsed",
                        "transactionDetails": "full",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            }
            await ws.send(json.dumps(req))
            logger.info("subscribed (transactionSubscribe) to {} wallet(s)", len(self.wallets))
        else:
            req = {
                "jsonrpc": "2.0",
                "id": next(self._id_gen),
                "method": "logsSubscribe",
                "params": [{"mentions": self.wallets}, {"commitment": "processed"}],
            }
            await ws.send(json.dumps(req))
            logger.info("subscribed (logsSubscribe/free-mode) to {} wallet(s)", len(self.wallets))

    async def _unsubscribe(self, ws) -> None:
        if self._sub_id is None:
            return
        req = {
            "jsonrpc": "2.0",
            "id": next(self._id_gen),
            "method": "transactionUnsubscribe" if self.mode == "tx" else "logsUnsubscribe",
            "params": [self._sub_id],
        }
        try:
            await ws.send(json.dumps(req))
        except Exception:
            pass
        self._sub_id = None

    async def _resubscribe(self, ws) -> None:
        await self._unsubscribe(ws)
        await self._subscribe(ws)

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        if isinstance(msg.get("result"), int) and "id" in msg:
            self._sub_id = msg["result"]
            return

        error = msg.get("error")
        if error:
            err_text = str(error.get("message", ""))
            if self.mode == "auto" and ("not available" in err_text or "-32601" in err_text):
                logger.info("transactionSubscribe unavailable → falling back to logsSubscribe")
                self.mode = "logs"
                return
            logger.debug("ws rpc error: {}", err_text[:150])
            return

        notification = msg.get("params")
        if not notification:
            return
        result = notification.get("result") or {}

        if self.mode in ("auto", "tx"):
            payload = result
            meta = ((payload.get("transaction") or {}).get("meta")) or {}
            if meta.get("err"):
                return
            self._push(payload)
        else:
            value = result.get("value") or {}
            sig = value.get("signature")
            if not sig or value.get("err"):
                return
            if sig in self._seen_sigs:
                return
            self._seen_sigs.add(sig)
            if len(self._seen_sigs) > 5000:
                self._seen_sigs.clear()
            payload = await self._fetch_tx(sig)
            if payload:
                self._push(payload)

    def _push(self, payload: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("signal queue full; dropping tx")

    async def _fetch_tx(self, signature: str) -> Optional[dict[str, Any]]:
        from hunt.utils.http import post_json_rpc

        for attempt in range(3):
            resp = await post_json_rpc(
                self.http,
                self.http_url,
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                rpc_id=next(self._id_gen),
            )
            if resp and resp.get("result"):
                r = resp["result"]
                return {
                    "signature": signature,
                    "slot": r.get("slot"),
                    "transaction": {
                        "transaction": r.get("transaction"),
                        "meta": r.get("meta"),
                    },
                }
            await asyncio.sleep(1.5 * (attempt + 1))
        return None
