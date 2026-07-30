"""ASGI session-affinity reverse proxy for the Web UI routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeAlias

import httpx

ASGIApp: TypeAlias = Callable[[dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[None]]
SessionDecoder: TypeAlias = Callable[[str], tuple[str, str] | None]

logger = logging.getLogger(__name__)

_INTERNAL_HOP_HEADER = b"x-doris-session-affinity-hop"
_HOP_BY_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


class _ClientDisconnected(Exception):
    """The downstream ASGI server reported that its client went away."""


def _connection_tokens(headers: list[tuple[bytes, bytes]]) -> set[bytes]:
    """Return lower-case header names nominated by Connection headers."""
    tokens: set[bytes] = set()
    for name, value in headers:
        if name.lower() == b"connection":
            tokens.update(token.strip().lower() for token in value.split(b",") if token.strip())
    return tokens


def _forward_headers(headers: list[tuple[bytes, bytes]], *, add_hop: bool = False) -> list[tuple[bytes, bytes]]:
    """Drop routing and hop-by-hop fields without flattening duplicate headers."""
    connection_headers = _connection_tokens(headers)
    excluded = _HOP_BY_HOP_HEADERS | connection_headers | {b"host", _INTERNAL_HOP_HEADER}
    result = [(name, value) for name, value in headers if name.lower() not in excluded]
    if add_hop:
        result.append((_INTERNAL_HOP_HEADER, b"1"))
    return result


def _without_internal_header(scope: dict[str, Any]) -> dict[str, Any]:
    """Do not expose the proxy control header to the local application."""
    headers = scope.get("headers", [])
    if not any(name.lower() == _INTERNAL_HOP_HEADER for name, _ in headers):
        return scope
    local_scope = dict(scope)
    local_scope["headers"] = [
        (name, value) for name, value in headers if name.lower() != _INTERNAL_HOP_HEADER
    ]
    return local_scope


def _cookie_value(headers: list[tuple[bytes, bytes]], cookie_name: str) -> str | None:
    """Read one named cookie without depending on a framework request object."""
    wanted = cookie_name.encode("ascii")
    for header_name, header_value in headers:
        if header_name.lower() != b"cookie":
            continue
        for item in header_value.split(b";"):
            name, separator, value = item.strip().partition(b"=")
            if separator and name == wanted:
                return value.decode("latin-1")
    return None


class SessionAffinityProxy:
    """Route a decoded Web UI session to the process which owns it.

    ``decoder`` is deliberately the only cookie-format dependency: a successful
    decoder result is trusted by this middleware.  ``target_port`` is local
    configuration, never data taken from the cookie.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        decoder: SessionDecoder,
        local_ip: str,
        target_port: int,
        cookie_name: str = "doris_mcp_session",
        client: httpx.AsyncClient | None = None,
        timeout: httpx.TimeoutTypes | None = None,
    ) -> None:
        self.app = app
        self.decoder = decoder
        self.local_ip = local_ip
        self.target_port = target_port
        self.cookie_name = cookie_name
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._client_lock = asyncio.Lock()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._run_lifespan(scope, receive, send)
            return
        if scope_type != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Login must stay local even if a stale cookie identifies another node.
        if path != "/mcp/web" and not path.startswith("/mcp/web/"):
            await self.app(scope, receive, send)
            return
        if path == "/mcp/web/login":
            await self.app(_without_internal_header(scope), receive, send)
            return

        headers = scope.get("headers", [])
        cookie = _cookie_value(headers, self.cookie_name)
        try:
            decoded = self.decoder(cookie) if cookie is not None else None
        except Exception:  # A decoder rejection is treated exactly as an invalid cookie.
            logger.warning("Session-affinity cookie decoder rejected a cookie")
            decoded = None
        if decoded is None or decoded[1] == self.local_ip:
            await self.app(_without_internal_header(scope), receive, send)
            return

        if any(name.lower() == _INTERNAL_HOP_HEADER for name, _ in headers):
            await self._send_error(send, 502, b"Bad Gateway")
            return
        await self._proxy(scope, receive, send, decoded[1])

    async def _run_lifespan(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        # The server drives the lifespan coroutine through startup and shutdown;
        # retaining the client for that whole call gives one client per process.
        await self._get_client()
        try:
            await self.app(scope, receive, send)
        finally:
            await self._close_client()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=self._timeout,
                )
        return self._client

    async def _close_client(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _proxy(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        target_ip: str,
    ) -> None:
        response: httpx.Response | None = None
        try:
            raw_path = scope.get("raw_path") or scope["path"].encode("utf-8")
            raw_query = scope.get("query_string", b"")
            # ASGI raw path/query are normally ASCII with non-ASCII octets
            # percent-encoded.  Constructing from that representation avoids
            # decoding and re-encoding escaped slash/query bytes.
            suffix = raw_path.decode("ascii")
            if raw_query:
                suffix += "?" + raw_query.decode("ascii")
            url = httpx.URL(f"http://{target_ip}:{self.target_port}{suffix}")
            request = (await self._get_client()).build_request(
                scope["method"],
                url,
                headers=_forward_headers(scope.get("headers", []), add_hop=True),
                content=self._request_body(receive),
            )
            response = await (await self._get_client()).send(request, stream=True)
            await self._stream_response(response, receive, send)
        except _ClientDisconnected:
            return
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            logger.warning("Session-affinity upstream timed out")
            if response is None:  # A response object means its headers were sent.
                await self._send_error(send, 504, b"Gateway Timeout")
        except httpx.NetworkError:
            logger.warning("Session-affinity upstream network error")
            if response is None:  # Do not append an error after response headers.
                await self._send_error(send, 502, b"Bad Gateway")
        finally:
            if response is not None:
                await response.aclose()

    async def _request_body(
        self, receive: Callable[[], Awaitable[dict[str, Any]]]
    ) -> AsyncIterator[bytes]:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _ClientDisconnected
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            if body:
                yield body
            if not message.get("more_body", False):
                return

    async def _stream_response(
        self,
        response: httpx.Response,
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        await send({
            "type": "http.response.start",
            "status": response.status_code,
            "headers": _forward_headers(list(response.headers.raw)),
        })
        # Some test transports intentionally provide an already-buffered
        # response.  Real ``send(..., stream=True)`` responses take the branch
        # below and remain fully streaming.
        if response.is_stream_consumed:
            await send({"type": "http.response.body", "body": response.content, "more_body": False})
            return

        iterator = response.aiter_raw()
        disconnect_waiter = asyncio.create_task(self._wait_for_disconnect(receive))
        try:
            while True:
                chunk_task = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait(
                    {chunk_task, disconnect_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect_waiter in done:
                    chunk_task.cancel()
                    await asyncio.gather(chunk_task, return_exceptions=True)
                    raise _ClientDisconnected
                try:
                    chunk = chunk_task.result()
                except StopAsyncIteration:
                    break
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            disconnect_waiter.cancel()
            await asyncio.gather(disconnect_waiter, return_exceptions=True)

    @staticmethod
    async def _wait_for_disconnect(
        receive: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        while (message := await receive())["type"] != "http.disconnect":
            pass

    @staticmethod
    async def _send_error(
        send: Callable[[dict[str, Any]], Awaitable[None]], status: int, body: bytes
    ) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})


# Explicit alias for integrations that name ASGI wrappers as middleware.
SessionAffinityProxyMiddleware = SessionAffinityProxy
