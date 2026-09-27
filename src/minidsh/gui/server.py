"""Small loopback-only HTTP adapter for the browser presentation."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import sys
import webbrowser
from contextlib import suppress
from http import HTTPStatus
from importlib.resources import files
from typing import Any

from minidsh.gui.session import GuiSession

_HEADER_LIMIT = 8192
_BODY_LIMIT = 65_536
_MAX_PORT = 65_535


async def _request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    header = await reader.readuntil(b"\r\n\r\n")
    if len(header) > _HEADER_LIMIT:
        raise ValueError("Headers too large")
    lines = header.decode("ascii").split("\r\n")
    method, path, version = lines[0].split(" ")
    if version != "HTTP/1.1":
        raise ValueError("HTTP/1.1 required")
    headers: dict[str, str] = {}
    for line in lines[1:-2]:
        key, value = line.split(":", 1)
        key = key.lower()
        if key in headers:
            raise ValueError("Duplicate header")
        headers[key] = value.strip()
    if "transfer-encoding" in headers:
        raise ValueError("Transfer encoding unsupported")
    length = int(headers.get("content-length", "0"))
    if not 0 <= length <= _BODY_LIMIT:
        raise ValueError("Body too large")
    return method, path, headers, await reader.readexactly(length)


async def _response(
    writer: asyncio.StreamWriter,
    status: HTTPStatus,
    body: bytes,
    content_type: str = "application/json; charset=utf-8",
) -> None:
    headers = (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\nCache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
        "Content-Security-Policy: default-src 'self'; script-src 'self'; "
        "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'\r\n\r\n"
    )
    writer.write(headers.encode("ascii") + body)
    await writer.drain()


async def _route(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session: GuiSession,
    token: str,
) -> None:
    method, path, headers, body = await _request(reader)
    address = writer.get_extra_info("sockname")
    host = f"127.0.0.1:{address[1]}"
    if headers.get("host") != host or headers.get("origin", f"http://{host}") != f"http://{host}":
        await _response(writer, HTTPStatus.FORBIDDEN, b'{"error":"Untrusted origin or host"}')
        return
    assets = {
        "/": ("index.html", "text/html"),
        "/app.js": ("app.js", "text/javascript"),
        "/style.css": ("style.css", "text/css"),
    }
    if method == "GET" and path in assets:
        filename, mime = assets[path]
        await _response(
            writer,
            HTTPStatus.OK,
            files("minidsh.gui").joinpath(filename).read_bytes(),
            f"{mime}; charset=utf-8",
        )
        return
    if not path.startswith("/api/"):
        await _response(writer, HTTPStatus.NOT_FOUND, b'{"error":"Not found"}')
        return
    if not hmac.compare_digest(headers.get("x-minidsh-token", ""), token):
        await _response(writer, HTTPStatus.FORBIDDEN, b'{"error":"Session token required"}')
        return
    if method == "GET" and path == "/api/state":
        await _response(
            writer, HTTPStatus.OK, json.dumps(session.snapshot(), ensure_ascii=False).encode()
        )
        return
    actions = {"/api/run", "/api/cancel", "/api/clear", "/api/resume", "/api/diff", "/api/settings"}
    if method != "POST" or path not in actions:
        await _response(writer, HTTPStatus.NOT_FOUND, b'{"error":"Unknown API endpoint"}')
        return
    data: Any = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    await session.action(path.removeprefix("/api/"), data)
    await _response(writer, HTTPStatus.OK, b'{"ok":true}')


async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    session: GuiSession,
    token: str,
) -> None:
    """Serve one bounded request, then close; never log credentials or payloads."""
    try:
        async with asyncio.timeout(10):
            await _route(reader, writer, session, token)
    except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        # Validation errors may contain input secrets: return a fixed message.
        await _response(
            writer, HTTPStatus.BAD_REQUEST, b'{"error":"Invalid request or operation unavailable"}'
        )
    except TimeoutError:
        pass
    except (OSError, RuntimeError):
        await _response(
            writer, HTTPStatus.INTERNAL_SERVER_ERROR, b'{"error":"Local operation failed"}'
        )
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def serve_gui(session: GuiSession, *, port: int = 0, open_browser: bool = True) -> None:
    """Run the local GUI until cancelled by the CLI; drain the Agent on exit."""
    if not 0 <= port <= _MAX_PORT:
        raise ValueError("port must be between 0 and 65535")
    token = secrets.token_urlsafe(32)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(reader, writer, session=session, token=token)

    server = await asyncio.start_server(handler, "127.0.0.1", port, limit=_HEADER_LIMIT)
    actual_port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{actual_port}/#token={token}"
    sys.stdout.write(f"Mini-dsh GUI: {url}\nCtrl+C to stop.\n")
    sys.stdout.flush()
    try:
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        await session.close()
