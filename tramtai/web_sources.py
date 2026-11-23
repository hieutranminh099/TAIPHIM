"""Read media links explicitly published in a page's visible markup or metadata."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

from .net import NetError, parse_public_url


_MAX_GROUPS = 60
_MAX_CANDIDATES = 5
_MAX_DRAFT_GROUPS = 180
_MEDIA_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".m3u8")
_MEDIA_MIMES = {
    "video/mp4",
    "video/x-m4v",
    "video/quicktime",
    "video/webm",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}
_EPISODE = re.compile(
    r"(?:^|[^\w]|_)(?:(?:tập|tap|episode|ep)(?:\s+số)?[\s._:-]*|s\d{1,3}e)"
    r"([0-9]{1,4})(?=$|[^\w]|_)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MediaLink:
    url: str
    mime: str | None = None


@dataclass
class MediaGroup:
    number: int | None = None
    label: str | None = None
    candidates: list[MediaLink] = field(default_factory=list)


@dataclass
class Page:
    title: str | None
    groups: list[MediaGroup]


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:120]
    return text.encode("utf-8", errors="replace").decode("utf-8") or None


def _number(value: object, *, numeric: bool = False) -> int | None:
    if numeric and type(value) is int:
        return value if 1 <= value <= 9999 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = _EPISODE.search(value)
    digits = match[1] if match else value if numeric and re.fullmatch(r"[0-9]{1,4}", value) else ""
    return (int(digits) or None) if digits else None


def _mime(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    declared = value.split(";", 1)[0].strip().lower()
    return declared if declared in _MEDIA_MIMES else None


def _media_suffix(value: str) -> bool:
    try:
        return unquote(urlsplit(value).path).lower().endswith(_MEDIA_SUFFIXES)
    except (UnicodeError, ValueError):
        return False


def _media_link(raw_url: object, mime: str | None, base: str) -> MediaLink | None:
    if not isinstance(raw_url, str):
        return None
    raw_url = raw_url.strip()
    if not raw_url or raw_url.startswith(("#", "?")) or any(
        ord(char) < 32 or char == "\\" for char in raw_url
    ):
        return None
    try:
        parts = urlsplit(raw_url)
        if parts.scheme and not parts.netloc:
            return None
        resolved = parse_public_url(urljoin(base, raw_url))
    except (NetError, UnicodeError, ValueError):
        return None
    return MediaLink(resolved, mime)


def _add(group: MediaGroup, item: MediaLink | None) -> None:
    if item is None:
        return
    for index, existing in enumerate(group.candidates):
        if existing.url == item.url:
            if existing.mime is None and item.mime is not None:
                group.candidates[index] = item
            return
    if len(group.candidates) < _MAX_CANDIDATES:
        group.candidates.append(item)


def _tag_group(attrs: dict[str, str]) -> MediaGroup:
    label = _text(attrs.get("title")) or _text(attrs.get("aria-label"))
    number = _number(attrs.get("data-episode"), numeric=True) or _number(
        attrs.get("data-episode-number"), numeric=True
    )
    return MediaGroup(number or _number(label), label)


def _add_tag_media(group: MediaGroup, attrs: dict[str, str], base: str) -> None:
    mime = _mime(attrs.get("type"))
    for name in ("src", "data-src"):
        _add(group, _media_link(attrs.get(name), mime, base))


class _Markup(HTMLParser):
    CDATA_CONTENT_ELEMENTS = (*HTMLParser.CDATA_CONTENT_ELEMENTS, "iframe", "xmp", "noembed", "noframes")

    def __init__(self, base: str):
        super().__init__()
        self.base = base
        self.saw_base = False
        self.groups: list[MediaGroup] = []
        self.graphs: list[str] = []
        self.og: list[tuple[str, str]] = []
        self.video: MediaGroup | None = None
        self.anchor: MediaGroup | None = None
        self.anchor_text: list[str] = []
        self.head_title: list[str] = []
        self.in_title = False
        self.in_json_ld = False
        self.template_depth = 0

    def handle_starttag(self, tag, attributes):
        if tag == "template":
            self.template_depth += 1
        if self.template_depth:
            return
        attrs = {name: value or "" for name, value in attributes}
        if tag == "base" and "href" in attrs and not self.saw_base:
            self.saw_base = True
            try:
                self.base = urljoin(self.base, attrs["href"])
            except ValueError:
                pass
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
            if key in {"og:title", "og:video", "og:video:url", "og:video:secure_url", "og:video:type"}:
                if len(self.og) < _MAX_DRAFT_GROUPS:
                    self.og.append((key, attrs.get("content", "").strip()))
        elif tag == "script":
            media_type = attrs.get("type", "").split(";", 1)[0].strip().lower()
            self.in_json_ld = media_type == "application/ld+json" and len(self.graphs) < 24
        elif tag == "video":
            self.video = None
            if len(self.groups) < _MAX_DRAFT_GROUPS:
                self.video = _tag_group(attrs)
                _add_tag_media(self.video, attrs, self.base)
                self.groups.append(self.video)
        elif tag == "source" and self.video is not None:
            _add_tag_media(self.video, attrs, self.base)
        elif tag == "a":
            self.handle_endtag("a")
            href = attrs.get("href", "")
            mime = _mime(attrs.get("type"))
            if len(self.groups) >= _MAX_DRAFT_GROUPS or not (
                mime or _media_suffix(href) or _media_suffix(attrs.get("download", ""))
            ):
                return
            link = _media_link(href, mime, self.base)
            if link:
                self.anchor = _tag_group(attrs)
                _add(self.anchor, link)

    def handle_data(self, data):
        if self.template_depth:
            return
        if self.in_json_ld:
            self.graphs.append(data)
        elif self.in_title and len(self.head_title) < 8:
            self.head_title.append(data[:120])
        elif self.anchor is not None and len(self.anchor_text) < 8:
            self.anchor_text.append(data[:120])

    def handle_endtag(self, tag):
        if self.template_depth:
            if tag == "template":
                self.template_depth -= 1
            return
        if tag == "video":
            self.video = None
        elif tag == "title":
            self.in_title = False
        elif tag == "script":
            self.in_json_ld = False
        elif tag == "a" and self.anchor is not None:
            label = _text(" ".join(self.anchor_text))
            self.anchor.label = self.anchor.label or label
            self.anchor.number = self.anchor.number or _number(label, numeric=True)
            self.groups.append(self.anchor)
            self.anchor = None
            self.anchor_text.clear()


def _items(value: object) -> list:
    return value[:120] if isinstance(value, list) else [value]


def _schema_types(node: dict) -> set[str]:
    types = set()
    for value in _items(node.get("@type")):
        if isinstance(value, str):
            for prefix in ("https://schema.org/", "http://schema.org/", "schema:"):
                value = value.removeprefix(prefix)
            types.add(value)
    return types


def _video_metadata(group: MediaGroup, items: list, base: str) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        for entry in [item, *_items(item.get("encoding"))]:
            if not isinstance(entry, dict):
                continue
            declared = entry.get("encodingFormat")
            mime = _mime(declared)
            for url in _items(entry.get("contentUrl")):
                if isinstance(url, str) and (
                    mime or not declared or _media_suffix(url)
                ):
                    _add(group, _media_link(url, mime, base))
                    if len(group.candidates) >= _MAX_CANDIDATES:
                        return


def _structured(graphs: list[str], base: str) -> tuple[str | None, list[MediaGroup]]:
    title = None
    groups: list[MediaGroup] = []
    for graph in graphs:
        try:
            pending = [json.loads(graph)]
        except (ValueError, RecursionError):
            continue
        for _ in range(600):
            if not pending or len(groups) >= _MAX_DRAFT_GROUPS:
                break
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(reversed(node[:120]))
                continue
            if not isinstance(node, dict):
                continue
            types = _schema_types(node)
            label = _text(node.get("name"))
            if "TVEpisode" in types:
                series = node.get("partOfSeries")
                if not isinstance(series, dict):
                    season = node.get("partOfSeason")
                    series = season.get("partOfSeries") if isinstance(season, dict) else None
                if title is None and isinstance(series, dict):
                    title = _text(series.get("name"))
                group = MediaGroup(_number(node.get("episodeNumber"), numeric=True), label)
                _video_metadata(
                    group, [*_items(node.get("video")), *_items(node.get("encoding"))], base
                )
                if group.candidates:
                    groups.append(group)
            elif "VideoObject" in types:
                group = MediaGroup(_number(label), label)
                _video_metadata(group, [node], base)
                if group.candidates:
                    groups.append(group)
            elif "Movie" in types or "TVSeries" in types:
                title = title or label
                pending.extend(reversed(_items(node.get("video"))))
            for name in ("@graph", "mainEntity", "itemListElement", "item", "episode"):
                if node.get(name) is not None:
                    pending.append(node[name])
    return title, groups


def _open_graph(entries: list[tuple[str, str]], base: str) -> list[MediaGroup]:
    groups: list[MediaGroup] = []
    urls: list[str] = []
    mime = None

    def finish():
        if mime:
            group = MediaGroup()
            for url in urls:
                _add(group, _media_link(url, mime, base))
            if group.candidates:
                groups.append(group)

    for key, value in entries:
        if not value:
            continue
        if key in {"og:video", "og:video:url"}:
            if not urls or value != urls[0]:
                finish()
                urls = [value]
                mime = None
        elif key == "og:video:secure_url" and urls:
            if len(urls) < _MAX_CANDIDATES:
                urls.append(value)
        elif key == "og:video:type" and urls:
            mime = _mime(value)
    finish()
    return groups


def _merged(groups: list[MediaGroup]) -> list[MediaGroup]:
    result: list[MediaGroup] = []
    for group in groups:
        if not group.candidates:
            continue
        if group.number is None:
            for candidate in group.candidates:
                group.number = _number(unquote(urlsplit(candidate.url).path))
                if group.number:
                    break
        urls = {candidate.url for candidate in group.candidates}
        matches = [
            existing
            for existing in result
            if (group.number is not None and existing.number == group.number)
            or any(candidate.url in urls for candidate in existing.candidates)
        ]
        if not matches:
            result.append(group)
            continue
        first, *duplicates = matches
        for entry in [group, *duplicates]:
            first.number = first.number or entry.number
            first.label = first.label or entry.label
            for candidate in entry.candidates:
                _add(first, candidate)
        for duplicate in duplicates:
            result.remove(duplicate)
    return result[:_MAX_GROUPS]


def parse_page(html: str, base: str) -> Page:
    """Extract bounded groups of publicly declared media URLs from one HTML response."""
    markup = _Markup(base)
    markup.feed(html)
    markup.close()
    markup.handle_endtag("a")
    structured_title, structured_groups = _structured(markup.graphs, markup.base)
    og_title = next((_text(value) for key, value in markup.og if key == "og:title"), None)
    title = structured_title or og_title or _text(" ".join(markup.head_title))
    groups = _merged([
        *structured_groups, *markup.groups, *_open_graph(markup.og, markup.base)
    ])
    return Page(title, groups)
