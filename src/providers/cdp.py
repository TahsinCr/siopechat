"""Small synchronous Chrome DevTools client for local provider sessions."""

from __future__ import annotations

import ipaddress
import json
import time
from collections import deque
from urllib.parse import urlsplit

import httpx
import websocket

_PROVIDER_ORIGINS = frozenset(
    {
        "https://chatgpt.com",
        "https://gemini.google.com",
        "https://claude.ai",
    }
)
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_DEFAULT_TIMEOUT = 5.0


class DevToolsError(RuntimeError):
    """A local browser debugging endpoint or command failed."""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authority(parts) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port is not None else host


def _local_http_base(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except (AttributeError, TypeError, ValueError) as error:
        raise DevToolsError("Invalid local browser debugging address") from error
    if (
        parts.scheme != "http"
        or not _is_loopback(parts.hostname)
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise DevToolsError("A local browser debugging address is required")
    return f"http://{_authority(parts)}"


def _provider_origin(value: str) -> str:
    try:
        parts = urlsplit(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DevToolsError("Invalid provider address") from error
    origin = _page_origin(value)
    if origin is None:
        raise DevToolsError("Unsupported provider address")
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise DevToolsError("Unsupported provider address")
    return origin


def _page_origin(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        return None
    origin = f"{parts.scheme}://{parts.hostname}" if parts.hostname else ""
    if port is not None:
        origin += f":{port}"
    if (
        parts.scheme != "https"
        or origin not in _PROVIDER_ORIGINS
        or parts.username
        or parts.password
    ):
        return None
    return origin


def _debugger_websocket(value: str) -> tuple[str, str]:
    try:
        parts = urlsplit(value)
        port = parts.port
    except (AttributeError, TypeError, ValueError) as error:
        raise DevToolsError("Invalid browser debugging socket") from error
    if (
        parts.scheme != "ws"
        or not _is_loopback(parts.hostname)
        or parts.username
        or parts.password
        or not parts.path.startswith("/devtools/")
        or parts.query
        or parts.fragment
        or port is None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise DevToolsError("A local browser debugging socket is required")
    origin = f"http://{_authority(parts)}"
    return value, origin


def _get_json(base: str, path: str, timeout: float = _DEFAULT_TIMEOUT):
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", f"{base}{path}") as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise DevToolsError(
                        f"Connected browser debugging endpoint returned HTTP {response.status_code}"
                    )
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_METADATA_BYTES:
                        raise DevToolsError("Browser debugging metadata exceeds the size limit")
                    chunks.append(chunk)
    except httpx.HTTPError as error:
        raise DevToolsError("Connected browser debugging endpoint is unavailable") from error
    try:
        return json.loads(b"".join(chunks))
    except (json.JSONDecodeError, ValueError) as error:
        raise DevToolsError("Browser returned invalid debugging metadata") from error


def _find_page(pages, origin: str) -> dict | None:
    if not isinstance(pages, list):
        raise DevToolsError("Browser returned an invalid page list")
    for page in pages:
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        page_origin = _page_origin(page.get("url", ""))
        socket_url = page.get("webSocketDebuggerUrl")
        if page_origin == origin and isinstance(socket_url, str):
            _debugger_websocket(socket_url)
            return page
    return None


def _page_for_origin(base: str, origin: str) -> dict:
    page = _find_page(_get_json(base, "/json/list"), origin)
    if page is not None:
        return page
    raise DevToolsError("Open the provider in the connected browser first")


class DevToolsConnection:
    """One bounded, single-owner CDP WebSocket connection."""

    def __init__(self, socket_url: str, timeout: float = _DEFAULT_TIMEOUT):
        socket_url, origin = _debugger_websocket(socket_url)
        self._timeout = timeout
        self._next_id = 0
        self._events = deque(maxlen=1024)
        try:
            self._socket = websocket.create_connection(
                socket_url,
                timeout=timeout,
                origin=origin,
                http_no_proxy=["localhost", "127.0.0.1", "::1"],
                enable_multithread=False,
            )
        except (websocket.WebSocketException, OSError, ValueError) as error:
            raise DevToolsError("Could not connect to the local browser debugger") from error

    def _receive(self, timeout: float) -> dict:
        try:
            self._socket.settimeout(max(timeout, 0.001))
            message = self._socket.recv()
        except websocket.WebSocketTimeoutException as error:
            raise DevToolsError("Browser debugging request timed out") from error
        except (websocket.WebSocketException, OSError) as error:
            raise DevToolsError("Browser debugging connection was interrupted") from error
        try:
            value = json.loads(message)
        except (TypeError, json.JSONDecodeError) as error:
            raise DevToolsError("Browser returned an invalid debugging message") from error
        if not isinstance(value, dict):
            raise DevToolsError("Browser returned an invalid debugging message")
        return value

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._next_id += 1
        command_id = self._next_id
        duration = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + duration
        try:
            self._socket.settimeout(duration)
            self._socket.send(
                json.dumps(
                    {"id": command_id, "method": method, "params": params or {}},
                    separators=(",", ":"),
                )
            )
        except (websocket.WebSocketException, OSError, ValueError) as error:
            raise DevToolsError("Could not send a browser debugging command") from error
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DevToolsError(f"Browser debugging command timed out: {method}")
            message = self._receive(remaining)
            if message.get("id") == command_id:
                if "error" in message:
                    detail = message["error"].get("message", "unknown error")
                    raise DevToolsError(f"Browser debugging command failed: {detail}")
                result = message.get("result", {})
                if not isinstance(result, dict):
                    raise DevToolsError("Browser returned an invalid command result")
                return result
            self._events.append(message)

    def next_event(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            if self._events:
                message = self._events.popleft()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DevToolsError("Browser debugging request timed out")
                message = self._receive(remaining)
            if isinstance(message.get("method"), str):
                return message

    def close(self) -> None:
        try:
            self._socket.close()
        except (websocket.WebSocketException, OSError):
            pass

    def __enter__(self) -> DevToolsConnection:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def open_provider_page(cdp_url: str, provider_url: str) -> None:
    """Open the provider's root page only when no matching tab is already open."""
    base = _local_http_base(cdp_url)
    origin = _provider_origin(provider_url)
    pages = _get_json(base, "/json/list")
    if _find_page(pages, origin) is not None:
        return

    version = _get_json(base, "/json/version")
    socket_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
    if not isinstance(socket_url, str):
        raise DevToolsError("Browser did not provide a debugging socket")
    with DevToolsConnection(socket_url) as connection:
        result = connection.call("Target.createTarget", {"url": f"{origin}/", "background": False})
    if not isinstance(result.get("targetId"), str) or not result["targetId"]:
        raise DevToolsError("Browser did not create a provider tab")


def read_provider_cookies(cdp_url: str, provider_url: str) -> list[dict]:
    """Read cookies scoped to an already-open provider tab; never persist them here."""
    base = _local_http_base(cdp_url)
    origin = _provider_origin(provider_url)
    page = _page_for_origin(base, origin)
    with DevToolsConnection(page["webSocketDebuggerUrl"]) as connection:
        result = connection.call("Network.getCookies", {"urls": [origin]})
    cookies = result.get("cookies")
    if not isinstance(cookies, list) or any(not isinstance(cookie, dict) for cookie in cookies):
        raise DevToolsError("Browser returned an invalid cookie list")
    return cookies


def capture_chatgpt_auth(cdp_url: str, timeout: float = 15.0) -> tuple[str, dict]:
    """Capture the authenticated ChatGPT /me request from the connected page."""
    base = _local_http_base(cdp_url)
    origin = "https://chatgpt.com"
    page = _page_for_origin(base, origin)
    requests = {}
    extra_headers = {}
    responses = {}

    def bound(mapping, key, value):
        mapping[key] = value
        while len(mapping) > 256:
            mapping.pop(next(iter(mapping)))

    def result_for(request_id):
        if (
            request_id not in requests
            or request_id not in extra_headers
            or request_id not in responses
        ):
            return None
        status = responses.pop(request_id)
        request = requests.pop(request_id)
        headers = extra_headers.pop(request_id)
        if not 200 <= status < 300:
            return None
        merged_headers = request.get("headers", {})
        if not isinstance(merged_headers, dict):
            merged_headers = {}
        merged_headers = {**merged_headers, **headers}
        return request["url"], merged_headers

    with DevToolsConnection(page["webSocketDebuggerUrl"]) as connection:
        connection.call("Network.enable")
        connection.call("Page.navigate", {"url": f"{origin}/"})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DevToolsError(
                    "Timed out waiting for an authenticated ChatGPT account request"
                )
            event = connection.next_event(remaining)
            params = event.get("params", {})
            request_id = params.get("requestId")
            if not isinstance(request_id, str):
                continue
            method = event.get("method")
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                if not isinstance(request, dict) or request.get("method") != "GET":
                    continue
                try:
                    request_url = urlsplit(request.get("url", ""))
                except ValueError:
                    continue
                if (
                    request_url.scheme != "https"
                    or request_url.hostname != "chatgpt.com"
                    or request_url.path != "/backend-api/me"
                ):
                    continue
                bound(requests, request_id, request)
            elif method == "Network.requestWillBeSentExtraInfo":
                headers = params.get("headers", {})
                bound(
                    extra_headers,
                    request_id,
                    headers if isinstance(headers, dict) else {},
                )
            elif method == "Network.responseReceived" and request_id in requests:
                response = params.get("response", {})
                status = response.get("status") if isinstance(response, dict) else None
                if isinstance(status, (int, float)):
                    bound(responses, request_id, int(status))
            captured = result_for(request_id)
            if captured is not None:
                return captured
