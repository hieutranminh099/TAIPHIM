import re
import shutil
from asyncio import to_thread
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from .imports import ImportBatches, preview_files, scan_files, source_folder, target_folder
from .jobs import CONCURRENCY, Queue, chosen_directory, clean_title
from .net import NetError, PublicClient, parse_public_url
from .sources import Kind, Source, discover


STATIC = Path(__file__).resolve().parent.parent / "static"
LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", re.IGNORECASE)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "object-src 'none'; frame-ancestors 'none'; form-action 'self'"
)
Link = Annotated[str, StringConstraints(min_length=1, max_length=8192)]


class DiscoverInput(BaseModel):
    links: Annotated[str, StringConstraints(max_length=1_000_000)]


class EpisodeInput(BaseModel):
    number: Annotated[int, Field(ge=1, le=9999)]
    url: Link
    kind: Kind
    label: Annotated[str, StringConstraints(max_length=200)] = ""


class JobInput(BaseModel):
    title: Annotated[str, StringConstraints(max_length=200)] = "Tên phim"
    directory: Annotated[str, StringConstraints(max_length=2048)] = ""
    episodes: list[EpisodeInput] = Field(min_length=1, max_length=250)


class ImportScanInput(BaseModel):
    source_directory: Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class ImportFileInput(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    number: Annotated[int, Field(ge=1, le=9999)]


class ImportBatchInput(ImportScanInput):
    directory: Annotated[str, StringConstraints(max_length=2048)] = ""
    title: Annotated[str, StringConstraints(max_length=200)] = "Tên phim"
    files: list[ImportFileInput] = Field(min_length=1)

    def selections(self) -> list[tuple[str, int]]:
        return [(item.name, item.number) for item in self.files]


def create_app(*, directory: Path | None = None, client=None, ffmpeg: str | None = None) -> FastAPI:
    downloads_directory = Path.home() / "Downloads"
    default_directory = directory or downloads_directory / "Tram Tai Tap Phim"
    queue = Queue(PublicClient() if client is None else client, shutil.which("ffmpeg") if ffmpeg is None else ffmpeg)
    imports = ImportBatches()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await queue.close()
        await imports.close()

    app = FastAPI(
        title="Trạm Tải Tập Phim",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        origin = request.headers.get("origin")
        if not LOOPBACK_HOST.fullmatch(host):
            return JSONResponse({"detail": "Hãy mở ứng dụng bằng địa chỉ 127.0.0.1."}, status_code=403)
        if request.url.path.startswith("/api/"):
            if (origin is not None and origin.lower() != f"http://{host}") or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse({"detail": "Yêu cầu phải gửi từ giao diện cục bộ."}, status_code=403)
            if request.method == "POST":
                kind = request.headers.get("content-type", "").partition(";")[0].strip().lower()
                if kind != "application/json":
                    return JSONResponse({"detail": "Yêu cầu phải có định dạng JSON."}, status_code=415)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        return JSONResponse(
            {"detail": "Dữ liệu chưa hợp lệ hoặc vượt giới hạn. Hãy kiểm tra đường dẫn, tiêu đề và số tập."},
            status_code=422,
        )

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def config():
        return {
            "directory": str(default_directory),
            "downloads_directory": str(downloads_directory),
            "ffmpeg": bool(queue.ffmpeg),
            "concurrency": CONCURRENCY,
        }

    @app.post("/api/discover")
    async def find_episodes(body: DiscoverInput):
        try:
            episodes, messages, title = await discover(queue.client, body.links)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"episodes": [asdict(episode) for episode in episodes], "messages": messages, "title": title}

    @app.get("/api/jobs")
    async def list_jobs():
        return {"jobs": queue.views()}

    @app.post("/api/jobs", status_code=201)
    async def add_jobs(body: JobInput):
        try:
            sources = [Source(item.number, parse_public_url(item.url), item.kind, item.label) for item in body.episodes]
            target = chosen_directory(body.directory, default_directory)
        except (NetError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        queue.add(clean_title(body.title), target, sources)
        return {"jobs": queue.views()}

    def get_job(job_id: str):
        job = queue.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Không tìm thấy tác vụ.")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        job = get_job(job_id)
        try:
            queue.cancel(job)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job": job.view()}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str):
        job = get_job(job_id)
        try:
            queue.retry(job)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job": job.view()}

    @app.post("/api/import/scan")
    def find_import_files(body: ImportScanInput):
        try:
            return {"files": scan_files(body.source_directory)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/import/preview")
    def preview_import(body: ImportBatchInput):
        try:
            source_folder(body.source_directory)
            target = chosen_directory(body.directory, default_directory, create_default=False)
            return {"files": preview_files(target, clean_title(body.title), body.selections())}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/import/batches", status_code=201)
    async def start_import(body: ImportBatchInput):
        try:
            source = await to_thread(source_folder, body.source_directory)
            target = await to_thread(target_folder, body.directory, default_directory)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        batch = imports.add(source, target, clean_title(body.title), body.selections())
        return batch.view()

    @app.get("/api/import/batches/{batch_id}")
    async def import_progress(batch_id: str):
        batch = imports.batches.get(batch_id)
        if batch is None:
            raise HTTPException(404, "Không tìm thấy lô nhập video.")
        return batch.view()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
