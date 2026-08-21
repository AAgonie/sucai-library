from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .db import get_db, normalize_path_key, transaction, utc_now_text
from .image_metadata import IMAGE_EXTENSIONS, read_image_size
from .v2 import enqueue

LEASE_TIMEOUT_SECONDS = 300
SUPERVISOR_ID = os.environ.get("PHOTO_WEB_SUPERVISOR_ID", "").strip()
WORKER_ID = f"{SUPERVISOR_ID}:{os.getpid()}-{uuid.uuid4().hex}" if SUPERVISOR_ID else f"{os.getpid()}-{uuid.uuid4().hex}"
_started = False
_stop_requested = threading.Event()


class JobOwnershipLost(RuntimeError):
    """Raised when a different process safely takes over a stale job."""


class JobCancelled(RuntimeError):
    """Raised after a worker observes a cooperative cancellation request."""


def start_workers() -> None:
    global _started
    if _started:
        return
    _started = True
    _stop_requested.clear()
    # main.py normally sets this before PaddleOCR is imported.  Keep a safe
    # fallback for worker-only callers while allowing deployment overrides.
    default_threads = str(min(12, os.cpu_count() or 12))
    os.environ.setdefault("OMP_NUM_THREADS", default_threads)
    os.environ.setdefault("MKL_NUM_THREADS", default_threads)
    for job_type in ("scan", "ocr", "duplicate"):
        threading.Thread(target=_worker, args=(job_type,), name=f"photo-{job_type}-worker", daemon=True).start()


def _worker(job_type: str) -> None:
    while not _stop_requested.is_set():
        try:
            if _acquire_worker_lease(job_type):
                if job_type == "scan":
                    _schedule_scans()
                _run_next(job_type)
        except Exception:
            pass
        _stop_requested.wait(2)


def stop_workers() -> None:
    """Release queue ownership so a normal service restart resumes immediately."""
    _stop_requested.set()
    with get_db() as conn:
        with transaction(conn):
            cancelled = conn.execute(
                "SELECT id,asset_id FROM background_jobs WHERE status='cancel_requested' AND worker_id=?",
                (WORKER_ID,),
            ).fetchall()
            conn.execute(
                "UPDATE background_jobs SET status='cancelled',finished_at=?,heartbeat_at=?,current_path='' "
                "WHERE status='cancel_requested' AND worker_id=?",
                (utc_now_text(), utc_now_text(), WORKER_ID),
            )
            for job in cancelled:
                conn.execute(
                    "INSERT INTO job_events(job_id,event_type,message,asset_id,created_at) VALUES (?, 'cancelled', ?, ?, ?)",
                    (job["id"], "服务停止时完成取消。", job["asset_id"], utc_now_text()),
                )
            conn.execute(
                "UPDATE background_jobs SET status='queued',worker_id='',heartbeat_at=NULL,started_at=NULL,current_path='' "
                "WHERE status='running' AND worker_id=?",
                (WORKER_ID,),
            )
            conn.execute("DELETE FROM job_worker_leases WHERE owner_id=?", (WORKER_ID,))


def _is_stale(timestamp: str | None) -> bool:
    if not timestamp:
        return True
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(timestamp)).total_seconds() > LEASE_TIMEOUT_SECONDS
    except ValueError:
        return True


def _acquire_worker_lease(job_type: str) -> bool:
    """Let one server process drain each queue, with stale-owner recovery."""
    now = utc_now_text()
    with get_db() as conn:
        with transaction(conn):
            lease = conn.execute(
                "SELECT owner_id,heartbeat_at FROM job_worker_leases WHERE job_type=?", (job_type,)
            ).fetchone()
            if not lease:
                conn.execute(
                    "INSERT INTO job_worker_leases(job_type,owner_id,heartbeat_at) VALUES (?,?,?)",
                    (job_type, WORKER_ID, now),
                )
                return True
            if lease["owner_id"] == WORKER_ID:
                conn.execute(
                    "UPDATE job_worker_leases SET heartbeat_at=? WHERE job_type=? AND owner_id=?",
                    (now, job_type, WORKER_ID),
                )
                return True
            same_supervisor = bool(SUPERVISOR_ID and str(lease["owner_id"]).startswith(f"{SUPERVISOR_ID}:"))
            if not same_supervisor and not _is_stale(lease["heartbeat_at"]):
                return False

            previous_owner = lease["owner_id"]
            conn.execute(
                "UPDATE job_worker_leases SET owner_id=?,heartbeat_at=? WHERE job_type=?",
                (WORKER_ID, now, job_type),
            )
            conn.execute(
                "UPDATE background_jobs SET status='queued',worker_id='',heartbeat_at=NULL,started_at=NULL "
                "WHERE job_type=? AND status='running' AND worker_id=?",
                (job_type, previous_owner),
            )
            stale_cancelled = conn.execute(
                "SELECT id,asset_id FROM background_jobs WHERE job_type=? AND status='cancel_requested' "
                "AND worker_id=?",
                (job_type, previous_owner),
            ).fetchall()
            conn.execute(
                "UPDATE background_jobs SET status='cancelled',finished_at=?,heartbeat_at=?,current_path='' "
                "WHERE job_type=? AND status='cancel_requested' AND worker_id=?",
                (now, now, job_type, previous_owner),
            )
            for job in stale_cancelled:
                conn.execute(
                    "INSERT INTO job_events(job_id,event_type,message,asset_id,created_at) VALUES (?, 'cancelled', ?, ?, ?)",
                    (job["id"], "失联工作者的停止请求已完成。", job["asset_id"], now),
                )
            return True


def _schedule_scans() -> None:
    with get_db() as conn:
        sources = conn.execute(
            "SELECT s.*,(SELECT MAX(j.created_at) FROM background_jobs j "
            "WHERE j.data_source_id=s.id AND j.job_type='scan') AS last_job_at "
            "FROM data_sources s WHERE s.source_type='smb' AND s.enabled=1"
        ).fetchall()
    now = datetime.now().astimezone()
    for source in sources:
        timestamps = [datetime.fromisoformat(value) for value in (source["last_scanned_at"], source["last_job_at"]) if value]
        last = max(timestamps) if timestamps else None
        if not last or (now - last).total_seconds() >= source["scan_interval_minutes"] * 60:
            enqueue("scan", source_id=source["id"])


def _touch_job(job: dict, **fields: object) -> None:
    assignments = [f"{name}=?" for name in fields]
    values = list(fields.values())
    assignments.append("heartbeat_at=?")
    values.append(utc_now_text())
    updated = False
    cancelled = False
    with get_db() as conn:
        with transaction(conn):
            current = conn.execute(
                "SELECT status FROM background_jobs WHERE id=? AND worker_id=?", (job["id"], WORKER_ID)
            ).fetchone()
            owns_lease = conn.execute(
                "SELECT 1 FROM job_worker_leases WHERE job_type=? AND owner_id=?",
                (job["job_type"], WORKER_ID),
            ).fetchone()
            if not owns_lease:
                current = None
            if current and current["status"] == "cancel_requested":
                cancelled = bool(conn.execute(
                    "UPDATE background_jobs SET status='cancelled',finished_at=?,heartbeat_at=?,current_path='' "
                    "WHERE id=? AND status='cancel_requested' AND worker_id=?",
                    (utc_now_text(), utc_now_text(), job["id"], WORKER_ID),
                ).rowcount)
                if cancelled:
                    conn.execute(
                        "INSERT INTO job_events(job_id,event_type,message,asset_id,created_at) "
                        "VALUES (?, 'cancelled', ?, ?, ?)",
                        (job["id"], "任务已按请求停止。", job["asset_id"], utc_now_text()),
                    )
            elif current and current["status"] == "running":
                updated = conn.execute(
                    f"UPDATE background_jobs SET {','.join(assignments)} WHERE id=? AND status='running' AND worker_id=?",
                    (*values, job["id"], WORKER_ID),
                ).rowcount
            if updated:
                conn.execute(
                    "UPDATE job_worker_leases SET heartbeat_at=? WHERE job_type=? AND owner_id=?",
                    (utc_now_text(), job["job_type"], WORKER_ID),
                )
    if cancelled:
        raise JobCancelled()
    if not updated:
        raise JobOwnershipLost()


def _run_next(job_type: str) -> None:
    with get_db() as conn:
        with transaction(conn):
            job = conn.execute(
                "SELECT * FROM background_jobs WHERE job_type=? AND status='queued' ORDER BY id LIMIT 1", (job_type,)
            ).fetchone()
            if not job:
                return
            now = utc_now_text()
            claimed = conn.execute(
                "UPDATE background_jobs SET status='running',started_at=?,heartbeat_at=?,worker_id=?,attempts=attempts+1,error='' "
                "WHERE id=? AND status='queued'",
                (now, now, WORKER_ID, job["id"]),
            ).rowcount
            if not claimed:
                return

    job = dict(job)
    job["worker_id"] = WORKER_ID
    try:
        if job_type == "scan":
            result = _scan(job)
            message = _scan_result_message(result)
        elif job_type == "ocr":
            result = _ocr(job)
            message = str(result.get("message", ""))
        elif job_type == "duplicate":
            result = _duplicate(job)
            message = str(result.get("message", ""))
        else:
            raise RuntimeError(f"不支持的后台任务类型：{job_type}")
        detail = result
        with get_db() as conn:
            with transaction(conn):
                finished = conn.execute(
                    "UPDATE background_jobs SET status='done',finished_at=?,heartbeat_at=?,"
                    "progress_current=CASE WHEN progress_total>0 THEN progress_total ELSE progress_current END,current_path='' "
                    "WHERE id=? AND status='running' AND worker_id=?",
                    (utc_now_text(), utc_now_text(), job["id"], WORKER_ID),
                ).rowcount
                if finished:
                    conn.execute(
                        "INSERT INTO job_events(job_id,event_type,message,detail,asset_id,created_at) "
                        "VALUES (?, 'completed', ?, ?, ?, ?)",
                        (job["id"], message, json.dumps(detail, ensure_ascii=False), job["asset_id"], utc_now_text()),
                    )
    except (JobOwnershipLost, JobCancelled):
        return
    except Exception as exc:
        with get_db() as conn:
            with transaction(conn):
                failed = conn.execute(
                    "UPDATE background_jobs SET status='failed',error=?,finished_at=?,heartbeat_at=? "
                    "WHERE id=? AND status='running' AND worker_id=?",
                    (str(exc), utc_now_text(), utc_now_text(), job["id"], WORKER_ID),
                ).rowcount
                if failed:
                    conn.execute(
                        "INSERT INTO job_events(job_id,event_type,message,asset_id,created_at) "
                        "VALUES (?, 'error', ?, ?, ?)",
                        (job["id"], str(exc), job["asset_id"], utc_now_text()),
                    )
                    if job_type == "scan" and job["data_source_id"]:
                        conn.execute(
                            "UPDATE data_sources SET last_scan_error=? WHERE id=?",
                            (str(exc), job["data_source_id"]),
                        )


def _scan_result_message(result: dict[str, int]) -> str:
    return (
        f"扫描完成：发现 {result['discovered']} 个图片文件，新增 {result['imported']} 条，"
        f"更新 {result['updated']} 条，未变更 {result['unchanged']} 条。"
    )


def _scan(job: dict) -> dict[str, int]:
    with get_db() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE id=?", (job["data_source_id"],)).fetchone()
    if not source:
        raise RuntimeError("数据源不存在")
    root = Path(source["root_path"])
    if not root.is_dir():
        raise RuntimeError(f"数据源目录不可访问或不是文件夹：{root}")

    candidates: list[Path] = []
    for listed, path in enumerate(root.rglob("*") if source["recursive"] else root.glob("*"), start=1):
        if listed == 1 or listed % 64 == 0:
            _touch_job(job, progress_current=0, progress_total=0, current_path="")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and not any(
            part.lower() == "database" or part == ".素材库回收站" for part in path.parts
        ):
            candidates.append(path)
    totals = {"discovered": len(candidates), "imported": 0, "updated": 0, "unchanged": 0}
    _touch_job(job, progress_current=0, progress_total=len(candidates), current_path="")

    for index, path in enumerate(candidates, start=1):
        stat = path.stat()
        key = normalize_path_key(str(path))
        modified = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
        asset_id: int | None = None
        with get_db() as conn:
            asset = conn.execute(
                "SELECT id,file_modified_at,file_size_bytes,resolution FROM assets WHERE path_key=?", (key,)
            ).fetchone()
            if asset and asset["file_modified_at"] == modified and asset["file_size_bytes"] == stat.st_size:
                if asset["resolution"] == "未知":
                    resolution, ratio = read_image_size(path)
                    conn.execute(
                        "UPDATE assets SET data_source_id=?,resolution=?,ratio=? WHERE id=?",
                        (source["id"], resolution, ratio, asset["id"]),
                    )
                    totals["updated"] += 1
                else:
                    totals["unchanged"] += 1
                    conn.execute("UPDATE assets SET data_source_id=? WHERE id=?", (source["id"], asset["id"]))
            elif asset:
                resolution, ratio = read_image_size(path)
                conn.execute(
                    "UPDATE assets SET data_source_id=?,file_modified_at=?,file_size_bytes=?,"
                    "file_size_fingerprint=?,resolution=?,ratio=?,ocr_status='pending',ocr_error='' WHERE id=?",
                    (source["id"], modified, stat.st_size, str(stat.st_size), resolution, ratio, asset["id"]),
                )
                asset_id = asset["id"]
                totals["updated"] += 1
            else:
                resolution, ratio = read_image_size(path)
                asset_id = conn.execute(
                    "INSERT INTO assets(source_path,path_key,data_source_id,library_name,detected_name,"
                    "resolution,ratio,file_size_bytes,file_modified_at,file_size_fingerprint,tags,"
                    "imported_at,source_type,source_author) VALUES (?,?,?,?,?,?,?,?,?,?,'[]',?,'smb',?)",
                    (str(path), key, source["id"], path.stem, path.stem, resolution, ratio, stat.st_size, modified,
                     str(stat.st_size), utc_now_text(), source["default_author"]),
                ).lastrowid
                totals["imported"] += 1
        _touch_job(job, progress_current=index, progress_total=len(candidates), current_path=str(path))
        if source["auto_ocr"] and asset_id is not None:
            enqueue("ocr", asset_id=asset_id)

    _touch_job(job, progress_current=len(candidates), progress_total=len(candidates), current_path="")
    with get_db() as conn:
        with transaction(conn):
            owned = conn.execute(
                "SELECT 1 FROM background_jobs WHERE id=? AND status='running' AND worker_id=?",
                (job["id"], WORKER_ID),
            ).fetchone()
            if owned:
                conn.execute(
                    "UPDATE data_sources SET last_scanned_at=?,last_scan_error='' WHERE id=?",
                    (utc_now_text(), source["id"]),
                )
    if not owned:
        raise JobOwnershipLost()
    return totals


def _ocr(job: dict) -> dict[str, object]:
    _touch_job(job)
    heartbeat_stop = threading.Event()

    def keep_heartbeat() -> None:
        while not heartbeat_stop.wait(30):
            try:
                _touch_job(job)
            except (JobOwnershipLost, JobCancelled):
                return

    heartbeat = threading.Thread(target=keep_heartbeat, name=f"photo-ocr-heartbeat-{job['id']}", daemon=True)
    heartbeat.start()
    from .main import recognize_asset_text
    try:
        result = recognize_asset_text(int(job["asset_id"]))
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
    _touch_job(job)
    if result.status != "ok":
        raise RuntimeError(result.message)
    return result.model_dump(mode="json", by_alias=True)


def _duplicate(job: dict) -> dict[str, object]:
    _touch_job(job)
    try:
        payload = json.loads(job.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("重复审查任务参数不是有效的 JSON。") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("重复审查任务参数必须是 JSON 对象。")

    folder = str(payload.get("folder") or "")
    ratio = str(payload.get("ratio") or "")
    heartbeat_stop = threading.Event()
    heartbeat_error: list[Exception] = []
    last_progress_touch = 0.0

    def keep_heartbeat() -> None:
        while not heartbeat_stop.wait(30):
            try:
                _touch_job(job)
            except (JobOwnershipLost, JobCancelled) as exc:
                heartbeat_error.append(exc)
                return

    heartbeat = threading.Thread(target=keep_heartbeat, name=f"photo-duplicate-heartbeat-{job['id']}", daemon=True)
    heartbeat.start()

    def progress_callback(progress_current: int, progress_total: int, current_path: str) -> None:
        nonlocal last_progress_touch
        now = time.monotonic()
        if progress_current not in {0, progress_total} and now - last_progress_touch < 0.5:
            return
        last_progress_touch = now
        _touch_job(
            job,
            progress_current=progress_current,
            progress_total=progress_total,
            current_path=current_path,
        )

    from .main import run_duplicate_analysis

    try:
        result = run_duplicate_analysis(folder, ratio, progress_callback)
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
    if heartbeat_error:
        raise heartbeat_error[0]
    _touch_job(job)
    if not isinstance(result, dict):
        raise RuntimeError("重复审查未返回有效结果。")
    return result
