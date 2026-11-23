import asyncio
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .net import NetError, parse_public_url


HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"}
MP4_ATOMS = {b"ftyp", b"styp", b"moov", b"mdat", b"free", b"skip", b"wide", b"sidx", b"uuid"}
EPISODE = re.compile(r"(?<![a-z0-9])(?:tap|episode|ep|e)[\s._-]*(\d{1,4})(?![a-z0-9])")
SAMPLE_HOST = "gkg0.aqkmowihz.cc"
SAMPLE_NOTICE = (
    "Trang mẫu công khai thông tin tập nhưng chưa xác minh được nguồn MP4 hoặc HLS tải trực tiếp. "
    "Hãy nhập link video trực tiếp."
)
PAGE_NOTICE = "Đây là trang web chưa có tích hợp danh sách tập. Hãy nhập link MP4 hoặc HLS trực tiếp."


@dataclass(frozen=True)
class Source:
    number: int
    url: str
    kind: str
    label: str


def mp4_header(data: bytes | bytearray) -> bool:
    return len(data) >= 8 and bytes(data[4:8]) in MP4_ATOMS


def kind_from_path(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    if path.endswith(".mp4"):
        return "mp4"
    if path.endswith(".m3u8"):
        return "hls"
    return None


def episode_number(url: str) -> int | None:
    path = unquote(urlsplit(url).path).lower().strip("/")
    normalized = unicodedata.normalize("NFKD", path)
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    match = EPISODE.search(normalized)
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


async def probe(client, url: str, limit: asyncio.Semaphore) -> str:
    async with limit, client.open(url) as response:
        if response.status_code != 200:
            raise NetError(response_error(response.status_code))
        media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type in {"text/html", "application/xhtml+xml"}:
            raise NetError(PAGE_NOTICE)
        if media_type == "video/mp4":
            return "mp4"
        if media_type in HLS_TYPES:
            return "hls"
        kind = kind_from_path(response.url)
        if kind:
            return kind
        prefix = bytearray()
        async for chunk in response.aiter_bytes():
            prefix.extend(chunk[: 64 - len(prefix)])
            if len(prefix) >= 64:
                break
        if mp4_header(prefix):
            return "mp4"
        if prefix.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U"):
            return "hls"
        raise NetError("Không nhận ra nguồn MP4 hoặc HLS. Hãy nhập link video trực tiếp.")


async def discover(client, links: str) -> tuple[list[Source], list[str]]:
    lines = [(i, line.strip()) for i, line in enumerate(links.splitlines(), 1) if line.strip()]
    if not lines:
        raise ValueError("Hãy nhập ít nhất một đường dẫn.")
    if len(lines) > 250:
        raise ValueError("Mỗi lần chỉ phân tích tối đa 250 đường dẫn.")

    semaphore = asyncio.Semaphore(4)
    seen: set[str] = set()

    async def inspect(line_no: int, line: str):
        try:
            if len(line) > 8192:
                raise NetError("Đường dẫn vượt quá độ dài hỗ trợ.")
            url = parse_public_url(line)
            if url in seen:
                return None, None
            seen.add(url)
            parts = urlsplit(url)
            if parts.hostname == SAMPLE_HOST and re.fullmatch(r"/video/\d+/?", parts.path):
                raise NetError(SAMPLE_NOTICE)
            kind = kind_from_path(url) or await probe(client, url, semaphore)
            return (url, kind, episode_number(url)), None
        except NetError as exc:
            return None, f"Dòng {line_no}. {exc}"

    results = await asyncio.gather(*(inspect(i, line) for i, line in lines))
    found = [entry for entry, _ in results if entry is not None]
    used = {number for _, _, number in found if number is not None}
    next_number = 1
    episodes = []
    for url, kind, number in found:
        if number is None:
            while next_number in used:
                next_number += 1
            number = next_number
            next_number += 1
        episodes.append(Source(number, url, kind, f"Tập {number:02d}"))
    episodes.sort(key=lambda episode: episode.number)
    return episodes, [notice for _, notice in results if notice is not None]
