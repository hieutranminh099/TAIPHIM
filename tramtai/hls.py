"""Download finite, unencrypted HLS and remux locally with FFmpeg."""

from __future__ import annotations

import asyncio
import math
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from .net import PublicClient, parse_public_url


class HLSError(Exception):
    """An HLS source or remuxing error safe to display."""


_MANIFEST_LIMIT = 8 * 1024 * 1024
_PROBE_SIZE = 32 * 1024
_NUMBER = re.compile(r"[0-9]{1,18}")
_DURATION = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,12})?")
_BYTE_RANGE = re.compile(r"([0-9]{1,18})(?:@([0-9]{1,18}))?")
_CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19}|\*)", re.I)
_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=(?:"([^"\r\n]*)"|([^,"\s]+))')
_UNSUPPORTED = {
    "#EXT-X-DEFINE",
    "#EXT-X-GAP",
    "#EXT-X-I-FRAMES-ONLY",
    "#EXT-X-PART",
    "#EXT-X-PRELOAD-HINT",
    "#EXT-X-SKIP",
}
_FORMATS = "mov,mpegts,aac,mp3,ac3,eac3"
_ENCRYPTED = "Nguồn HLS sử dụng mã hóa hoặc DRM."
_LIVE = "Nguồn HLS đang phát trực tiếp hoặc chưa kết thúc (thiếu EXT-X-ENDLIST)."


@dataclass(frozen=True)
class Resource:
    url: str
    start: int | None = None
    length: int | None = None


@dataclass(frozen=True)
class Segment:
    duration: str
    resource: Resource
    init: Resource | None
    discontinuity: bool


@dataclass(frozen=True)
class Media:
    sequence: int
    discontinuity_sequence: int
    segments: list[Segment]


def _attributes(value: str) -> dict[str, str]:
    if not value:
        return {}
    fields = {}
    position = 0
    while True:
        match = _ATTRIBUTE.match(value, position)
        if match is None or match[1] in fields:
            raise HLSError("Thuộc tính trong danh sách HLS không hợp lệ.")
        key, quoted, bare = match.groups()
        fields[key] = quoted if quoted is not None else bare
        position = match.end()
        if position == len(value):
            return fields
        if value[position] != ",":
            raise HLSError("Thuộc tính trong danh sách HLS không hợp lệ.")
        position += 1
        while position < len(value) and value[position] in " \t":
            position += 1


def _number(value: str) -> int:
    if not _NUMBER.fullmatch(value):
        raise HLSError("Danh sách HLS có giá trị số không hợp lệ.")
    return int(value)


def _uri(base: str, value: str) -> str:
    if not value or "{$" in value:
        raise HLSError("Địa chỉ phân đoạn HLS không hợp lệ hoặc dùng biến URI.")
    try:
        return parse_public_url(urljoin(base, value))
    except ValueError:
        raise HLSError("Địa chỉ phân đoạn HLS không hợp lệ.") from None


def _response_status(status: int, expected: int, subject: str) -> None:
    if status in {401, 403, 407}:
        raise HLSError(f"{subject} yêu cầu quyền truy cập (HTTP {status}).")
    if status != expected:
        raise HLSError(f"{subject} trả về HTTP {status}; cần HTTP {expected}.")


def parse_manifest(data: bytes | bytearray) -> list[str]:
    """Decode an HLS manifest and reject unsupported features in its tags."""
    try:
        lines = data.decode("utf-8-sig").strip().split("\n")
    except UnicodeDecodeError:
        raise HLSError("Danh sách HLS có mã hóa văn bản không hợp lệ.") from None
    cleaned = [line.strip() for line in lines if line.strip()]
    if not cleaned or cleaned[0] != "#EXTM3U":
        raise HLSError("Địa chỉ không trả về danh sách phát HLS.")

    tags = cleaned[1:]
    for line in tags:
        tag, _, fields = line.partition(":")
        if tag == "#EXT-X-SESSION-KEY":
            raise HLSError(_ENCRYPTED)
        if tag == "#EXT-X-KEY" and _attributes(fields) != {"METHOD": "NONE"}:
            raise HLSError(_ENCRYPTED)
        if tag in _UNSUPPORTED:
            raise HLSError(f"Danh sách HLS dùng tính năng {tag[1:]} chưa hỗ trợ.")
    return tags


def validate_discovered_hls(data: bytes | bytearray) -> None:
    """Check a source's root manifest without opening any referenced URLs."""
    lines = parse_manifest(data)
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        return
    if "#EXT-X-ENDLIST" not in lines:
        raise HLSError(_LIVE)
    if not any(line.startswith("#EXTINF:") for line in lines):
        raise HLSError("Danh sách HLS không có đủ phân đoạn.")


async def _read_playlist(client: PublicClient, url: str) -> tuple[str, list[str]]:
    async with client.open(url) as response:
        _response_status(response.status_code, 200, "Danh sách HLS")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > _MANIFEST_LIMIT:
                raise HLSError("Danh sách HLS vượt quá 8 MiB.")
            data.extend(chunk)
        base = response.url
    return base, parse_manifest(data)


def _select_variant(base: str, lines: list[str]) -> tuple[str, str | None]:
    variants = []
    audio_groups: dict[str, list[dict[str, str]]] = {}
    pending = None
    for line in lines:
        tag, _, value = line.partition(":")
        if tag == "#EXT-X-MEDIA":
            fields = _attributes(value)
            if fields.get("TYPE") == "AUDIO" and fields.get("GROUP-ID"):
                audio_groups.setdefault(fields["GROUP-ID"], []).append(fields)
        elif tag == "#EXT-X-STREAM-INF":
            if pending is not None:
                raise HLSError("Biến thể HLS thiếu địa chỉ danh sách phát.")
            pending = _attributes(value)
        elif not line.startswith("#"):
            if pending is None:
                raise HLSError("Địa chỉ biến thể HLS nằm sai vị trí.")
            variants.append((pending, line))
            pending = None
    if pending is not None or not variants:
        raise HLSError("Danh sách chính HLS không có đủ địa chỉ biến thể.")

    def quality(variant: tuple[dict[str, str], str]) -> tuple[bool, int, int]:
        fields, _ = variant
        resolution = re.fullmatch(r"([0-9]{1,6})x([0-9]{1,6})", fields.get("RESOLUTION", ""))
        pixels = int(resolution[1]) * int(resolution[2]) if resolution else 0
        codecs = fields.get("CODECS", "").lower()
        audio_only = bool(codecs) and all(
            codec.strip().startswith(("mp4a", "ac-3", "ec-3", "opus", "flac", "alac", "mp3"))
            for codec in codecs.split(",")
        )
        return bool(pixels) or not audio_only, pixels, _number(fields.get("BANDWIDTH", "0"))

    fields, uri = max(variants, key=quality)
    if not quality((fields, uri))[0]:
        raise HLSError("Danh sách HLS chỉ có âm thanh.")

    audio = None
    if "AUDIO" in fields:
        tracks = audio_groups.get(fields["AUDIO"])
        if not tracks:
            raise HLSError("Biến thể HLS thiếu danh sách âm thanh.")
        track = max(tracks, key=lambda item: (item.get("DEFAULT") == "YES", item.get("AUTOSELECT") == "YES"))
        if "URI" in track:
            audio = _uri(base, track["URI"])
    return _uri(base, uri), audio


def _resource(url: str, value: str | None, previous: Resource | None) -> Resource:
    if value is None:
        return Resource(url)
    bounds = _BYTE_RANGE.fullmatch(value)
    if bounds is None or int(bounds[1]) == 0:
        raise HLSError("Phạm vi byte của phân đoạn HLS không hợp lệ.")
    if bounds[2] is not None:
        start = int(bounds[2])
    elif previous and previous.url == url and previous.start is not None and previous.length is not None:
        start = previous.start + previous.length
    else:
        raise HLSError("Phạm vi byte của phân đoạn HLS thiếu vị trí bắt đầu.")
    return Resource(url, start, int(bounds[1]))


def _parse_media(base: str, lines: list[str]) -> Media:
    if "#EXT-X-ENDLIST" not in lines:
        raise HLSError(_LIVE)

    segments = []
    sequence = 0
    discontinuity_sequence = 0
    pending_duration = None
    pending_range = None
    init = None
    discontinuity = False
    for line in lines:
        tag, _, value = line.partition(":")
        if tag == "#EXT-X-STREAM-INF":
            raise HLSError("Biến thể HLS trỏ tới một danh sách chính khác.")
        if tag == "#EXT-X-MEDIA-SEQUENCE":
            sequence = _number(value)
        elif tag == "#EXT-X-DISCONTINUITY-SEQUENCE":
            discontinuity_sequence = _number(value)
        elif tag == "#EXT-X-DISCONTINUITY":
            discontinuity = True
        elif tag == "#EXT-X-MAP":
            fields = _attributes(value)
            init = _resource(_uri(base, fields.get("URI", "")), fields.get("BYTERANGE"), init)
        elif tag == "#EXT-X-BYTERANGE":
            if pending_range is not None:
                raise HLSError("Phân đoạn HLS có nhiều phạm vi byte.")
            pending_range = value
        elif tag == "#EXTINF":
            duration = value.partition(",")[0]
            if pending_duration is not None or not _DURATION.fullmatch(duration):
                raise HLSError("Thời lượng phân đoạn HLS không hợp lệ.")
            pending_duration = duration
        elif not line.startswith("#"):
            if pending_duration is None:
                raise HLSError("Phân đoạn HLS thiếu thời lượng.")
            previous = segments[-1].resource if segments else None
            resource = _resource(_uri(base, line), pending_range, previous)
            segments.append(Segment(pending_duration, resource, init, discontinuity))
            pending_duration = None
            pending_range = None
            discontinuity = False

    if not segments or pending_duration is not None or pending_range is not None:
        raise HLSError("Danh sách HLS không có đủ phân đoạn.")
    return Media(sequence, discontinuity_sequence, segments)


def _known_size(media: Media) -> int | None:
    resources = [segment.resource for segment in media.segments]
    resources.extend({segment.init for segment in media.segments if segment.init is not None})
    lengths = [resource.length for resource in resources]
    if None in lengths:
        return None
    return sum(length for length in lengths if length is not None)


def _segment_suffix(data: bytes, is_init: bool) -> str:
    if data[4:8] in {b"ftyp", b"moov", b"styp", b"moof", b"sidx", b"emsg", b"prft", b"free", b"mdat"}:
        return ".mp4" if is_init else ".m4s"
    if data[:1] == b"\x47" or data[4:5] == b"\x47":
        return ".ts"
    if not is_init:
        # Packed audio can start with an ID3 timestamp and then codec sync bytes.
        if data.startswith(b"ID3") and len(data) >= 10 and all(byte < 128 for byte in data[6:10]):
            tag_size = 0
            for byte in data[6:10]:
                tag_size = (tag_size << 7) | byte
            footer_size = 10 if data[5] & 0x10 else 0
            data = data[10 + tag_size + footer_size:]
        if len(data) >= 6 and data.startswith(b"\x0b\x77"):
            return ".eac3" if data[5] >> 3 > 10 else ".ac3"
        if len(data) >= 2 and data[0] == 0xFF:
            if data[1] & 0xF6 == 0xF0:
                return ".aac"
            if data[1] & 0xE0 == 0xE0:
                return ".mp3"
    raise HLSError("Phân đoạn HLS có định dạng không hỗ trợ hoặc dữ liệu bị lỗi.")


async def _stage_resource(
    client: PublicClient,
    resource: Resource,
    staging: Path,
    stem: str,
    count_bytes: Callable[[int], None],
    *,
    is_init: bool = False,
) -> str:
    headers = None
    if resource.start is not None and resource.length is not None:
        end = resource.start + resource.length - 1
        headers = {"Range": f"bytes={resource.start}-{end}"}
    temp = staging / f"{stem}.tmp"
    expected = resource.length
    received = 0
    probe = bytearray()
    async with client.open(resource.url, headers=headers) as response:
        _response_status(response.status_code, 206 if headers else 200, "Phân đoạn HLS")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise HLSError("Máy chủ nén nội dung phân đoạn HLS bằng định dạng không hỗ trợ.")
        if headers:
            bounds = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
            if (
                bounds is None or int(bounds[1]) != resource.start or int(bounds[2]) != end
                or (bounds[3] != "*" and int(bounds[3]) <= end)
            ):
                raise HLSError("Máy chủ không trả về đúng phạm vi byte của phân đoạn HLS.")
        elif length := response.headers.get("content-length"):
            expected = _number(length)
        with temp.open("xb") as handle:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if expected is not None and received > expected:
                    raise HLSError("Máy chủ trả về nhiều dữ liệu hơn dung lượng phân đoạn đã báo.")
                handle.write(chunk)
                if len(probe) < _PROBE_SIZE:
                    probe.extend(chunk[:_PROBE_SIZE - len(probe)])
                count_bytes(len(chunk))
    if not received or (expected is not None and received != expected):
        raise HLSError("Phân đoạn HLS rỗng hoặc tải chưa đủ dữ liệu.")
    suffix = _segment_suffix(bytes(probe), is_init)
    name = stem + suffix
    temp.rename(staging / name)
    return name


async def _stage_media(
    client: PublicClient,
    media: Media,
    staging: Path,
    prefix: str,
    count_bytes: Callable[[int], None],
) -> str:
    target_duration = max(1, math.ceil(max(float(segment.duration) for segment in media.segments)))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{media.sequence}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{media.discontinuity_sequence}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    maps = {}
    previous_map = None
    for index, segment in enumerate(media.segments, 1):
        if segment.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if segment.init is not None and (segment.init != previous_map or segment.discontinuity):
            if segment.init not in maps:
                maps[segment.init] = await _stage_resource(
                    client, segment.init, staging, f"{prefix}-init-{len(maps) + 1}",
                    count_bytes, is_init=True,
                )
            lines.append(f'#EXT-X-MAP:URI="{maps[segment.init]}"')
        previous_map = segment.init

        name = await _stage_resource(client, segment.resource, staging, f"{prefix}-{index}", count_bytes)
        lines.extend((f"#EXTINF:{segment.duration},", name))
    lines.append("#EXT-X-ENDLIST")
    name = f"{prefix}.m3u8"
    (staging / name).write_text("\n".join(lines) + "\n", encoding="ascii")
    return name


async def _remux(ffmpeg: str, staging: Path, video: str, audio: str | None) -> Path:
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-abort_on", "empty_output+empty_output_stream", "-n",
    ]
    inputs = [video, audio] if audio else [video]
    for name in inputs:
        # The FFmpeg process sees generated file names and supported demuxers only.
        command.extend((
            "-protocol_whitelist", "file",
            "-format_whitelist", f"hls,{_FORMATS}",
            "-seg_format_options", f"format_whitelist={_FORMATS}",
            "-f", "hls", "-i", name,
        ))
    command.extend(("-map", "0:v:0", "-map", "1:a:0" if audio else "0:a:0?"))
    command.extend(("-c", "copy", "-f", "mp4", "combined.mp4"))
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, cwd=staging, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    ))
    process = None
    try:
        process = await asyncio.shield(spawn)
        await process.wait()
    except asyncio.CancelledError:
        if process is None:
            try:
                process = await spawn
            except OSError:
                pass
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        raise
    except OSError:
        raise HLSError("Không khởi động được FFmpeg. Kiểm tra cài đặt và PATH.") from None
    result = staging / "combined.mp4"
    if process.returncode != 0 or not result.is_file() or result.stat().st_size == 0:
        raise HLSError("FFmpeg không thể sao chép luồng HLS sang MP4. Kiểm tra định dạng video và âm thanh.")
    return result


async def download_hls(
    client: PublicClient,
    url: str,
    output: Path,
    progress: Callable[[int, int | None], None],
    ffmpeg: str,
) -> None:
    """Stage validated resources and remux only app-generated local playlists."""
    base, lines = await _read_playlist(client, url)
    audio_url = None
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        video_url, audio_url = _select_variant(base, lines)
        base, lines = await _read_playlist(client, video_url)
    video = _parse_media(base, lines)
    audio = _parse_media(*await _read_playlist(client, audio_url)) if audio_url is not None else None
    sizes = [_known_size(track) for track in (video, audio) if track is not None]
    expected = sum(size for size in sizes if size is not None) if None not in sizes else None

    downloaded = 0

    def count_bytes(amount: int) -> None:
        nonlocal downloaded
        downloaded += amount
        progress(downloaded, expected)

    progress(downloaded, expected)
    with tempfile.TemporaryDirectory(prefix=".hls-", dir=output.parent) as directory:
        staging = Path(directory)
        video_file = await _stage_media(client, video, staging, "video", count_bytes)
        audio_file = await _stage_media(client, audio, staging, "audio", count_bytes) if audio else None
        progress(downloaded, downloaded)
        result = await _remux(ffmpeg, staging, video_file, audio_file)
        result.rename(output)
