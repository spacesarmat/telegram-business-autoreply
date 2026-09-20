from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_STATUS_HTML = """<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Telegram Business AutoReply</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:720px;margin:48px auto;padding:0 20px;background:#111827;color:#f9fafb}
    .card{background:#1f2937;border-radius:18px;padding:28px;box-shadow:0 12px 35px rgba(0,0,0,.25)}
    .ok{color:#34d399;font-weight:700} code{background:#374151;padding:3px 7px;border-radius:6px}
  </style>
</head>
<body><div class=\"card\">
<h1>Telegram Business AutoReply</h1>
<p class=\"ok\">● Контейнер запущен</p>
<p>Управление автоответчиком выполняется в Telegram через команду <code>/admin</code>.</p>
<p>Проверка контейнера: <code>/health</code></p>
</div></body></html>"""


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(4096), timeout=5)
        first_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="ignore")
        parts = first_line.split()
        path = parts[1] if len(parts) >= 2 else "/"

        if path == "/health":
            body = b"ok\n"
            content_type = "text/plain; charset=utf-8"
        else:
            body = _STATUS_HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"

        header = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-store\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(header + body)
        await writer.drain()
    except Exception:
        logger.debug("Status HTTP request failed", exc_info=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_status_server(port: int) -> asyncio.AbstractServer:
    server = await asyncio.start_server(_handle, host="0.0.0.0", port=port)
    logger.info("Status page listening on 0.0.0.0:%s", port)
    return server
