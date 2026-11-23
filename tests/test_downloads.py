import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx

from tramtai import net
from tramtai.app import create_app
from tramtai.net import NetError, PublicClient


FIXTURE_ORIGIN = "https://fixture.example"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def make_media(output, *args):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(output)],
        capture_output=True,
        check=True,
    )


class FixtureServer:
    """Serve generated media over HTTP with controllable short reads and errors."""

    def __init__(self, media):
        self.media = media
        self.stalled = {
            "/downloads/tap-01.mp4": asyncio.Event(),
            "/downloads/tap-02.mp4": asyncio.Event(),
        }
        self.requests = []
        self.ranges = []
        self.handlers = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        for signal in self.stalled.values():
            signal.set()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, path, _ = request_line.decode("ascii").split()
            headers = {}
            while (line := await reader.readline()) not in (b"\r\n", b""):
                key, _, value = line.decode("ascii").partition(":")
                headers[key.lower()] = value.strip()
            path = urlsplit(path).path
            first = path not in self.requests
            self.requests.append(path)
            status = "200 OK"
            body = self.media.get(path, b"")
            if path == "/redirect-to-loopback":
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/blocked-resource\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            if method != "GET" or path not in self.media:
                status = "404 Not Found"
            elif path == "/downloads/fail-once.mp4" and first:
                status = "503 Service Unavailable"
                body = b"Temporarily unavailable"
            elif path == "/downloads/interrupted.mp4" and first:
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body[:97]
                )
                await writer.drain()
                return
            content_type = "application/octet-stream"
            if path.endswith(".mp4"):
                content_type = "video/mp4"
            elif path.endswith(".m3u8"):
                content_type = "application/vnd.apple.mpegurl"
            extra = ""
            if "range" in headers and status == "200 OK":
                self.ranges.append((path, headers["range"]))
                start, end = map(int, headers["range"].removeprefix("bytes=").split("-"))
                extra = f"Content-Range: bytes {start}-{end}/{len(body)}\r\n"
                status = "206 Partial Content"
                body = body[start:end + 1]
            length_header = f"Content-Length: {len(body)}\r\n"
            if path == "/downloads/fail-once.mp4" and not first:
                length_header = ""
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
                f"{length_header}{extra}Connection: close\r\n\r\n".encode()
            )
            if first and path in self.stalled:
                writer.write(body[:97])
                await writer.drain()
                await self.stalled[path].wait()
                body = body[97:]
            writer.write(body)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.handlers.discard(task)


class FixtureClient:
    """Connect only the test origin to the real loopback fixture server."""

    def __init__(self, transport):
        self.transport = transport

    @asynccontextmanager
    async def open(self, url, headers=None):
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != FIXTURE_ORIGIN:
            raise NetError("Nguồn ngoài bộ kiểm thử.")
        relative = parts.path
        if parts.query:
            relative += f"?{parts.query}"
        async with self.transport.stream("GET", relative, headers=headers) as response:
            yield FixtureResponse(response, url)


class FixtureResponse:
    def __init__(self, response, url):
        self.response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.url = url

    async def aiter_bytes(self):
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        except httpx.HTTPError as exc:
            raise NetError("Kết nối thử nghiệm bị gián đoạn.") from exc


@unittest.skipUnless(FFMPEG and FFPROBE, "Cần FFmpeg và FFprobe để tạo và kiểm tra video thử.")
class DownloadTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        source = Path(temp.name)
        cls.media = {}
        for number, color in enumerate(("red", "green", "blue"), 1):
            name = f"tap-{number:02}.mp4"
            make_media(
                source / name,
                "-f", "lavfi", "-i", f"color=c={color}:s=128x72:r=15:d=1",
                "-c:v", "mpeg4", "-movflags", "+faststart",
            )
            cls.media[f"/downloads/{name}"] = (source / name).read_bytes()

        cls.media["/downloads/fail-once.mp4"] = cls.media["/downloads/tap-01.mp4"]
        cls.media["/downloads/interrupted.mp4"] = cls.media["/downloads/tap-02.mp4"]
        hls = source / "hls"
        hls.mkdir()
        make_media(
            hls / "video.m3u8",
            "-f", "lavfi", "-i", "testsrc2=s=128x72:r=15:d=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
            "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(hls / "piece%02d.ts"),
        )
        for media_file in hls.iterdir():
            cls.media[f"/hls/{media_file.name}"] = media_file.read_bytes()
        make_media(
            hls / "packed.m3u8",
            "-f", "lavfi", "-i", "testsrc2=s=128x72:r=15:d=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
            "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4", "-hls_flags", "single_file",
            "-hls_segment_filename", str(hls / "packed.m4s"),
        )
        for name in ("packed.m3u8", "packed.m4s"):
            cls.media[f"/hls/{name}"] = (hls / name).read_bytes()
        cls.media["/hls/master.m3u8"] = (
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=80000,RESOLUTION=128x72\nvideo.m3u8\n"
        )
        cls.media["/hls/encrypted.m3u8"] = (
            b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
            b'#EXT-X-KEY:METHOD=AES-128,URI="fixture.key"\n'
            b"#EXTINF:1,\npiece00.ts\n#EXT-X-ENDLIST\n"
        )

    @asynccontextmanager
    async def serve(self):
        with tempfile.TemporaryDirectory() as folder:
            async with FixtureServer(self.media) as fixture:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{fixture.port}", trust_env=False
                ) as fixture_transport:
                    directory = Path(folder)
                    application = create_app(
                        directory=directory, client=FixtureClient(fixture_transport), ffmpeg=FFMPEG
                    )
                    async with application.router.lifespan_context(application):
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=application),
                            base_url="http://127.0.0.1:8765",
                        ) as api:
                            try:
                                yield api, fixture, directory
                            finally:
                                for signal in fixture.stalled.values():
                                    signal.set()

    async def wait_jobs(self, api, condition, timeout=15):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            response = await api.get("/api/jobs")
            self.assertEqual(response.status_code, 200, response.text)
            jobs = response.json()["jobs"]
            if condition(jobs):
                return jobs
            if loop.time() >= deadline:
                self.fail(f"Hết thời gian kiểm tra hàng đợi: {jobs}")
            await asyncio.sleep(0.02)

    async def job_action(self, api, job, action):
        response = await api.post(f"/api/jobs/{job['id']}/{action}", json={})
        self.assertLess(response.status_code, 300, response.text)
        return response.json()["job"]

    async def enqueue(self, api, directory, episodes, title="Phim thử"):
        response = await api.post(
            "/api/jobs", json={"directory": str(directory), "title": title, "episodes": episodes}
        )
        self.assertLess(response.status_code, 300, response.text)
        self.assertEqual(len(response.json()["jobs"]), len(episodes))
        return response.json()["jobs"]

    @staticmethod
    def episode(number, path, kind="mp4"):
        return {"number": number, "url": FIXTURE_ORIGIN + path, "label": f"Tập {number}", "kind": kind}

    async def test_three_mp4_downloads_queue_cancel_retry_and_collision(self):
        async with self.serve() as (api, fixture, directory):
            config = await api.get("/api/config")
            self.assertEqual(config.json()["concurrency"], 2)
            original = directory / "Phim thử - Tập 01.mp4"
            original.write_bytes(b"existing file")
            response = await api.post(
                "/api/discover", json={"links": "\n".join(
                    FIXTURE_ORIGIN + f"/downloads/tap-{number:02}.mp4"
                    for number in (3, 9, 1, 2)
                )},
            )
            self.assertEqual(response.status_code, 200, response.text)
            episodes = response.json()["episodes"]
            self.assertEqual([item["number"] for item in episodes], [1, 2, 3, 9])

            selected = [item for item in episodes if 1 <= item["number"] <= 3]
            await self.enqueue(api, directory, selected)
            jobs = await self.wait_jobs(
                api,
                lambda items: (
                    len(items) == 3
                    and sum(item["status"] == "running" and item["downloaded"] > 0 for item in items) == 2
                ),
            )
            by_episode = {item["number"]: item for item in jobs}
            self.assertEqual(by_episode[3]["status"], "queued")
            self.assertCountEqual(fixture.requests, ["/downloads/tap-01.mp4", "/downloads/tap-02.mp4"])
            for number in (1, 2):
                job = by_episode[number]
                self.assertLess(job["downloaded"], job["total"])
                self.assertEqual(job["total"], len(self.media[f"/downloads/tap-{number:02}.mp4"]))
            self.assertEqual(list(directory.glob("*.mp4")), [original])

            await self.job_action(api, by_episode[2], "cancel")
            await self.wait_jobs(api, lambda items: any(
                item["number"] == 2 and item["status"] == "cancelled" for item in items
            ))
            fixture.stalled["/downloads/tap-01.mp4"].set()
            fixture.stalled["/downloads/tap-02.mp4"].set()
            await self.wait_jobs(api, lambda items: sum(item["status"] == "completed" for item in items) == 2)
            await self.job_action(api, by_episode[2], "retry")
            jobs = await self.wait_jobs(api, lambda items: sum(item["status"] == "completed" for item in items) == 3)

            saved = {original}
            for job in jobs:
                if job["status"] != "completed":
                    continue
                path = directory / job["filename"]
                self.assertEqual(path.parent, directory)
                self.assertNotIn(path, saved)
                saved.add(path)
                data = self.media[f"/downloads/tap-{job['number']:02}.mp4"]
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(job["downloaded"], len(data))
                self.assertEqual(job["total"], len(data))
            self.assertEqual(original.read_bytes(), b"existing file")
            self.assertEqual(set(directory.iterdir()), saved)
            self.assertNotIn("/downloads/tap-09.mp4", fixture.requests)
            self.assertEqual(fixture.requests.count("/downloads/tap-02.mp4"), 2)

    async def test_http_failure_and_interrupted_download_can_be_retried(self):
        async with self.serve() as (api, fixture, directory):
            await self.enqueue(api, directory, [
                self.episode(1, "/downloads/fail-once.mp4"),
                self.episode(2, "/downloads/interrupted.mp4"),
            ], title="../Dự án / Ngoại truyện: Tập đặc biệt")
            failed = await self.wait_jobs(api, lambda items: sum(item["status"] == "failed" for item in items) == 2)
            self.assertTrue(all(item["error"] for item in failed))
            self.assertEqual(list(directory.iterdir()), [])
            for job in failed:
                await self.job_action(api, job, "retry")
            complete = await self.wait_jobs(api, lambda items: sum(item["status"] == "completed" for item in items) == 2)
            outputs = list(directory.glob("*.mp4"))
            self.assertEqual(len(complete), 2)
            self.assertEqual(len(outputs), 2)
            unknown_size = next(item for item in complete if item["number"] == 1)
            self.assertIsNone(unknown_size["total"])
            self.assertEqual(unknown_size["downloaded"], len(self.media["/downloads/fail-once.mp4"]))
            for item in complete:
                filename = item["filename"]
                self.assertEqual((directory / filename).resolve().parent, directory.resolve())
                self.assertFalse(set(filename).intersection('<>:"/\\|?*'))
            self.assertCountEqual([path.read_bytes() for path in outputs], [
                self.media["/downloads/fail-once.mp4"], self.media["/downloads/interrupted.mp4"],
            ])
            self.assertEqual(fixture.requests.count("/downloads/fail-once.mp4"), 2)
            self.assertEqual(fixture.requests.count("/downloads/interrupted.mp4"), 2)

    async def test_clear_hls_remux_and_encrypted_hls_rejection(self):
        async with self.serve() as (api, fixture, directory):
            await self.enqueue(api, directory, [
                self.episode(4, "/hls/master.m3u8", "hls"),
                self.episode(5, "/hls/encrypted.m3u8", "hls"),
                self.episode(6, "/hls/packed.m3u8", "hls"),
            ])
            jobs = await self.wait_jobs(api, lambda items: (
                len(items) == 3 and all(item["status"] in {"completed", "failed"} for item in items)
            ))
            by_episode = {item["number"]: item for item in jobs}
            self.assertEqual(by_episode[5]["status"], "failed", by_episode[5])
            self.assertTrue(by_episode[5]["error"])
            self.assertNotIn("/hls/fixture.key", fixture.requests)
            self.assertIn("/hls/piece00.ts", fixture.requests)
            self.assertIn("/hls/piece01.ts", fixture.requests)
            packed_ranges = [bounds for path, bounds in fixture.ranges if path == "/hls/packed.m4s"]
            self.assertEqual(len(set(packed_ranges)), 3)
            self.assertTrue(packed_ranges[0].startswith("bytes=0-"))
            self.assertEqual(by_episode[6]["downloaded"], len(self.media["/hls/packed.m4s"]))
            outputs = []
            for number in (4, 6):
                completed = by_episode[number]
                self.assertEqual(completed["status"], "completed", completed)
                self.assertGreater(completed["downloaded"], 0)
                output = directory / completed["filename"]
                outputs.append(output)
                probe = await asyncio.to_thread(subprocess.run, [
                    FFPROBE, "-v", "error", "-show_entries", "stream=codec_name,width,height:format=duration",
                    "-of", "json", str(output),
                ], capture_output=True, check=True, text=True)
                result = json.loads(probe.stdout)
                self.assertEqual(result["streams"], [{"codec_name": "h264", "width": 128, "height": 72}])
                self.assertAlmostEqual(float(result["format"]["duration"]), 2.0, places=2)
            self.assertCountEqual(directory.iterdir(), outputs)


class PublicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_local_address_is_rejected_before_request(self):
        client = PublicClient()
        with self.assertRaises(NetError):
            async with client.open("http://127.0.0.1/video.mp4"):
                self.fail("Không được kết nối đến địa chỉ nội bộ.")

    async def test_dns_answer_must_contain_only_public_addresses(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 80)),
        ]
        resolver = AsyncMock(return_value=addresses)
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
            with self.assertRaises(NetError):
                async with PublicClient().open("http://fixture.example/video.mp4"):
                    self.fail("Không được chọn địa chỉ từ kết quả DNS có IP nội bộ.")
        resolver.assert_awaited_once()

    async def test_local_redirect_is_rejected_before_connecting(self):
        async with FixtureServer({}) as fixture:
            resolver = AsyncMock(return_value=("127.0.0.1",))
            # Route just the first test request to the controlled HTTP fixture.
            with patch.object(net, "_resolve_public_addresses", resolver):
                with self.assertRaises(NetError):
                    async with PublicClient().open(
                        f"http://fixture.example:{fixture.port}/redirect-to-loopback"
                    ):
                        self.fail("Không được theo chuyển hướng đến địa chỉ nội bộ.")
            resolver.assert_awaited_once_with("fixture.example", fixture.port)
            self.assertEqual(fixture.requests, ["/redirect-to-loopback"])


if __name__ == "__main__":
    unittest.main()
