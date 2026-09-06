from __future__ import annotations
import asyncio, json
from loguru import logger

async def health_server(port: int = 8080):
    if port <= 0: return
    async def handler(reader, writer):
        try:
            data = await reader.read(1024)
            req = data.decode(errors="ignore")
            if "GET /healthz" in req:
                body = json.dumps({"status": "ok"})
                resp = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(resp.encode())
            elif "GET /metrics" in req:
                # minimal prometheus
                body = "# HELP hunt_up 1 if up\n# TYPE hunt_up gauge\nhunt_up 1\n"
                resp = f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(resp.encode())
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except Exception as e:
            logger.debug("health handler error {}", e)
        finally:
            writer.close()
    try:
        server = await asyncio.start_server(handler, "127.0.0.1", port)
        logger.info("health server on 127.0.0.1:{}", port)
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.warning("health server failed {}", e)
