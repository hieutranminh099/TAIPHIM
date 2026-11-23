import asyncio
import errno
import os
import re
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import uuid4

from .jobs import INVALID_NAME, RESERVED_NAME, chosen_directory, destination_names, publish_file
from .sources import media_header


PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp")
EPISODE_HINT = re.compile(r"(?<![^\W_])(?:tap|episode|ep)[\s._-]*([0-9]{1,4})(?![^\W_])")
READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
COPY_CHUNK_SIZE = 1024 * 1024
STAGE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _episode_hint(name: str) -> int | None:
    text = unicodedata.normalize("NFKD", Path(name).stem.casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    numbers = {int(match) for match in EPISODE_HINT.findall(text)} - {0}
    return numbers.pop() if len(numbers) == 1 else None


def _validate_name(name: str) -> None:
    device = name.partition(".")[0].rstrip(" ")
    if (
        Path(name).suffix.lower() != ".mp4"
        or INVALID_NAME.search(name)
        or RESERVED_NAME.match(device)
        or device.upper() in {"CONIN$", "CONOUT$"}
        or any(unicodedata.category(char) == "Cs" for char in name)
    ):
        raise ValueError("Tên tệp phải là tên MP4 trực tiếp trong thư mục nguồn.")


def _regular(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISREG(info.st_mode) and not (attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True)
class LocalFolder:
    path: Path
    info: os.stat_result

    def check(self) -> None:
        try:
            if self.path.resolve(strict=True) == self.path and os.path.samestat(self.path.stat(), self.info):
                return
        except (OSError, RuntimeError):
            pass
        raise ValueError("Thư mục đã thay đổi hoặc không còn truy cập được. Hãy chọn lại thư mục.")


def source_folder(entered: str) -> LocalFolder:
    try:
        path = chosen_directory(entered, None)
        return LocalFolder(path, path.stat())
    except (ValueError, OSError) as exc:
        raise ValueError("Hãy nhập đường dẫn đầy đủ đến thư mục nguồn cục bộ có thể đọc được.") from exc


def target_folder(entered: str, default: Path) -> LocalFolder:
    try:
        path = chosen_directory(entered, default)
        return LocalFolder(path, path.stat())
    except OSError as exc:
        raise ValueError("Không mở được thư mục lưu. Hãy kiểm tra đường dẫn và quyền truy cập.") from exc


@contextmanager
def _open_directory(folder: LocalFolder):
    folder.check()
    if os.open not in os.supports_dir_fd:
        yield None
        return
    descriptor = os.open(folder.path, READ_FLAGS | os.O_DIRECTORY)
    try:
        if not os.path.samestat(os.fstat(descriptor), folder.info):
            raise ValueError("Thư mục đã thay đổi. Hãy chọn lại thư mục.")
        yield descriptor
    finally:
        os.close(descriptor)


def _has_partial(path: Path, directory_fd: int | None) -> bool:
    for suffix in PARTIAL_SUFFIXES:
        try:
            os.stat(f"{path}{suffix}", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        return True
    return False


def _same_file(info: os.stat_result, opened: os.stat_result) -> bool:
    return _regular(info) and os.path.samestat(info, opened)


@contextmanager
def _open_mp4(folder: LocalFolder, name: str):
    _validate_name(name)
    with _open_directory(folder) as directory_fd:
        path = folder.path / name if directory_fd is None else Path(name)

        def source_stat():
            return os.stat(path, dir_fd=directory_fd, follow_symlinks=False)

        if _has_partial(path, directory_fd):
            raise ValueError("Tệp nguồn vẫn đang tải dở.")
        try:
            before = source_stat()
            if not _regular(before):
                raise ValueError("Tệp nguồn phải là tệp MP4 thông thường trong thư mục đã chọn.")
            descriptor = os.open(path, READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError as exc:
            raise ValueError("Tệp nguồn không còn trong thư mục đã chọn.") from exc
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            folder.check()
            if not _same_file(opened, before) or not _same_file(source_stat(), opened):
                raise ValueError("Tệp nguồn đã thay đổi. Hãy tìm lại tệp.")
            if not media_header(source.read(8), "mp4"):
                raise ValueError("Tệp nguồn chưa hoàn tất hoặc không có định dạng MP4.")
            source.seek(0)
            yield source, opened


def scan_files(entered: str) -> list[dict]:
    folder = source_folder(entered)
    try:
        names = os.listdir(folder.path)
    except OSError as exc:
        raise ValueError("Không đọc được thư mục nguồn. Hãy kiểm tra quyền truy cập.") from exc
    partial_names = set()
    for name in names:
        base, extension = os.path.splitext(name)
        if extension.lower() in PARTIAL_SUFFIXES:
            partial_names.add(os.path.normcase(base))

    files = []
    for name in names:
        if Path(name).suffix.lower() != ".mp4" or os.path.normcase(name) in partial_names:
            continue
        try:
            with _open_mp4(folder, name) as (_, info):
                files.append({"name": name, "size": info.st_size, "episode": _episode_hint(name)})
        except (OSError, ValueError):
            continue
    folder.check()
    return sorted(files, key=lambda item: (item["episode"] or 10000, item["name"].casefold(), item["name"]))


def preview_files(directory: Path, title: str, selections: list[tuple[str, int]]) -> list[dict]:
    reserved = set()
    preview = []
    for name, number in selections:
        _validate_name(name)
        for filename in destination_names(title, number, "mp4"):
            if filename not in reserved and not os.path.lexists(directory / filename):
                reserved.add(filename)
                preview.append({"name": name, "number": number, "filename": filename})
                break
    return preview


@dataclass
class ImportFile:
    name: str
    number: int
    status: str = "queued"
    copied: int = 0
    total: int | None = None
    filename: str | None = None
    error: str | None = None


@dataclass
class ImportBatch:
    id: str
    source: LocalFolder
    target: LocalFolder
    title: str
    files: list[ImportFile]

    def view(self) -> dict:
        return {"id": self.id, "files": [vars(item).copy() for item in self.files]}


def _copy_chunks(source, target, item: ImportFile, stopping: Event) -> None:
    end = item.total or 0
    while item.copied < end:
        if stopping.is_set():
            raise ValueError("Ứng dụng đang đóng.")
        block = source.read(min(COPY_CHUNK_SIZE, end - item.copied))
        if not block:
            break
        target.write(block)
        item.copied += len(block)


def _copy_file(batch: ImportBatch, item: ImportFile, stopping: Event) -> str:
    with _open_mp4(batch.source, item.name) as (source, original):
        item.total = original.st_size
        with _open_directory(batch.target) as directory_fd:
            name = f".tramtai-{uuid4().hex}.part"
            temporary = batch.target.path / name if directory_fd is None else Path(name)
            descriptor = os.open(temporary, STAGE_FLAGS, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    _copy_chunks(source, target, item, stopping)
                batch.source.check()
                path = batch.source.path / item.name
                current = os.fstat(source.fileno())
                if (
                    item.copied != original.st_size
                    or not _same_file(path.lstat(), original)
                    or current.st_size != original.st_size
                    or current.st_mtime_ns != original.st_mtime_ns
                    or _has_partial(path, None)
                ):
                    raise ValueError("Tệp nguồn đã thay đổi trong khi sao chép. Hãy tìm lại tệp.")
                if stopping.is_set():
                    raise ValueError("Ứng dụng đang đóng.")
                batch.target.check()
                return publish_file(
                    temporary, batch.target.path, batch.title, item.number, "mp4", directory_fd=directory_fd
                )
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass


class ImportBatches:
    def __init__(self):
        self.batches: dict[str, ImportBatch] = {}
        self.active: set[asyncio.Task] = set()
        self.stopping = Event()

    def add(self, source: LocalFolder, target: LocalFolder, title: str, selections: list[tuple[str, int]]) -> ImportBatch:
        batch = ImportBatch(uuid4().hex, source, target, title, [ImportFile(*item) for item in selections])
        self.batches[batch.id] = batch
        task = asyncio.create_task(self.run(batch))
        self.active.add(task)
        task.add_done_callback(self.active.discard)
        return batch

    async def close(self) -> None:
        self.stopping.set()
        await asyncio.gather(*self.active, return_exceptions=True)

    async def run(self, batch: ImportBatch) -> None:
        for item in batch.files:
            if self.stopping.is_set():
                return
            item.status = "running"
            try:
                item.filename = await asyncio.to_thread(_copy_file, batch, item, self.stopping)
                item.status = "completed"
            except ValueError as exc:
                item.error = str(exc)
            except FileNotFoundError:
                item.error = "Tệp nguồn hoặc thư mục đã thay đổi trong khi sao chép. Hãy tìm lại tệp."
            except PermissionError:
                item.error = "Không đọc hoặc ghi được tệp. Hãy kiểm tra quyền truy cập và đóng tệp đang mở."
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    item.error = "Ổ đĩa không còn đủ dung lượng để lưu video."
                else:
                    item.error = "Không sao chép hoặc hoàn tất được tệp. Hãy kiểm tra các thư mục và ổ đĩa."
            except Exception:
                item.error = "Đã xảy ra lỗi khi sao chép video. Hãy kiểm tra tệp và thử lại."
            if item.error is not None:
                item.status = "failed"
