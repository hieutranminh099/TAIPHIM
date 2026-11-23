import asyncio
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

import httpx

from tramtai.app import create_app


FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "Cần FFmpeg để tạo các video MP4 thử.")
class ImportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        fixtures = Path(temporary.name)
        cls.media = {}
        for index, (name, color) in enumerate((
            ("Phim Tập 03.mp4", "red"),
            ("Phim tap-02.mp4", "green"),
            ("Phim EP01.MP4", "blue"),
            ("Video 1080p.mp4", "yellow"),
        )):
            path = fixtures / f"clip-{index}.mp4"
            subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                 f"color=c={color}:s=128x72:r=15:d=1", "-c:v", "mpeg4", "-movflags", "+faststart", str(path)],
                capture_output=True, check=True,
            )
            cls.media[name] = path.read_bytes()

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "Tải xuống Cốc Cốc"
        self.destination = self.root / "Thư mục phim"
        self.default = self.root / "Downloads" / "Tram Tai Tap Phim"
        self.source.mkdir()
        self.destination.mkdir()
        for name, data in self.media.items():
            (self.source / name).write_bytes(data)

        application = create_app(directory=self.default, client=object(), ffmpeg=FFMPEG)
        stack = AsyncExitStack()
        self.addAsyncCleanup(stack.aclose)
        await stack.enter_async_context(application.router.lifespan_context(application))
        self.api = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1:8765",
        ))

    async def scan(self):
        response = await self.api.post("/api/import/scan", json={"source_directory": str(self.source)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["files"]

    def plan(self, picks, title="Ngôi Nhà Trên Đồi", directory=None):
        return {
            "source_directory": str(self.source),
            "directory": str(self.destination if directory is None else directory),
            "title": title,
            "files": [{"name": name, "number": number} for name, number in picks],
        }

    async def preview(self, plan):
        response = await self.api.post("/api/import/preview", json=plan)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["files"]

    async def start(self, plan):
        response = await self.api.post("/api/import/batches", json=plan)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def wait_for_batch(self, batch_id):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while True:
            response = await self.api.get(f"/api/import/batches/{batch_id}")
            self.assertEqual(response.status_code, 200, response.text)
            items = response.json()["files"]
            if all(item["status"] in {"completed", "failed"} for item in items):
                return items
            if loop.time() >= deadline:
                self.fail(f"Hết thời gian chờ nhập: {items}")
            await asyncio.sleep(0.02)

    def verify_copy(self, item, output, original):
        self.assertEqual(item["status"], "completed", item)
        self.assertEqual(item["filename"], output.name)
        self.assertEqual(item["copied"], len(original))
        self.assertEqual(item["total"], len(original))
        self.assertIsNone(item["error"])
        self.assertEqual(output.read_bytes(), original)

    async def test_scans_completed_files_and_imports_selected_three_in_edited_order(self):
        data = self.media["Phim EP01.MP4"]
        for temporary_name in ("video.mp4.crdownload", "video.mp4.PART", "video.MP4.tmp"):
            (self.source / temporary_name).write_bytes(data)
        (self.source / "Còn tải.mp4").write_bytes(data)
        (self.source / "Còn tải.mp4.CRDOWNLOAD").touch()
        (self.source / "video.mov").write_bytes(data)
        (self.source / "video rỗng.mp4").touch()
        (self.source / "văn bản.mp4").write_bytes(b"not an MP4")
        nested = self.source / "Không phải tệp.mp4"
        nested.mkdir()
        (nested / "Tập 04.mp4").write_bytes(data)

        found = await self.scan()
        self.assertCountEqual(
            [(file["name"], file["size"], file["episode"]) for file in found],
            [(name, len(self.media[name]), episode) for name, episode in (
                ("Phim Tập 03.mp4", 3), ("Phim tap-02.mp4", 2),
                ("Phim EP01.MP4", 1), ("Video 1080p.mp4", None),
            )],
        )

        original = self.destination / "Ngôi Nhà Trên Đồi - Tập 05.mp4"
        prior_copy = self.destination / "Ngôi Nhà Trên Đồi - Tập 05 (2).mp4"
        original.write_bytes(b"original")
        prior_copy.write_bytes(b"prior copy")
        picks = [("Phim Tập 03.mp4", 5), ("Phim EP01.MP4", 6), ("Phim tap-02.mp4", 7)]
        plan = self.plan(picks)
        filenames = [
            "Ngôi Nhà Trên Đồi - Tập 05 (3).mp4",
            "Ngôi Nhà Trên Đồi - Tập 06.mp4",
            "Ngôi Nhà Trên Đồi - Tập 07.mp4",
        ]
        preview = await self.preview(plan)
        self.assertEqual(
            [(item["name"], item["number"], item["filename"]) for item in preview],
            [(name, number, filename) for (name, number), filename in zip(picks, filenames)],
        )
        self.assertCountEqual(self.destination.iterdir(), [original, prior_copy])

        copied = await self.wait_for_batch(await self.start(plan))
        self.assertEqual([(item["name"], item["number"]) for item in copied], picks)
        outputs = [self.destination / filename for filename in filenames]
        for item, output in zip(copied, outputs):
            self.verify_copy(item, output, self.media[item["name"]])
        self.assertEqual(original.read_bytes(), b"original")
        self.assertEqual(prior_copy.read_bytes(), b"prior copy")
        self.assertCountEqual(self.destination.iterdir(), [original, prior_copy, *outputs])
        for name, data in self.media.items():
            self.assertEqual((self.source / name).read_bytes(), data)
        self.assertEqual((await self.api.get("/api/jobs")).json()["jobs"], [])

    async def test_disappeared_file_fails_one_item_and_same_directory_keeps_sources(self):
        first, missing, last = "Đồi Mây - Tập 05.mp4", "Phim tap-02.mp4", "Phim EP01.MP4"
        original = self.source / first
        (self.source / "Phim Tập 03.mp4").rename(original)
        self.assertIn(missing, [file["name"] for file in await self.scan()])
        plan = self.plan([(first, 5), (missing, 6), (last, 7)], title="Đồi Mây", directory=self.source)
        await self.preview(plan)
        (self.source / missing).unlink()
        before_import = set(self.source.iterdir())

        copied = await self.wait_for_batch(await self.start(plan))
        self.assertEqual([item["status"] for item in copied], ["completed", "failed", "completed"])
        first_copy = self.source / "Đồi Mây - Tập 05 (2).mp4"
        last_copy = self.source / "Đồi Mây - Tập 07.mp4"
        self.verify_copy(copied[0], first_copy, self.media["Phim Tập 03.mp4"])
        self.assertTrue(copied[1]["error"])
        self.assertIsNone(copied[1]["filename"])
        self.verify_copy(copied[2], last_copy, self.media[last])
        self.assertEqual(original.read_bytes(), self.media["Phim Tập 03.mp4"])
        self.assertCountEqual(self.source.iterdir(), [*before_import, first_copy, last_copy])

    async def test_default_save_directory_is_available_for_import(self):
        config = (await self.api.get("/api/config")).json()
        self.assertEqual(config["directory"], str(self.default))
        self.assertEqual(config["downloads_directory"], str(Path.home() / "Downloads"))
        name = "Video 1080p.mp4"
        plan = self.plan([(name, 1)], title="Thử: Tên phim", directory="")
        await self.preview(plan)
        self.assertFalse(self.default.exists())
        copied = await self.wait_for_batch(await self.start(plan))
        self.verify_copy(copied[0], self.default / "Thử_ Tên phim - Tập 01.mp4", self.media[name])

    async def test_rejects_invalid_paths_and_does_not_import_an_outside_file(self):
        source_file = self.source / "Phim EP01.MP4"
        response = await self.api.post(
            "/api/import/scan",
            json={"source_directory": str(self.source)},
            headers={"Origin": "https://fixture.invalid"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        for source in ("tương đối", str(source_file), str(self.root / "không tồn tại")):
            response = await self.api.post("/api/import/scan", json={"source_directory": source})
            self.assertEqual(response.status_code, 400, response.text)
        for target in ("tương đối", source_file, self.root / "không tồn tại"):
            response = await self.api.post("/api/import/batches", json=self.plan([(source_file.name, 1)], directory=target))
            self.assertEqual(response.status_code, 400, response.text)

        outside = self.root / "Tập 09.mp4"
        outside.write_bytes(self.media[source_file.name])
        for name in ("../Tập 09.mp4", "..\\Tập 09.mp4", str(outside)):
            response = await self.api.post("/api/import/batches", json=self.plan([(name, 1)]))
            if response.status_code == 201:
                items = await self.wait_for_batch(response.json()["id"])
                self.assertEqual(items[0]["status"], "failed", items[0])
                self.assertTrue(items[0]["error"])
            else:
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(outside.read_bytes(), self.media[source_file.name])

    async def test_ignores_linked_source_and_preserves_existing_destination_link(self):
        name = "Phim EP01.MP4"
        outside = self.root / "Tệp bên ngoài.mp4"
        outside.write_bytes(self.media[name])
        linked_source = self.source / "Liên kết EP09.mp4"
        linked_destination = self.destination / "Ngôi Nhà Trên Đồi - Tập 01.mp4"
        try:
            linked_source.symlink_to(outside)
            linked_destination.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Môi trường không cho phép tạo liên kết tượng trưng.")
        self.assertNotIn(linked_source.name, [file["name"] for file in await self.scan()])

        plan = self.plan([(linked_source.name, 9), (name, 1)])
        copied = await self.wait_for_batch(await self.start(plan))
        self.assertEqual(copied[0]["status"], "failed", copied[0])
        output = self.destination / "Ngôi Nhà Trên Đồi - Tập 01 (2).mp4"
        self.verify_copy(copied[1], output, self.media[name])
        self.assertTrue(linked_destination.is_symlink())
        self.assertEqual(outside.read_bytes(), self.media[name])
        self.assertCountEqual(self.destination.iterdir(), [linked_destination, output])

    async def test_progress_and_cleanup_after_mid_copy_error(self):
        from tramtai.imports import _copy_chunks

        started = threading.Event()
        resume = threading.Event()
        first, second = "Phim EP01.MP4", "Phim tap-02.mp4"

        def copy_with_first_failure(source, target, item, stopping):
            if item.number != 5:
                return _copy_chunks(source, target, item, stopping)
            item.copied = target.write(source.read(16))
            started.set()
            if not resume.wait(10):
                raise RuntimeError("Hết thời gian chờ kiểm tra tiến độ.")
            raise OSError("Lỗi ghi tệp giả lập.")

        plan = self.plan([(first, 5), (second, 6)])
        with patch("tramtai.imports._copy_chunks", copy_with_first_failure):
            try:
                batch_id = await self.start(plan)
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                response = await self.api.get(f"/api/import/batches/{batch_id}")
                self.assertEqual(response.status_code, 200, response.text)
                running = response.json()["files"][0]
                self.assertEqual(running["status"], "running", running)
                self.assertEqual(running["copied"], 16)
                self.assertEqual(running["total"], len(self.media[first]))
                self.assertFalse((self.destination / "Ngôi Nhà Trên Đồi - Tập 05.mp4").exists())
            finally:
                resume.set()
            copied = await self.wait_for_batch(batch_id)

        self.assertEqual(copied[0]["status"], "failed", copied[0])
        self.assertIsNone(copied[0]["filename"])
        self.assertTrue(copied[0]["error"])
        output = self.destination / "Ngôi Nhà Trên Đồi - Tập 06.mp4"
        self.verify_copy(copied[1], output, self.media[second])
        self.assertEqual(list(self.destination.iterdir()), [output])
        self.assertEqual((self.source / first).read_bytes(), self.media[first])

    async def test_overlapping_batches_keep_both_copies(self):
        names = ["Phim EP01.MP4", "Phim tap-02.mp4"]
        batch_ids = await asyncio.gather(*(self.start(self.plan([(name, 5)])) for name in names))
        first, second = await asyncio.gather(*(self.wait_for_batch(batch_id) for batch_id in batch_ids))
        outputs = [self.destination / item["filename"] for item in (first[0], second[0])]
        self.assertCountEqual([path.name for path in outputs], [
            "Ngôi Nhà Trên Đồi - Tập 05.mp4", "Ngôi Nhà Trên Đồi - Tập 05 (2).mp4",
        ])
        self.verify_copy(first[0], outputs[0], self.media[names[0]])
        self.verify_copy(second[0], outputs[1], self.media[names[1]])
        self.assertCountEqual(self.destination.iterdir(), outputs)
