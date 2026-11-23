import asyncio
import re
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from .hls import HLSError, validate_discovered_hls
from .net import NetError, parse_public_url
from .web_sources import MediaGroup, parse_page


Kind = Literal["mp4", "hls", "webm", "mov"]
DIRECT_NAMES = {"mp4": "MP4", "webm": "WebM", "mov": "MOV"}
MIME_KINDS: dict[str, Kind] = {
    "video/mp4": "mp4",
    "video/x-m4v": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "application/vnd.apple.mpegurl": "hls",
    "application/mpegurl": "hls",
    "application/x-mpegurl": "hls",
    "audio/mpegurl": "hls",
    "audio/x-mpegurl": "hls",
}
EXTENSION_KINDS: dict[str, Kind] = {
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".webm": "webm", ".m3u8": "hls",
}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
MP4_ATOMS = {b"ftyp", b"styp", b"moov", b"mdat", b"free", b"skip", b"wide", b"sidx", b"uuid"}
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"
MEDIA_PREFIX = 256
HTML_LIMIT = 2 * 1024 * 1024
HLS_PROBE_LIMIT = 1024 * 1024
CANDIDATE_LIMIT = 120
EPISODE_LIMIT = 250
PROBE_TIMEOUT = 15
PAGE_CHECK_TIMEOUT = 35
EPISODE = re.compile(r"(?<![a-z0-9])(?:tap|episode|ep|e)[\s._-]*(\d{1,4})(?![a-z0-9])")
SEASON_EPISODE = re.compile(r"(?<![a-z0-9])s\d{1,3}e(\d{1,4})(?![a-z0-9])")
HTML_PREFIX = re.compile(rb"\s*(?:<!doctype\s+html\b|<!--|<(?:html|head|meta|title|body|main|video)\b)", re.I)
CHARSET = re.compile(r"\bcharset\s*=\s*['\"]?([\w.-]+)", re.I)
ACCESS_PARAMETERS = {"access_token", "auth", "authorization", "hdnea", "hdnts", "jwt", "key-pair-id", "policy", "sig", "signature", "token"}
SAMPLE_PAGES = {("gkg0.aqkmowihz.cc", "/video/15372"), ("v24t.aqkmowihz.cc", "/video/15586")}
SAMPLE_NOTICE = (
    "Trang này công bố thông tin tập nhưng chưa xác minh được liên kết tải công khai. "
    "Hãy nhập link video trực tiếp mà bạn có quyền tải."
)
PAGE_NOTICE = (
    "Không tìm thấy liên kết video công khai trong HTML của trang. "
    "Hãy nhập link MP4, M4V, WebM, MOV hoặc HLS trực tiếp."
)


@dataclass(frozen=True)
class Source:
    number: int
    url: str
    kind: Kind
    label: str


@dataclass(frozen=True)
class DiscoveredMedia:
    url: str
    kind: Kind
    number: int | None = None
    label: str | None = None


def media_type(value: str) -> str:
    return value.partition(";")[0].strip().lower()


def media_header(data: bytes | bytearray, kind: Kind) -> bool:
    if kind == "webm":
        return data.startswith(WEBM_MAGIC) and b"\x42\x82\x84webm" in data[:MEDIA_PREFIX]
    return len(data) >= 8 and bytes(data[4:8]) in MP4_ATOMS


def kind_from_path(url: str) -> Kind | None:
    path = PurePosixPath(unquote(urlsplit(url).path).lower())
    return EXTENSION_KINDS.get(path.suffix)


def episode_number(url: str) -> int | None:
    path = unquote(urlsplit(url).path).lower().strip("/")
    normalized = unicodedata.normalize("NFKD", path)
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    match = EPISODE.search(normalized) or SEASON_EPISODE.search(normalized)
    if match:
        value = int(match[1])
    else:
        stem = PurePosixPath(normalized).stem
        if not re.fullmatch(r"[0-9]{1,4}", stem):
            return None
        value = int(stem)
    return value or None


def response_error(code: int) -> str:
    if code in {401, 403, 407, 451}:
        return f"Nguồn yêu cầu quyền truy cập hoặc từ chối kết nối (HTTP {code}). Hãy dùng link công khai."
    if code == 404:
        return "Không tìm thấy video ở nguồn này (HTTP 404)."
    return f"Nguồn trả về HTTP {code}."


async def read_prefix(stream: AsyncIterator[bytes], minimum: int = MEDIA_PREFIX) -> bytearray:
    prefix = bytearray()
    async for chunk in stream:
        prefix.extend(chunk)
        if len(prefix) >= minimum:
            break
    return prefix


async def read_bounded(data: bytearray, stream: AsyncIterator[bytes], maximum: int, error: str) -> bytearray:
    if len(data) > maximum:
        raise NetError(error)
    async for chunk in stream:
        if len(data) + len(chunk) > maximum:
            raise NetError(error)
        data.extend(chunk)
    return data


def sniff_media(data: bytes | bytearray, url: str, content_type: str, declared_type: str = "") -> Kind | None:
    if media_header(data, "webm"):
        return "webm"
    if media_header(data, "mp4"):
        if content_type.startswith("audio/") or data[4:12] in (b"ftypM4A ", b"ftypM4B "):
            return None
        if data[4:12] == b"ftypqt  ":
            return "mov"
        for hint in (MIME_KINDS.get(content_type), kind_from_path(url), MIME_KINDS.get(declared_type)):
            if hint in {"mp4", "mov"}:
                return hint
        return "mp4"
    if data.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U"):
        return "hls"
    return None


def decode_html(data: bytearray, content_type: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    charset = CHARSET.search(content_type) or CHARSET.search(data[:1024].decode("ascii", errors="ignore"))
    if charset:
        try:
            return data.decode(charset[1], errors="replace")
        except (LookupError, UnicodeError):
            pass
    return data.decode("utf-8", errors="replace")


async def inspect_input(client, url: str):
    async with client.open(url) as response:
        if response.status_code != 200:
            raise NetError(response_error(response.status_code))
        content_type = response.headers.get("content-type", "")
        mime = media_type(content_type)
        stream = response.aiter_bytes()
        prefix = await read_prefix(stream)
        if mime in HTML_TYPES or HTML_PREFIX.match(prefix.removeprefix(b"\xef\xbb\xbf")):
            data = await read_bounded(prefix, stream, HTML_LIMIT, "Trang HTML vượt quá 2 MiB. Hãy nhập link video trực tiếp.")
            return parse_page(decode_html(data, content_type), response.url)
        kind = sniff_media(prefix, response.url, mime)
        if kind is None:
            raise NetError("Không nhận ra video MP4, M4V, WebM, MOV hoặc HLS ở địa chỉ này.")
        return kind


async def inspect_candidate(client, url: str, declared_type: str) -> Kind:
    parameters = {name.lower() for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    if parameters & ACCESS_PARAMETERS or any(name.startswith(("x-amz-", "x-goog-")) for name in parameters):
        raise NetError("Trang đưa ra liên kết có thông tin truy cập riêng.")
    async with client.open(url) as response:
        if response.status_code != 200:
            raise NetError(response_error(response.status_code))
        mime = media_type(response.headers.get("content-type", ""))
        stream = response.aiter_bytes()
        prefix = await read_prefix(stream)
        kind = sniff_media(prefix, response.url, mime, declared_type)
        if kind is None:
            raise NetError("Liên kết trong trang không trả về video có định dạng được hỗ trợ.")
        if kind == "hls":
            data = await read_bounded(prefix, stream, HLS_PROBE_LIMIT, "Danh sách HLS vượt quá giới hạn kiểm tra 1 MiB. Hãy nhập link HLS trực tiếp.")
            validate_discovered_hls(data)
        return kind


async def discover(client, links: str) -> tuple[list[Source], list[str], str]:
    lines = [(i, line.strip()) for i, line in enumerate(links.splitlines(), 1) if line.strip()]
    if not lines:
        raise ValueError("Hãy nhập ít nhất một đường dẫn.")
    if len(lines) > EPISODE_LIMIT:
        raise ValueError(f"Mỗi lần chỉ phân tích tối đa {EPISODE_LIMIT} đường dẫn.")

    semaphore = asyncio.Semaphore(4)
    seen: set[str] = set()
    remaining_checks = CANDIDATE_LIMIT

    async def checked(operation, *args):
        async with semaphore:
            try:
                return await asyncio.wait_for(operation(client, *args), timeout=PROBE_TIMEOUT)
            except asyncio.TimeoutError:
                raise NetError("Nguồn phản hồi quá chậm khi tìm video.") from None

    async def choose(group: MediaGroup) -> tuple[DiscoveredMedia | None, str | None]:
        nonlocal remaining_checks
        error = None
        for candidate in group.candidates:
            if not remaining_checks:
                return None, "Đã đạt giới hạn kiểm tra liên kết. Hãy tìm từng trang hoặc nhập link trực tiếp."
            remaining_checks -= 1
            try:
                url = parse_public_url(candidate.url)
                kind = await checked(inspect_candidate, url, media_type(candidate.mime or ""))
                return DiscoveredMedia(url, kind, group.number or episode_number(url), group.label), None
            except (NetError, HLSError) as exc:
                error = error or str(exc)
        return None, error

    async def find_page_media(groups: list[MediaGroup]) -> tuple[list[DiscoveredMedia], list[str]]:
        tasks = [asyncio.create_task(choose(group)) for group in groups]
        try:
            done, pending = await asyncio.wait(tasks, timeout=PAGE_CHECK_TIMEOUT)
            checked_groups = [task.result() for task in tasks if task in done]
        finally:
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)
        media = [item for item, _ in checked_groups if item is not None]
        errors = list(dict.fromkeys(error for _, error in checked_groups if error))
        if pending:
            errors.insert(0, "Một số liên kết trong trang phản hồi quá chậm. Hãy thử lại hoặc nhập link trực tiếp.")
        return media, errors

    async def inspect(line_no: int, line: str):
        try:
            if len(line) > 8192:
                raise NetError("Đường dẫn vượt quá độ dài hỗ trợ.")
            url = parse_public_url(line)
            if url in seen:
                return [], None, []
            seen.add(url)
            parts = urlsplit(url)
            if (parts.hostname, parts.path.rstrip("/")) in SAMPLE_PAGES:
                raise NetError(SAMPLE_NOTICE)
            result = kind_from_path(url) or await checked(inspect_input, url)
            if isinstance(result, str):
                return [DiscoveredMedia(url, result, episode_number(url))], None, []
            if not result.groups:
                raise NetError(PAGE_NOTICE)
            media, errors = await find_page_media(result.groups)
            notes = [f"Dòng {line_no}. {error}" for error in errors[:3]]
            return media, result.title if media else None, notes
        except NetError as exc:
            return [], None, [f"Dòng {line_no}. {exc}"]

    results = await asyncio.gather(*(inspect(i, line) for i, line in lines))
    found: dict[str, DiscoveredMedia] = {}
    for group, _, _ in results:
        for item in group:
            found.setdefault(item.url, item)
    media = list(found.values())[:EPISODE_LIMIT]
    used = {item.number for item in media if item.number is not None}
    next_number = 1
    episodes = []
    for item in media:
        number = item.number
        if number is None:
            while next_number in used:
                next_number += 1
            number = next_number
            next_number += 1
        episodes.append(Source(number, item.url, item.kind, item.label or f"Tập {number:02d}"))
    episodes.sort(key=lambda episode: episode.number)
    messages = [message for _, _, notes in results for message in notes]
    if len(found) > EPISODE_LIMIT:
        messages.append(f"Hiển thị tối đa {EPISODE_LIMIT} tập mỗi lần tìm.")
    title = results[0][1] if len(results) == 1 else None
    return episodes, messages, title or "Tên phim"
