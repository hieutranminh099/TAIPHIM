"""Streaming HTTP client restricted to public servers."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from time import monotonic
from urllib.parse import urlsplit

import httpcore
import httpx


class NetError(Exception):
    """An outbound request failed, with a message safe to display."""


_HOST_LABEL = re.compile(rb"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)
_BAD_URL_CHAR = re.compile(r"[\x00-\x20\x7f\\]")
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".home.arpa")
_IPV4_SPECIAL = tuple(
    ipaddress.IPv4Network(network)
    for network in ("192.0.0.0/24", "192.88.99.0/24")
)
_IPV6_UNICAST = ipaddress.IPv6Network("2000::/3")
_IPV6_SPECIAL = tuple(
    ipaddress.IPv6Network(network)
    for network in ("2001::/23", "2002::/16", "3fff::/20")
)
_REDIRECTS = {301, 302, 303, 307, 308}
_BLOCKED = "Địa chỉ mạng nội bộ hoặc địa chỉ không công khai đã bị chặn."
_BAD_URL = "URL không hợp lệ. Dùng địa chỉ HTTP hoặc HTTPS đầy đủ."
_CONNECT_TIMEOUT = 10.0
_REQUEST_TIMEOUT = {"connect": _CONNECT_TIMEOUT, "read": 20.0, "write": 10.0, "pool": 5.0}


def _check_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise NetError(_BLOCKED)
    if isinstance(address, ipaddress.IPv4Address):
        if any(address in network for network in _IPV4_SPECIAL):
            raise NetError(_BLOCKED)
    elif (
        address.scope_id is not None
        or address not in _IPV6_UNICAST
        or any(address in network for network in _IPV6_SPECIAL)
    ):
        raise NetError(_BLOCKED)


def _parse_url(url: str) -> httpx.URL:
    value = url.strip()
    if len(value) > 8192 or _BAD_URL_CHAR.search(value):
        raise NetError(_BAD_URL)
    try:
        authority = urlsplit(value).netloc
        if "@" in authority:
            raise NetError("URL không được chứa thông tin đăng nhập.")
        parsed = httpx.URL(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not authority
            or authority.endswith(":")
            or not parsed.raw_host
        ):
            raise NetError(_BAD_URL)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise NetError(_BAD_URL)
    except (httpx.InvalidURL, UnicodeError, ValueError):
        raise NetError(_BAD_URL) from None

    raw_host = parsed.raw_host.removesuffix(b".").lower()
    host = raw_host.decode("ascii")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
            raise NetError(_BLOCKED) from None
        labels = raw_host.split(b".")
        if (
            len(labels) < 2
            or len(raw_host) > 253
            or labels[-1].isdigit()
            or not all(_HOST_LABEL.fullmatch(label) for label in labels)
        ):
            raise NetError(_BAD_URL) from None
    else:
        _check_ip(address)
    return parsed.copy_with(host=host, fragment=None)


def parse_public_url(url: str) -> str:
    """Validate URL syntax and literal hosts without making a DNS request."""
    return str(_parse_url(url))


async def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _check_ip(literal)
        return (str(literal),)

    try:
        # The final dot prevents the resolver from applying a local DNS search suffix.
        results = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                f"{host}.", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            ),
            timeout=_CONNECT_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError):
        raise NetError("Không phân giải được tên miền nguồn video.") from None

    addresses = []
    for _family, _type, _proto, _name, sockaddr in results:
        address = ipaddress.ip_address(sockaddr[0])
        _check_ip(address)
        addresses.append(str(address))
    if not addresses:
        raise NetError("Tên miền nguồn video không trả về địa chỉ.")
    return tuple(dict.fromkeys(addresses))


class _PinnedBackend(httpcore.AnyIOBackend):
    def __init__(self, addresses: tuple[str, ...]):
        self.addresses = addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        del host
        deadline = monotonic() + (timeout if timeout is not None else _CONNECT_TIMEOUT)
        error = None
        for index, address in enumerate(self.addresses):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                # Preserve the origin name in httpcore for Host and certificate
                # verification. Only the socket destination uses the checked IP.
                return await super().connect_tcp(
                    address,
                    port,
                    timeout=remaining / (len(self.addresses) - index),
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        if error is not None:
            raise error
        raise httpcore.ConnectTimeout("Unable to reach the public origin in time")


def _message_for(error: Exception) -> str:
    if isinstance(error, httpcore.TimeoutException):
        return "Nguồn video phản hồi quá chậm."
    if isinstance(error, httpcore.ConnectError):
        return "Không kết nối được với nguồn video. Kiểm tra địa chỉ hoặc chứng chỉ HTTPS."
    return "Kết nối tới nguồn video bị gián đoạn hoặc trả dữ liệu HTTP không hợp lệ."


_HTTP_ERRORS = (
    httpcore.NetworkError,
    httpcore.TimeoutException,
    httpcore.ProtocolError,
    httpcore.UnsupportedProtocol,
)


class PublicResponse:
    def __init__(self, response: httpcore.Response, url: httpx.URL):
        self._response = response
        self.status_code = response.status
        self.headers = httpx.Headers(response.headers)
        self.url = str(url)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self.headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise NetError("Máy chủ gửi dữ liệu nén HTTP ngoài định dạng được hỗ trợ.")
        try:
            async for chunk in self._response.aiter_stream():
                yield chunk
        except _HTTP_ERRORS as exc:
            raise NetError(_message_for(exc)) from None


class PublicClient:
    """Each request hop gets a stateless connection and a fresh checked DNS result."""

    def __init__(self):
        self._ssl_context = ssl.create_default_context()

    @asynccontextmanager
    async def open(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> AsyncIterator[PublicResponse]:
        current = _parse_url(url)
        request_headers = httpx.Headers(
            {
                "User-Agent": "TramTaiTapPhim/1.0",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
        )
        for name, value in (headers or {}).items():
            if name.lower() in {"accept", "range"}:
                request_headers[name] = value

        for _ in range(6):
            port = current.port or (443 if current.scheme == "https" else 80)
            addresses = await _resolve_public_addresses(current.raw_host.decode("ascii"), port)
            request_headers["Host"] = current.netloc.decode("ascii")
            target = httpcore.URL(
                scheme=current.raw_scheme,
                host=current.raw_host,
                port=port,
                target=current.raw_path,
            )
            async with AsyncExitStack() as stack:
                pool = await stack.enter_async_context(
                    httpcore.AsyncConnectionPool(
                        ssl_context=self._ssl_context,
                        max_connections=1,
                        max_keepalive_connections=0,
                        network_backend=_PinnedBackend(addresses),
                    )
                )
                try:
                    raw_response = await stack.enter_async_context(
                        pool.stream(
                            "GET", target, headers=request_headers.raw,
                            extensions={"timeout": _REQUEST_TIMEOUT}
                        )
                    )
                except _HTTP_ERRORS as exc:
                    raise NetError(_message_for(exc)) from None
                response = PublicResponse(raw_response, current)
                if response.status_code not in _REDIRECTS:
                    yield response
                    return

                locations = response.headers.get_list("location")
                if len(locations) != 1 or not locations[0]:
                    raise NetError("Máy chủ trả về chuyển hướng không hợp lệ.")
                # Validate absolute redirects before URL normalization can drop
                # empty credentials. Relative paths are resolved against the origin.
                location = locations[0]
                try:
                    parts = urlsplit(location)
                    if (
                        _BAD_URL_CHAR.search(location)
                        or "@" in parts.netloc
                        or (parts.scheme and not parts.netloc)
                    ):
                        raise ValueError
                    current = _parse_url(str(current.join(location)))
                except (httpx.InvalidURL, UnicodeError, ValueError):
                    raise NetError("Máy chủ trả về chuyển hướng không hợp lệ.") from None

        raise NetError("Nguồn video chuyển hướng quá nhiều lần.")
