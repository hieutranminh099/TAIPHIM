import asyncio
import errno
import os
import re
import tempfile
import time
import unicodedata
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .hls import HLSError, download_hls
from .net import NetError
from .sources import DIRECT_NAMES, HTML_TYPES, MEDIA_PREFIX, Source, media_header, media_type, read_prefix, response_error


CONCURRENCY = 2
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.IGNORECASE)


def clean_title(title: str) -> str:
    clean = INVALID_NAME.sub("_", unicodedata.normalize("NFC", title))
    clean = "".join(char for char in clean if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    clean = clean.strip(" .")[:100].rstrip(" .") or "Tên phim"
    return f"_{clean}" if RESERVED_NAME.match(clean) else clean


def chosen_directory(entered: str, default: Path | None, *, create_default: bool = True) -> Path:
    try:
        raw = entered.strip() or (str(default) if default is not None else "")
        if raw.startswith(("//", "\\\\")) or "\x00" in raw:
            raise ValueError("Hãy chọn một thư mục trên ổ đĩa cục bộ.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("Hãy nhập đường dẫn đầy đủ đến thư mục lưu.")
        is_default = path == default
        path = path.resolve(strict=not is_default)
        if str(path).startswith(("//", "\\\\")):
            raise ValueError("Hãy chọn một thư mục trên ổ đĩa cục bộ.")
        if is_default:
            if create_default:
                path.mkdir(parents=True, exist_ok=True)
            elif not os.path.lexists(path):
                return path
        if not path.is_dir():
            raise ValueError("Đường dẫn đã chọn phải là thư mục.")
        return path
    except (OSError, RuntimeError) as exc:
        raise ValueError("Không mở được thư mục lưu. Hãy kiểm tra đường dẫn và quyền truy cập.") from exc


def destination_names(title: str, number: int, extension: str) -> Iterator[str]:
    stem = f"{title} - Tập {number:02d}"
    for collision in range(1, 10001):
        suffix = "" if collision == 1 else f" ({collision})"
        yield f"{stem}{suffix}.{extension}"
    raise ValueError("Có quá nhiều tệp trùng tên trong thư mục lưu.")


def publish_file(
    temporary: Path, directory: Path, title: str, number: int, extension: str, *, directory_fd: int | None = None
) -> str:
    for filename in destination_names(title, number, extension):
        destination = directory / filename
        try:
            if os.name == "nt":
                # On Windows rename fails when the destination already exists.
                temporary.rename(destination)
            else:
                # A hard link on the same volume publishes the completed file without overwriting.
                os.link(
                    temporary,
                    destination if directory_fd is None else filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            return filename
        except FileExistsError:
            continue


async def download_direct(client, source: Source, temporary: Path, progress) -> None:
    name = DIRECT_NAMES[source.kind]
    async with client.open(source.url) as response:
        if response.status_code != 200:
            raise NetError(response_error(response.status_code))
        content_type = media_type(response.headers.get("content-type", ""))
        if content_type in HTML_TYPES or content_type == "application/json":
            raise NetError(f"Nguồn trả về văn bản thay vì video {name}. Hãy kiểm tra link tải trực tiếp.")
        length = response.headers.get("content-length", "")
        total = int(length) if re.fullmatch(r"[0-9]{1,20}", length.strip()) else None
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise NetError("Nguồn dùng mã hóa truyền tải chưa được hỗ trợ.")

        progress(0, total)
        stream = response.aiter_bytes()
        prefix = await read_prefix(stream, MEDIA_PREFIX if source.kind == "webm" else 8)
        if not media_header(prefix, source.kind):
            raise NetError(f"Nội dung tải về không có định dạng {name}.")
        with temporary.open("xb") as file:
            file.write(prefix)
            downloaded = len(prefix)
            progress(downloaded, total)
            async for chunk in stream:
                file.write(chunk)
                downloaded += len(chunk)
                progress(downloaded, total)
            if total is not None and downloaded != total:
                raise NetError("Kết nối kết thúc trước khi nhận đủ dữ liệu video.")
            file.flush()


@dataclass
class Job:
    id: str
    source: Source
    title: str
    directory: Path
    status: str = "queued"
    downloaded: int = 0
    total: int | None = None
    error: str | None = None
    filename: str | None = None
    speed: int | None = None

    def view(self) -> dict:
        return {
            "id": self.id,
            "number": self.source.number,
            "title": self.title,
            "kind": self.source.kind,
            "status": self.status,
            "downloaded": self.downloaded,
            "total": self.total,
            "error": self.error,
            "filename": self.filename,
            "speed": self.speed,
        }


class Queue:
    def __init__(self, client, ffmpeg: str | None):
        self.client = client
        self.ffmpeg = ffmpeg
        self.jobs: dict[str, Job] = {}
        self.waiting: deque[str] = deque()
        self.active: dict[str, asyncio.Task] = {}
        self.closing = False

    def views(self) -> list[dict]:
        return [job.view() for job in self.jobs.values()]

    def add(self, title: str, directory: Path, sources: list[Source]) -> None:
        for source in sorted(sources, key=lambda item: item.number):
            job = Job(uuid4().hex, source, title, directory)
            self.jobs[job.id] = job
            self.waiting.append(job.id)
        self.dispatch()

    def dispatch(self) -> None:
        while not self.closing and self.waiting and len(self.active) < CONCURRENCY:
            job_id = self.waiting.popleft()
            job = self.jobs[job_id]
            job.status = "running"
            task = asyncio.create_task(self.run(job))
            self.active[job_id] = task
            task.add_done_callback(lambda done, job=job: self.finished(job, done))

    def finished(self, job: Job, task: asyncio.Task) -> None:
        if self.active.get(job.id) is not task:
            return
        self.active.pop(job.id, None)
        if task.cancelled():
            job.status = "cancelled"
            job.speed = None
        self.dispatch()

    def cancel(self, job: Job) -> None:
        if job.status == "queued":
            self.waiting.remove(job.id)
            job.status = "cancelled"
        elif job.status == "running":
            job.status = "cancelling"
            self.active[job.id].cancel()
        elif job.status not in {"cancelling", "cancelled"}:
            raise ValueError("Chỉ có thể hủy tác vụ đang chờ hoặc đang tải.")

    def retry(self, job: Job) -> None:
        if job.status not in {"failed", "cancelled"}:
            raise ValueError("Chỉ có thể thử lại tác vụ đã lỗi hoặc đã hủy.")
        finished_task = self.active.get(job.id)
        if finished_task is not None and finished_task.done():
            self.finished(job, finished_task)
        job.status = "queued"
        job.downloaded = 0
        job.total = job.error = job.filename = job.speed = None
        self.waiting.append(job.id)
        self.dispatch()

    async def close(self) -> None:
        self.closing = True
        self.waiting.clear()
        tasks = list(self.active.values())
        for job_id, task in self.active.items():
            if self.jobs[job_id].status == "running":
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, job: Job) -> None:
        started = time.monotonic()

        def progress(downloaded: int, total: int | None) -> None:
            job.downloaded = downloaded
            job.total = total
            elapsed = time.monotonic() - started
            if downloaded and elapsed > 0.15:
                job.speed = int(downloaded / elapsed)

        try:
            if job.source.kind == "hls" and not self.ffmpeg:
                raise ValueError("Thiếu FFmpeg. Hãy cài FFmpeg, thêm vào PATH rồi khởi động lại ứng dụng.")
            if job.directory.resolve(strict=True) != job.directory:
                raise ValueError("Thư mục lưu đã thay đổi. Hãy chọn lại thư mục.")
            with tempfile.TemporaryDirectory(prefix=".tramtai-", dir=job.directory) as staging:
                temporary = Path(staging) / "video.part"
                if job.source.kind == "hls":
                    await download_hls(self.client, job.source.url, temporary, progress, self.ffmpeg)
                else:
                    await download_direct(self.client, job.source, temporary, progress)
                if job.directory.resolve(strict=True) != job.directory:
                    raise ValueError("Thư mục lưu đã thay đổi. Hãy chọn lại thư mục.")
                extension = "mp4" if job.source.kind == "hls" else job.source.kind
                job.filename = publish_file(temporary, job.directory, job.title, job.source.number, extension)
            job.status = "completed"
        except (NetError, HLSError, ValueError) as exc:
            job.error = str(exc)
            job.status = "failed"
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                job.error = "Ổ đĩa không còn đủ dung lượng để lưu video."
            elif isinstance(exc, PermissionError):
                job.error = "Không ghi được tệp. Hãy kiểm tra quyền thư mục và tệp đang được mở."
            else:
                job.error = "Không ghi hoặc hoàn tất được tệp. Hãy kiểm tra thư mục và ổ đĩa."
            job.status = "failed"
        except Exception:
            job.error = "Đã xảy ra lỗi khi xử lý video. Hãy kiểm tra nguồn và thử lại."
            job.status = "failed"
        finally:
            if job.filename:
                job.error = None
                job.status = "completed"
            job.speed = None
