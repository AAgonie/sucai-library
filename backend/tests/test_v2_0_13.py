from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from backend.app import db, main, v2, workers


class V2013RegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "test.sqlite3"
        self.original_db_path = db.DB_PATH
        self.original_main_db_path = main.DB_PATH
        db.DB_PATH = self.db_path
        main.DB_PATH = self.db_path
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        main.DB_PATH = self.original_main_db_path
        self.temp_dir.cleanup()

    def add_asset(self, name: str, colour: tuple[int, int, int], ocr_text: str = "") -> int:
        image_path = self.root / f"{name}.jpg"
        Image.new("RGB", (96, 54), colour).save(image_path)
        with db.get_db(self.db_path) as connection:
            return int(
                connection.execute(
                    "INSERT INTO assets(source_path,path_key,library_name,detected_name,resolution,ratio,"
                    "file_size_bytes,file_modified_at,file_size_fingerprint,imported_at,source_type,ocr_text,ocr_status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(image_path),
                        str(image_path).casefold(),
                        name,
                        name,
                        "96 x 54",
                        "16:9",
                        image_path.stat().st_size,
                        "2026-08-03T00:00:00+08:00",
                        str(image_path.stat().st_size),
                        "2026-08-03T00:00:00+08:00",
                        "local",
                        ocr_text,
                        "done" if ocr_text else "pending",
                    ),
                ).lastrowid
            )

    def test_v2_13_migration_adds_visual_hash_fingerprint(self) -> None:
        with db.get_db(self.db_path, readonly=True) as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(asset_visual_hashes)")}
            migrations = {row["version"] for row in connection.execute("SELECT version FROM schema_migrations")}
        self.assertIn("source_fingerprint", columns)
        self.assertIn("V2-13", migrations)

    def test_database_context_releases_connection(self) -> None:
        with db.get_db(self.db_path, readonly=True) as connection:
            connection.execute("SELECT 1").fetchone()
        with self.assertRaisesRegex(Exception, "closed database"):
            connection.execute("SELECT 1").fetchone()

    def test_duplicate_analysis_reuses_valid_cached_hashes(self) -> None:
        first_id = self.add_asset("first", (100, 50, 20), "相同文案测试")
        second_id = self.add_asset("second", (101, 50, 20), "相同文案测试")
        self.add_asset("different-picture", (10, 160, 220), "相同文案测试")
        self.add_asset("different-text", (100, 50, 20), "完全不同内容")

        result = main.run_duplicate_analysis()
        grouped_ids = [{item["id"] for item in group["items"]} for group in result["groups"]]
        self.assertIn({first_id, second_id}, grouped_ids)

        original_visual_signature = main.visual_signature
        try:
            def fail_if_recomputed(_row):
                raise AssertionError("valid visual hash should have been reused")

            main.visual_signature = fail_if_recomputed
            cached_result = main.run_duplicate_analysis()
        finally:
            main.visual_signature = original_visual_signature
        self.assertEqual(len(result["groups"]), len(cached_result["groups"]))

        changed_path = self.root / "first.jpg"
        Image.new("RGB", (96, 54), (220, 10, 10)).save(changed_path)
        stat = changed_path.stat()
        os.utime(changed_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        recomputed: list[int] = []
        try:
            def count_recomputed(row):
                recomputed.append(int(row["id"]))
                return original_visual_signature(row)

            main.visual_signature = count_recomputed
            main.run_duplicate_analysis()
        finally:
            main.visual_signature = original_visual_signature
        self.assertEqual([first_id], recomputed)

    def test_duplicate_analysis_runs_as_persistent_job(self) -> None:
        self.add_asset("queued-first", (100, 50, 20), "相同文案测试")
        self.add_asset("queued-second", (101, 50, 20), "相同文案测试")
        job_id = v2.enqueue("duplicate", payload={"folder": "", "ratio": ""})
        self.assertTrue(workers._acquire_worker_lease("duplicate"))
        workers._run_next("duplicate")
        with db.get_db(self.db_path, readonly=True) as connection:
            job = connection.execute("SELECT status FROM background_jobs WHERE id=?", (job_id,)).fetchone()
            event = connection.execute(
                "SELECT detail FROM job_events WHERE job_id=? AND event_type='completed'", (job_id,)
            ).fetchone()
        self.assertEqual("done", job["status"])
        result = json.loads(event["detail"])
        self.assertEqual(1, len(result["groups"]))

    def test_same_supervisor_takeover_recovers_newer_job_heartbeat(self) -> None:
        old_supervisor_id = workers.SUPERVISOR_ID
        old_worker_id = workers.WORKER_ID
        try:
            workers.SUPERVISOR_ID = "test-supervisor"
            workers.WORKER_ID = "test-supervisor:new-worker"
            with db.get_db(self.db_path) as connection:
                connection.execute(
                    "INSERT INTO job_worker_leases(job_type,owner_id,heartbeat_at) VALUES ('duplicate', ?, ?)",
                    ("test-supervisor:old-worker", "2026-08-03T00:00:00+08:00"),
                )
                job_id = connection.execute(
                    "INSERT INTO background_jobs(job_type,status,payload,created_at,started_at,heartbeat_at,worker_id) "
                    "VALUES ('duplicate','running','{}',?,?,?,?)",
                    (
                        "2026-08-03T00:00:00+08:00",
                        "2026-08-03T00:00:01+08:00",
                        "2026-08-03T00:00:01+08:00",
                        "test-supervisor:old-worker",
                    ),
                ).lastrowid

            self.assertTrue(workers._acquire_worker_lease("duplicate"))
            with db.get_db(self.db_path, readonly=True) as connection:
                job = connection.execute(
                    "SELECT status,worker_id,heartbeat_at FROM background_jobs WHERE id=?", (job_id,)
                ).fetchone()
                lease = connection.execute(
                    "SELECT owner_id FROM job_worker_leases WHERE job_type='duplicate'"
                ).fetchone()
            self.assertEqual("queued", job["status"])
            self.assertEqual("", job["worker_id"])
            self.assertIsNone(job["heartbeat_at"])
            self.assertEqual(workers.WORKER_ID, lease["owner_id"])
        finally:
            workers.SUPERVISOR_ID = old_supervisor_id
            workers.WORKER_ID = old_worker_id

    def test_worker_cannot_touch_job_after_losing_queue_lease(self) -> None:
        old_worker_id = workers.WORKER_ID
        try:
            workers.WORKER_ID = "test-worker"
            with db.get_db(self.db_path) as connection:
                connection.execute(
                    "INSERT INTO job_worker_leases(job_type,owner_id,heartbeat_at) VALUES ('duplicate','replacement-worker',?)",
                    ("2026-08-03T00:00:00+08:00",),
                )
                job_id = connection.execute(
                    "INSERT INTO background_jobs(job_type,status,payload,created_at,started_at,heartbeat_at,worker_id) "
                    "VALUES ('duplicate','running','{}',?,?,?,?)",
                    (
                        "2026-08-03T00:00:00+08:00",
                        "2026-08-03T00:00:00+08:00",
                        "2026-08-03T00:00:00+08:00",
                        workers.WORKER_ID,
                    ),
                ).lastrowid
            with self.assertRaises(workers.JobOwnershipLost):
                workers._touch_job(
                    {"id": job_id, "job_type": "duplicate"},
                    progress_current=1,
                    progress_total=2,
                )
        finally:
            workers.WORKER_ID = old_worker_id

    def test_ocr_text_collection_ignores_paddle_metadata(self) -> None:
        result = {
            "rec_texts": ["热播短剧", "60岁以后的存款"],
            "input_path": "temporary-ocr-input.webp",
            "model_name": "PP-OCRv6_medium_rec",
            "process_type": "general",
        }
        self.assertEqual(["热播短剧", "60岁以后的存款"], main.collect_ocr_text(result))

    def test_ocr_text_collection_finds_nested_text_without_metadata(self) -> None:
        result = {
            "result": [
                {
                    "rec_texts": ["图片标题"],
                    "input_path": "temporary-source.jpg",
                    "mode": "general",
                }
            ],
            "pipeline_name": "OCR",
        }
        self.assertEqual(["图片标题"], main.collect_ocr_text(result))

    def test_restart_exit_code_survives_worker_cleanup_failure(self) -> None:
        original_sleep = main.time.sleep
        original_stop_workers = main.stop_workers
        original_exit = main.os._exit
        received: list[int] = []

        def fail_cleanup() -> None:
            raise RuntimeError("locked")

        def capture_exit(code: int) -> None:
            received.append(code)
            raise SystemExit(code)

        try:
            main.time.sleep = lambda _seconds: None
            main.stop_workers = fail_cleanup
            main.os._exit = capture_exit
            with self.assertRaisesRegex(SystemExit, "75"):
                main.shutdown_current_service_after_delay()
        finally:
            main.time.sleep = original_sleep
            main.stop_workers = original_stop_workers
            main.os._exit = original_exit
        self.assertEqual([main.SUPERVISED_RESTART_EXIT_CODE], received)

    def test_ocr_book_title_hides_and_renames_existing_derivative(self) -> None:
        asset_id = self.add_asset("source", (80, 80, 80))
        derivative_path = self.root / "1-source.jpg"
        Image.new("RGB", (1280, 720), (80, 80, 80)).save(derivative_path)
        with db.get_db(self.db_path) as connection:
            connection.execute(
                "INSERT INTO asset_derivatives(asset_id,kind,output_path,sequence_number,title,author,channel,"
                "source_resolution,source_size_bytes,output_size_bytes,output_resolution,output_format,generated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset_id,
                    "16:9",
                    str(derivative_path),
                    1,
                    "source",
                    "测试作者",
                    "测试渠道",
                    "96 x 54",
                    1,
                    derivative_path.stat().st_size,
                    "1280 x 720",
                    "JPEG",
                    "2026-08-03T00:00:00+08:00",
                    "done",
                ),
            )

        asset, hidden_reason, sync_warning = main.apply_ocr_result(asset_id, "推广《三体》上线")
        renamed_path = self.root / "1-推广三体上线.jpg"
        self.assertTrue(asset.hidden)
        self.assertIn("《三体》", hidden_reason)
        self.assertEqual("", sync_warning)
        self.assertTrue(renamed_path.exists())
        self.assertFalse(derivative_path.exists())
        with db.get_db(self.db_path, readonly=True) as connection:
            derivative = connection.execute(
                "SELECT title,output_path FROM asset_derivatives WHERE asset_id=?", (asset_id,)
            ).fetchone()
        self.assertEqual("推广三体上线", derivative["title"])
        self.assertEqual(str(renamed_path), derivative["output_path"])

    def test_ocr_hide_survives_derivative_name_conflict(self) -> None:
        asset_id = self.add_asset("conflict-source", (80, 80, 80))
        derivative_path = self.root / "2-conflict-source.jpg"
        conflicting_path = self.root / "2-推广三体上线.jpg"
        Image.new("RGB", (1280, 720), (80, 80, 80)).save(derivative_path)
        Image.new("RGB", (1280, 720), (10, 10, 10)).save(conflicting_path)
        with db.get_db(self.db_path) as connection:
            connection.execute(
                "INSERT INTO asset_derivatives(asset_id,kind,output_path,sequence_number,title,author,channel,"
                "source_resolution,source_size_bytes,output_size_bytes,output_resolution,output_format,generated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, "16:9", str(derivative_path), 2, "conflict-source", "作者", "渠道", "96 x 54", 1, derivative_path.stat().st_size, "1280 x 720", "JPEG", "2026-08-03T00:00:00+08:00", "done"),
            )
        asset, hidden_reason, sync_warning = main.apply_ocr_result(asset_id, "推广《三体》上线")
        self.assertTrue(asset.hidden)
        self.assertIn("《三体》", hidden_reason)
        self.assertIn("已存在同名文件", sync_warning)
        self.assertTrue(derivative_path.exists())

    def test_derivative_rename_rolls_back_when_database_update_fails(self) -> None:
        asset_id = self.add_asset("locked-source", (80, 80, 80))
        derivative_path = self.root / "3-locked-source.jpg"
        renamed_path = self.root / "3-new-title.jpg"
        Image.new("RGB", (1280, 720), (80, 80, 80)).save(derivative_path)
        with db.get_db(self.db_path) as connection:
            connection.execute(
                "INSERT INTO asset_derivatives(asset_id,kind,output_path,sequence_number,title,author,channel,"
                "source_resolution,source_size_bytes,output_size_bytes,output_resolution,output_format,generated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, "16:9", str(derivative_path), 3, "locked-source", "作者", "渠道", "96 x 54", 1, derivative_path.stat().st_size, "1280 x 720", "JPEG", "2026-08-03T00:00:00+08:00", "done"),
            )
        blocker = sqlite3.connect(self.db_path)
        original_timeout = db.SQLITE_BUSY_TIMEOUT_MS
        try:
            blocker.execute("BEGIN IMMEDIATE")
            db.SQLITE_BUSY_TIMEOUT_MS = 50
            with self.assertRaises(sqlite3.OperationalError):
                main.sync_derivative_detected_name(asset_id, "new-title")
        finally:
            blocker.rollback()
            blocker.close()
            db.SQLITE_BUSY_TIMEOUT_MS = original_timeout
        self.assertTrue(derivative_path.exists())
        self.assertFalse(renamed_path.exists())

    def test_browser_download_returns_attachment_and_counts_once(self) -> None:
        asset_id = self.add_asset("download", (20, 30, 40))
        response = main.download_asset_file(asset_id, "raw")
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        self.assertIsNotNone(response.background)
        asyncio.run(response.background())
        with db.get_db(self.db_path, readonly=True) as connection:
            count = connection.execute("SELECT downloads FROM assets WHERE id=?", (asset_id,)).fetchone()[0]
        self.assertEqual(1, count)

    def test_remote_download_streams_bounded_chunks_and_closes(self) -> None:
        response = io.BytesIO(b"x" * (2 * 1024 * 1024 + 7))
        recorded: list[int] = []
        original_record = main.record_asset_download
        try:
            main.record_asset_download = recorded.append
            chunks = list(main.stream_remote_download(response, 42))
        finally:
            main.record_asset_download = original_record
        self.assertEqual([1024 * 1024, 1024 * 1024, 7], [len(chunk) for chunk in chunks])
        self.assertTrue(response.closed)
        self.assertEqual([42], recorded)

    def test_duplicate_failures_are_scoped_by_payload(self) -> None:
        with db.get_db(self.db_path) as connection:
            connection.execute(
                "INSERT INTO background_jobs(job_type,status,payload,created_at,error) VALUES ('duplicate','failed',?,?,?)",
                (json.dumps({"folder": "A", "ratio": ""}, ensure_ascii=False), "2026-08-03T00:00:00+08:00", "A failed"),
            )
            connection.execute(
                "INSERT INTO background_jobs(job_type,status,payload,created_at) VALUES ('duplicate','done',?,?)",
                (json.dumps({"folder": "B", "ratio": ""}, ensure_ascii=False), "2026-08-03T00:00:01+08:00"),
            )
        self.assertEqual(1, v2.jobs_status()["failed"])


if __name__ == "__main__":
    unittest.main()
