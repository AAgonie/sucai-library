from __future__ import annotations

import io
import json
import mimetypes
import os
import platform
import difflib
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .db import DB_PATH, get_db, init_db
from .image_metadata import IMAGE_EXTENSIONS, read_image_size
from .v2 import enqueue, router as v2_router
from .workers import start_workers, stop_workers

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.local.json"
OCR_IGNORE_PATH = PROJECT_ROOT / "data" / "ocr_ignore_phrases.json"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
APP_TITLE = "短剧广告大图素材库展示网站"
LEGACY_DERIVATIVE_OUTPUT_PATH = r"\\FILE_SERVER\shared\image-library\DataBase"
DEFAULT_DERIVATIVE_OUTPUT_PATH = r"\\FILE_SERVER\shared\image-library\derived"
DERIVATIVE_SPECS: dict[str, dict[str, object]] = {
    "16:9": {"size": (1280, 720), "folder": "16x9"},
    "1:1": {"size": (800, 800), "folder": "1x1"},
    "9:16": {"size": (720, 1280), "folder": "9x16"},
}
SortKey = Literal["imported_desc", "clicks_desc", "downloads_desc", "size_desc", "title_asc", "front_number_desc", "front_number_asc"]
DisplayVersion = Literal["raw", "16:9", "1:1", "9:16", "original", "1280"]
OcrFilter = Literal["", "unrecognized", "done", "failed"]
AnnotationFilter = Literal["", "unconfirmed", "confirmed"]
SERVICE_STARTED_AT = time.monotonic()
APP_VERSION = "2.0.16"
RESTART_MODE = os.environ.get("PHOTO_WEB_RESTART_MODE", "disabled").strip().lower()
SUPERVISED = os.environ.get("PHOTO_WEB_SUPERVISED", "").strip() == "1"
SUPERVISED_RESTART_EXIT_CODE = 75
CPU_TIME_SAMPLE: Optional[tuple[int, int, int]] = None
OCR_EXECUTION_LOCK = threading.Lock()
DEFAULT_OCR_CPU_THREADS = 12
OCR_MODEL_PROFILE = os.environ.get("PHOTO_WEB_OCR_MODEL_PROFILE", "mobile").strip().lower()
OCR_USE_MOBILE_MODELS = OCR_MODEL_PROFILE != "medium"


def configured_ocr_cpu_threads() -> int:
    """Return a bounded CPU-thread budget for the single PaddleOCR engine."""
    configured = os.environ.get("PHOTO_WEB_OCR_CPU_THREADS") or os.environ.get("OMP_NUM_THREADS")
    try:
        requested = int(configured) if configured else DEFAULT_OCR_CPU_THREADS
    except ValueError:
        requested = DEFAULT_OCR_CPU_THREADS
    available = os.cpu_count() or DEFAULT_OCR_CPU_THREADS
    return max(1, min(requested, available, 16))


OCR_CPU_THREADS = configured_ocr_cpu_threads()
# Paddle reads these values when its native runtime is imported.  Preserve an
# explicit deployment override while raising the default above the old value 2.
os.environ.setdefault("OMP_NUM_THREADS", str(OCR_CPU_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(OCR_CPU_THREADS))


def paddle_ocr_model_options() -> dict[str, object]:
    """Use the CPU-friendly mobile OCR pair unless deployment opts into medium."""
    if not OCR_USE_MOBILE_MODELS:
        return {}
    return {
        "text_detection_model_name": "PP-OCRv5_mobile_det",
        "text_recognition_model_name": "PP-OCRv5_mobile_rec",
        "text_det_limit_type": "max",
        "text_det_limit_side_len": 960,
    }


class NasConfig(BaseModel):
    type: Literal["smb", "http"] = "smb"
    host: str = Field(default="")
    share: str = Field(default="")
    base_path: str = Field(default=r"\\FILE_SERVER\shared\image-library")
    username: str = Field(default="")
    password: str = Field(default="")
    recursive: bool = False


class AppConfig(BaseModel):
    title: str = APP_TITLE
    default_batch_size: int = 50
    allowed_extensions: list[str] = [".jpg", ".jpeg", ".png", ".webp"]
    derivative_output_path: str = DEFAULT_DERIVATIVE_OUTPUT_PATH


class LocalConfig(BaseModel):
    nas: NasConfig = NasConfig()
    app: AppConfig = AppConfig()


class ServiceRestartRequest(BaseModel):
    confirm: bool = False


class ConnectionCheckResponse(BaseModel):
    status: Literal["ok", "invalid_config", "failed"]
    message: str
    detail: Optional[str] = None
    missing_fields: list[str] = []


class ScanRequest(BaseModel):
    config: Optional[NasConfig] = None


class AssetUpdateRequest(BaseModel):
    libraryName: str
    detectedName: str
    tags: list[str]


class DeleteRequest(BaseModel):
    username: str = ""
    kind: Optional[Literal["16:9", "1:1", "9:16"]] = None


class ScanResponse(BaseModel):
    status: Literal["ok", "failed"]
    message: str
    scanned: int = 0
    imported: int = 0


class OcrResponse(BaseModel):
    status: Literal["ok", "missing_dependency", "failed"]
    message: str
    text: str = ""
    asset: Optional["AssetResponse"] = None
    assetId: Optional[int] = None
    name: str = ""
    autoHidden: bool = False
    hiddenReason: str = ""
    syncWarning: str = ""


class OcrBatchRequest(BaseModel):
    asset_ids: list[int]


class OcrBatchResponse(BaseModel):
    status: Literal["ok", "failed"]
    message: str
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    autoHidden: int = 0
    syncWarnings: list[str] = Field(default_factory=list)


class OcrFailureItem(BaseModel):
    id: int
    name: str
    reason: str
    status: str


class OcrSummaryResponse(BaseModel):
    total: int
    unrecognized: int
    done: int
    failed: int
    running: int
    unrecognizedIds: list[int]
    failedIds: list[int]
    failedItems: list[OcrFailureItem]


class OcrIgnorePhraseRequest(BaseModel):
    phrase: str


class OcrIgnorePhrasesRequest(BaseModel):
    phrases: list[str]


class SourceAuditRequest(BaseModel):
    asset_ids: list[int] = []


class DatabaseDeleteRequest(BaseModel):
    asset_ids: list[int]
    username: str = ""


class RestoreDeletedAssetsRequest(BaseModel):
    deleted_ids: list[int]
    username: str = ""


class OrphanDerivativeDeleteRequest(BaseModel):
    paths: list[str]
    username: str = ""


class DerivativePreflightRequest(BaseModel):
    asset_ids: list[int]
    author: str = ""
    channel: str = ""
    force_jpeg: bool = False


class DerivativeFolderPreflightRequest(BaseModel):
    folder: str
    author: str = ""
    channel: str = ""
    force_jpeg: bool = False


class DerivativeGenerateRequest(BaseModel):
    asset_ids: list[int]
    author: str = ""
    channel: str = ""
    force_jpeg: bool = False
    username: str = ""


class DerivativeAuthorUpdateRequest(BaseModel):
    asset_ids: list[int]
    author: str = ""
    channel: str = ""
    username: str = ""


class LegacyDerivativeMigrationRequest(BaseModel):
    confirm: bool = False
    migration_ids: list[int] = []


class DuplicateAnalysisRequest(BaseModel):
    folder: str = ""
    ratio: str = ""


class DuplicateRetainRequest(BaseModel):
    keep_asset_id: int
    candidate_asset_ids: list[int]


class DuplicateUndoRequest(BaseModel):
    keep_asset_id: int


class AssetResponse(BaseModel):
    id: int
    detectedName: str
    libraryName: str
    resolution: str
    ratio: str
    fileSizeMb: float
    tags: list[str]
    importedAt: str
    downloads: int
    clicks: int
    path: str
    imageUrl: str
    ocrText: str = ""
    ocrStatus: str = "pending"
    ocrError: str = ""
    folder: str = ""
    sourcePath: str = ""
    displayVersion: DisplayVersion = "raw"
    displayAuthor: str = "未识别作者"
    derivativeAvailable: bool = False
    derivativeStatus: str = ""
    derivativePath: str = ""
    derivativeAuthor: str = ""
    derivativeChannel: str = ""
    derivativeSequence: Optional[int] = None
    derivativeGeneratedAt: str = ""
    hidden: bool = False


class AssetListResponse(BaseModel):
    items: list[AssetResponse]
    total: int
    message: str


app = FastAPI(title=f"{APP_TITLE} API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(v2_router)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@app.on_event("startup")
def startup() -> None:
    init_db()
    start_workers()


@app.on_event("shutdown")
def shutdown() -> None:
    stop_workers()


def load_config() -> LocalConfig:
    if not CONFIG_PATH.exists():
        return LocalConfig()
    config = LocalConfig.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    # Versions before this move stored the old output root in config.local.json.
    # Use the new root without silently rewriting the user's configuration file.
    if os.path.normcase(os.path.normpath(config.app.derivative_output_path)) == os.path.normcase(os.path.normpath(LEGACY_DERIVATIVE_OUTPUT_PATH)):
        config.app.derivative_output_path = DEFAULT_DERIVATIVE_OUTPUT_PATH
    if config.app.title in {"杂物间", "素材库"}:
        config.app.title = APP_TITLE
    return config


def save_config_file(config: LocalConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    temp_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temp_path, CONFIG_PATH)


def is_http_path(path: str) -> bool:
    return path.lower().startswith(("http://", "https://"))


def build_request(url: str, config: NasConfig, method: str = "GET") -> urllib.request.Request:
    headers = {"User-Agent": "short-drama-ad-library/0.3"}
    if config.username or config.password:
        token = f"{config.username}:{config.password}".encode("utf-8")
        import base64

        headers["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
    return urllib.request.Request(url, headers=headers, method=method)


def guess_tags(name: str) -> list[str]:
    text = name.lower()
    tags: list[str] = []
    if any(word in text for word in ["game", "游戏", "play"]):
        tags.append("游戏")
    if any(word in text for word in ["food", "pizza", "餐", "吃"]):
        tags.append("餐饮")
    if any(word in text for word in ["product", "shop", "buy", "商品", "采购"]):
        tags.append("商品")
    if any(word in text for word in ["people", "person", "portrait", "人像"]):
        tags.append("人像")
    if not tags:
        tags.append("未标注")
    return tags


def read_image_author(path: Path) -> str:
    """Read the author embedded in a generated image without changing the file."""
    try:
        from PIL import Image
    except ModuleNotFoundError:
        return ""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            xp_author = exif.get(40093)
            if isinstance(xp_author, tuple):
                xp_author = bytes(xp_author)
            if isinstance(xp_author, bytes):
                value = xp_author.decode("utf-16le", errors="ignore").rstrip("\x00").strip()
                if value:
                    return normalize_author(value)
            elif xp_author:
                value = str(xp_author).rstrip("\x00").strip()
                if value:
                    return normalize_author(value)
            artist = exif.get(315)
            if artist:
                return normalize_author(str(artist))
            png_author = image.info.get("Author")
            if png_author:
                return normalize_author(str(png_author))
    except (OSError, ValueError, SyntaxError):
        return ""
    return ""


def ratio_text(width: int, height: int) -> str:
    if not width or not height:
        return "未知"
    value = width / height
    if abs(value - 16 / 9) < 0.03:
        return "16:9"
    return f"{value:.2f}:1"


def folder_from_source_path(source_path: str) -> str:
    if is_http_path(source_path):
        parsed = urllib.parse.urlparse(source_path)
        folder = str(Path(urllib.parse.unquote(parsed.path)).parent).replace("\\", "/").strip("/")
        return folder or parsed.netloc
    return str(Path(source_path).parent)


def normalize_display_version(version: str) -> Literal["raw", "16:9", "1:1", "9:16"]:
    """Keep the pre-V2 query values readable while using V2 ratio names internally."""
    if version in {"original", "raw"}:
        return "raw"
    if version in {"1280", "16:9"}:
        return "16:9"
    if version in {"1:1", "9:16"}:
        return version
    return "raw"


def derivative_resolution_for_version(version: str) -> str:
    return {"16:9": "1280 x 720", "1:1": "800 x 800", "9:16": "720 x 1280"}.get(version, "未知")


def asset_from_row(row: sqlite3.Row) -> AssetResponse:
    if "relation_tags" in row.keys():
        tags = [tag for tag in (row["relation_tags"] or "").split("\x1f") if tag]
    else:
        try:
            tags = json.loads(row["tags"])
        except json.JSONDecodeError:
            tags = []
    display_version = normalize_display_version(row["display_version"] if "display_version" in row.keys() else "raw")
    derivative_available = bool(row["derivative_available"]) if "derivative_available" in row.keys() else bool(row["derivative_path"]) if "derivative_path" in row.keys() else False
    derivative_path = row["derivative_path"] if "derivative_path" in row.keys() else ""
    is_derivative = display_version != "raw"
    display_path = derivative_path if is_derivative and derivative_path else row["source_path"]
    display_size = row["derivative_size_bytes"] if is_derivative and "derivative_size_bytes" in row.keys() else row["file_size_bytes"]
    return AssetResponse(
        id=row["id"],
        detectedName=row["detected_name"],
        libraryName=row["library_name"],
        resolution=derivative_resolution_for_version(display_version) if is_derivative else row["resolution"],
        ratio=display_version if is_derivative else row["ratio"],
        fileSizeMb=round(display_size / 1024 / 1024, 3) if display_size else 0,
        tags=tags,
        importedAt=row["imported_at"],
        downloads=row["downloads"],
        clicks=row["clicks"],
        path=display_path,
        imageUrl=f"/api/assets/{row['id']}/derivative-file?kind={urllib.parse.quote(display_version)}" if is_derivative else f"/api/assets/{row['id']}/file",
        ocrText=row["ocr_text"] if "ocr_text" in row.keys() else "",
        ocrStatus=row["ocr_status"] if "ocr_status" in row.keys() else "pending",
        ocrError=row["ocr_error"] if "ocr_error" in row.keys() else "",
        folder=folder_from_source_path(row["source_path"]),
        sourcePath=row["source_path"],
        displayVersion=display_version,
        displayAuthor=display_author_for_row(row),
        derivativeAvailable=derivative_available,
        derivativeStatus=row["derivative_status"] if "derivative_status" in row.keys() else "",
        derivativePath=derivative_path or "",
        derivativeAuthor=display_author_for_row(row),
        derivativeChannel=row["derivative_channel"] if "derivative_channel" in row.keys() and row["derivative_channel"] else "",
        derivativeSequence=row["derivative_sequence"] if "derivative_sequence" in row.keys() else None,
        derivativeGeneratedAt=row["derivative_generated_at"] if "derivative_generated_at" in row.keys() and row["derivative_generated_at"] else "",
        hidden=bool(row["hidden_at"]) if "hidden_at" in row.keys() else False,
    )


def upsert_asset(path: str, source_type: str, file_size: int = 0, resolution: str = "未知", ratio: str = "未知", source_author: str = "") -> bool:
    name = Path(urllib.parse.urlparse(path).path).name or path.rstrip("/").split("/")[-1]
    stem = Path(name).stem or name
    tags = guess_tags(stem)
    with get_db() as conn:
        exists = conn.execute("SELECT id FROM assets WHERE source_path = ?", (path,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE assets SET file_size_bytes = ?, resolution = ?, ratio = ?, source_author = CASE WHEN ? <> '' THEN ? ELSE source_author END WHERE source_path = ?",
                (file_size, resolution, ratio, source_author, source_author, path),
            )
            return False
        conn.execute(
            """
            INSERT INTO assets (source_path, library_name, detected_name, resolution, ratio, file_size_bytes, tags, imported_at, source_type, source_author)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (path, stem, stem, resolution, ratio, file_size, json.dumps(tags, ensure_ascii=False), now_text(), source_type, source_author),
        )
        return True


def scan_local_path(base_path: str, recursive: bool, allowed_extensions: set[str]) -> tuple[int, int]:
    root = Path(base_path)
    if not root.exists():
        raise FileNotFoundError(f"路径不存在：{base_path}")
    candidates = root.rglob("*") if recursive else root.glob("*")
    scanned = 0
    imported = 0
    for file_path in candidates:
        if not file_path.is_file() or file_path.suffix.lower() not in allowed_extensions:
            continue
        scanned += 1
        resolution, ratio = read_image_size(file_path)
        author = read_image_author(file_path)
        if upsert_asset(str(file_path), "local", file_path.stat().st_size, resolution, ratio, author):
            imported += 1
    return scanned, imported


def scan_http_path(config: NasConfig, allowed_extensions: set[str]) -> tuple[int, int]:
    url = config.base_path.strip()
    parsed = urllib.parse.urlparse(url)
    if Path(parsed.path).suffix.lower() in allowed_extensions:
        imported = 1 if upsert_asset(url, "http") else 0
        return 1, imported

    request = build_request(url, config)
    with urllib.request.urlopen(request, timeout=8) as response:
        content_type = response.headers.get("Content-Type", "")
        body = response.read(2_000_000)
    if "html" not in content_type.lower() and b"<html" not in body[:500].lower():
        raise ValueError("HTTP 地址不是图片，也不像目录列表页面。")
    html = body.decode("utf-8", errors="ignore")
    hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", html, flags=re.IGNORECASE)
    urls: list[str] = []
    for href in hrefs:
        absolute = urllib.parse.urljoin(url, href)
        if Path(urllib.parse.urlparse(absolute).path).suffix.lower() in allowed_extensions:
            urls.append(absolute)
    scanned = 0
    imported = 0
    for image_url in dict.fromkeys(urls):
        scanned += 1
        if upsert_asset(image_url, "http"):
            imported += 1
    return scanned, imported


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "name": APP_TITLE, "version": APP_VERSION}


def memory_status_mb() -> tuple[int, int]:
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical / 1024 / 1024), int(status.available_physical / 1024 / 1024)
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = page_size * os.sysconf("SC_PHYS_PAGES")
        available = page_size * os.sysconf("SC_AVPHYS_PAGES")
        return int(total / 1024 / 1024), int(available / 1024 / 1024)
    except (AttributeError, OSError, ValueError):
        return 0, 0


def cpu_usage_percent() -> Optional[float]:
    """Return total machine CPU usage between two status samples on Windows."""
    global CPU_TIME_SAMPLE
    if os.name != "nt":
        return None
    try:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

        idle, kernel, user = FileTime(), FileTime(), FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        values = tuple((item.dwHighDateTime << 32) | item.dwLowDateTime for item in (idle, kernel, user))
        previous = CPU_TIME_SAMPLE
        CPU_TIME_SAMPLE = values
        if previous is None:
            return None
        idle_delta = values[0] - previous[0]
        total_delta = values[1] - previous[1] + values[2] - previous[2]
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)), 1)
    except (AttributeError, OSError):
        return None


@app.get("/api/system/status")
def system_status() -> dict[str, object]:
    memory_total_mb, memory_available_mb = memory_status_mb()
    disk = shutil.disk_usage(PROJECT_ROOT)
    try:
        from importlib.util import find_spec

        ocr_installed = find_spec("paddleocr") is not None
    except (ImportError, ValueError):
        ocr_installed = False
    return {
        "status": "running",
        "version": APP_VERSION,
        "pid": os.getpid(),
        "uptimeSeconds": int(time.monotonic() - SERVICE_STARTED_AT),
        "hostname": platform.node() or "未知主机",
        "platform": platform.platform(),
        "pythonVersion": platform.python_version(),
        "memoryTotalMb": memory_total_mb,
        "memoryAvailableMb": memory_available_mb,
        "cpuUsagePercent": cpu_usage_percent(),
        "diskTotalGb": round(disk.total / 1024 / 1024 / 1024, 1),
        "diskFreeGb": round(disk.free / 1024 / 1024 / 1024, 1),
        "ocrInstalled": ocr_installed,
        "restartSupported": SUPERVISED and RESTART_MODE in {"local", "nas"},
    }


def shutdown_current_service_after_delay() -> None:
    time.sleep(0.8)
    try:
        stop_workers()
    finally:
        # The supervisor must always receive the restart code, even if lease cleanup fails.
        os._exit(SUPERVISED_RESTART_EXIT_CODE)


@app.post("/api/system/restart")
def restart_service(payload: ServiceRestartRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需要确认后才能重启服务")
    if not SUPERVISED or RESTART_MODE not in {"local", "nas"}:
        raise HTTPException(status_code=409, detail="当前启动方式没有服务监督器，请从 start.cmd 或 start-nas.cmd 启动。")
    background_tasks.add_task(shutdown_current_service_after_delay)
    return {"status": "restarting", "message": "服务正在重启，网页将在数秒后自动恢复。"}


@app.get("/api/config", response_model=LocalConfig)
def get_config() -> LocalConfig:
    return load_config()


@app.post("/api/config", response_model=LocalConfig)
def save_config(config: LocalConfig) -> LocalConfig:
    save_config_file(config)
    return config


@app.post("/api/nas/check", response_model=ConnectionCheckResponse)
def check_nas_connection(config: NasConfig) -> ConnectionCheckResponse:
    missing = [field for field in ["base_path"] if not getattr(config, field).strip()]
    if missing:
        return ConnectionCheckResponse(status="invalid_config", message="配置不完整。", missing_fields=missing)
    try:
        if is_http_path(config.base_path):
            request = build_request(config.base_path, config, method="GET")
            with urllib.request.urlopen(request, timeout=8) as response:
                return ConnectionCheckResponse(
                    status="ok",
                    message="HTTP 地址可以访问。",
                    detail=f"HTTP {response.status}; Content-Type: {response.headers.get('Content-Type', '未知')}",
                )
        path = Path(config.base_path)
        if path.exists():
            return ConnectionCheckResponse(status="ok", message="Windows 路径可以访问。", detail=str(path))
        return ConnectionCheckResponse(status="failed", message="路径不存在或当前账号无权访问。", detail=config.base_path)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ConnectionCheckResponse(status="failed", message="连接失败。", detail=str(exc))


@app.post("/api/assets/scan", response_model=ScanResponse)
def scan_assets(payload: ScanRequest) -> ScanResponse:
    config = payload.config or load_config().nas
    allowed_extensions = {ext.lower() for ext in load_config().app.allowed_extensions}
    try:
        if is_http_path(config.base_path):
            scanned, imported = scan_http_path(config, allowed_extensions)
        else:
            scanned, imported = scan_local_path(config.base_path, config.recursive, allowed_extensions)
        return ScanResponse(status="ok", message="扫描完成。", scanned=scanned, imported=imported)
    except Exception as exc:
        return ScanResponse(status="failed", message=str(exc), scanned=0, imported=0)


@app.post("/api/derivatives/backfill-authors")
def backfill_derivative_authors() -> dict[str, object]:
    """Import author metadata from existing DataBase files into SQLite on demand."""
    output_root = derivative_output_root()
    normalized_root = os.path.normcase(os.path.normpath(str(output_root))).rstrip("\\") + "\\"
    with get_db() as conn:
        asset_rows = conn.execute(
            "SELECT id, source_path FROM assets WHERE source_type = 'local' AND TRIM(COALESCE(source_author, '')) = ''"
        ).fetchall()
        derivative_rows = conn.execute(
            "SELECT id, output_path FROM asset_derivatives WHERE kind = '16:9' AND status = 'done' AND TRIM(COALESCE(author, '')) IN ('', '未识别作者')"
        ).fetchall()
    updated = 0
    checked = 0
    errors: list[str] = []
    for row in asset_rows:
        source_path = str(row["source_path"])
        normalized_path = os.path.normcase(os.path.normpath(source_path))
        if not normalized_path.startswith(normalized_root):
            continue
        checked += 1
        try:
            author = read_image_author(Path(source_path))
            if author:
                with get_db() as conn:
                    conn.execute("UPDATE assets SET source_author = ? WHERE id = ?", (author, row["id"]))
                remember_derivative_author(author)
                updated += 1
        except OSError as exc:
            if len(errors) < 20:
                errors.append(f"{source_path}：{exc}")
    for row in derivative_rows:
        output_path = str(row["output_path"])
        checked += 1
        try:
            author = read_image_author(Path(output_path))
            if author:
                with get_db() as conn:
                    conn.execute("UPDATE asset_derivatives SET author = ? WHERE id = ?", (author, row["id"]))
                remember_derivative_author(author)
                updated += 1
        except OSError as exc:
            if len(errors) < 20:
                errors.append(f"{output_path}：{exc}")
    return {"status": "ok", "checked": checked, "updated": updated, "errors": errors, "message": "DataBase 作者回填完成。"}


def front_number_value(row: sqlite3.Row) -> Optional[int]:
    text = " ".join(str(row[field] or "") for field in ["library_name", "detected_name", "ocr_text", "source_path"] if field in row.keys())
    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group(0))


def normalize_imported_day(value: str) -> str:
    """Return the calendar day stored at the start of every imported_at value.

    Older rows use ``YYYY-MM-DD HH:MM:SS`` while V2 scan rows use ISO 8601
    timestamps such as ``YYYY-MM-DDTHH:MM:SS+08:00``.  Comparing their full
    text values against a boundary containing a space makes every ISO row on
    the boundary day sort after the supposed end of that day.  The date picker
    works on calendar days, so compare the stable leading date component.
    """
    normalized = value.strip()
    match = re.match(r"\d{4}-\d{2}-\d{2}", normalized)
    return match.group(0) if match else normalized


@app.get("/api/assets", response_model=AssetListResponse)
def list_assets(
    q: str = "",
    tags: list[str] = Query(default=[]),
    tag_mode: Literal["and", "or"] = "and",
    sort: SortKey = "imported_desc",
    fuzzy: bool = False,
    include_library: bool = True,
    imported_from: str = "",
    imported_to: str = "",
    folder: str = "",
    author: str = "",
    channel: str = "",
    exclude_path: str = "",
    ocr: OcrFilter = "",
    version: DisplayVersion = "raw",
    exclude_hidden: bool = False,
    include_hidden: bool = False,
    annotation_status: AnnotationFilter = "",
    limit: int = 20,
    offset: int = 0,
) -> AssetListResponse:
    clauses = []
    params: list[object] = []
    display_version = normalize_display_version(version)
    # The explicit visibility toggle is the single source of truth for hidden rows.
    # Folder and author filters narrow the result set but do not override it.
    if exclude_hidden or not include_hidden:
        clauses.append("assets.hidden_at IS NULL")
    if annotation_status == "unconfirmed":
        clauses.append("NOT EXISTS (SELECT 1 FROM asset_annotations aa WHERE aa.asset_id = assets.id)")
        clauses.append("NOT EXISTS (SELECT 1 FROM asset_tags annotation_tags WHERE annotation_tags.asset_id = assets.id)")
    elif annotation_status == "confirmed":
        clauses.append("EXISTS (SELECT 1 FROM asset_annotations aa WHERE aa.asset_id = assets.id) OR EXISTS (SELECT 1 FROM asset_tags annotation_tags WHERE annotation_tags.asset_id = assets.id)")
    if q.strip():
        search_fields = ["assets.detected_name", "assets.ocr_text"]
        if include_library:
            search_fields.insert(0, "assets.library_name")

        def search_clause(query: str) -> str:
            name_clause = " OR ".join(f"{field} LIKE ?" for field in search_fields)
            tag_clause = "EXISTS (SELECT 1 FROM asset_tags sat JOIN tag_definitions std ON std.id = sat.tag_id JOIN tag_categories stc ON stc.id = std.category_id WHERE sat.asset_id = assets.id AND std.active = 1 AND stc.active = 1 AND std.name LIKE ?)"
            params.extend([query] * len(search_fields))
            params.append(query)
            return f"({name_clause} OR {tag_clause})"

        if fuzzy:
            compact = re.sub(r"\s+", "", q.strip())
            tokens = [token for token in compact if token]
            for token in tokens[:24]:
                clauses.append(search_clause(f"%{token}%"))
        else:
            clauses.append(search_clause(f"%{q.strip()}%"))
    selected_tags = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
    if selected_tags:
        tag_predicates = ["EXISTS (SELECT 1 FROM asset_tags at JOIN tag_definitions td ON td.id = at.tag_id JOIN tag_categories tc ON tc.id = td.category_id WHERE at.asset_id = assets.id AND td.name = ? AND td.active = 1 AND tc.active = 1)" for _ in selected_tags]
        clauses.append("(" + (" OR " if tag_mode == "or" else " AND ").join(tag_predicates) + ")")
        params.extend(selected_tags)
    if imported_from.strip():
        clauses.append("substr(assets.imported_at, 1, 10) >= ?")
        params.append(normalize_imported_day(imported_from))
    if imported_to.strip():
        clauses.append("substr(assets.imported_at, 1, 10) <= ?")
        params.append(normalize_imported_day(imported_to))
    if folder.strip():
        normalized_folder = folder.strip().replace("/", "\\").rstrip("\\")
        clauses.append("LOWER(REPLACE(assets.source_path, '/', '\\')) LIKE LOWER(?)")
        params.append(f"{normalized_folder}\\%")
    if author.strip():
        if display_version == "raw":
            clauses.append("(TRIM(COALESCE(assets.source_author, '')) = ? OR (TRIM(COALESCE(assets.source_author, '')) = '' AND LOWER(REPLACE(assets.source_path, '/', '\\')) LIKE LOWER(?)))")
            params.extend([author.strip(), f"%\\{author.strip()}\\%"])
        else:
            clauses.append("COALESCE(NULLIF(TRIM(d.author), ''), '未识别作者') = ?")
            params.append(author.strip())
    if channel.strip():
        clauses.append("EXISTS (SELECT 1 FROM asset_derivatives cd WHERE cd.asset_id = assets.id AND cd.status = 'done' AND cd.channel = ?)")
        params.append(channel.strip())
    if exclude_path.strip():
        normalized_exclude_path = exclude_path.strip().replace("/", "\\").rstrip("\\")
        if normalized_exclude_path:
            exclude_prefix = f"{normalized_exclude_path}\\"
            clauses.append("LOWER(REPLACE(assets.source_path, '/', '\\')) NOT LIKE LOWER(?)")
            params.append(f"{exclude_prefix}%")
    if ocr == "unrecognized":
        clauses.append("(assets.ocr_status IS NULL OR assets.ocr_status = '' OR assets.ocr_status IN ('pending', 'empty', 'failed', 'missing_dependency') OR assets.ocr_text = '')")
    elif ocr == "done":
        clauses.append("(assets.ocr_status = 'done' AND assets.ocr_text != '')")
    elif ocr == "failed":
        clauses.append("assets.ocr_status IN ('failed', 'missing_dependency')")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order_map = {
        "imported_desc": "assets.imported_at DESC",
        "clicks_desc": "assets.clicks DESC",
        "downloads_desc": "assets.downloads DESC",
        "size_desc": "assets.file_size_bytes DESC",
        "title_asc": "assets.library_name ASC",
    }
    order_by = order_map.get(sort, "imported_at DESC")
    derivative_join = f"JOIN asset_derivatives d ON d.asset_id = assets.id AND d.kind = '{display_version}' AND d.status = 'done'" if display_version != "raw" else "LEFT JOIN asset_derivatives d ON d.asset_id = assets.id AND d.kind = '16:9' AND d.status = 'done'"
    select_fields = """
        assets.*,
        CASE WHEN d.id IS NULL THEN 0 ELSE 1 END AS derivative_available,
        COALESCE(d.output_path, '') AS derivative_path,
        COALESCE(d.status, '') AS derivative_status,
        COALESCE(d.author, '') AS derivative_author,
        COALESCE(d.channel, '') AS derivative_channel,
        d.sequence_number AS derivative_sequence,
        COALESCE(d.generated_at, '') AS derivative_generated_at,
        COALESCE(d.output_size_bytes, 0) AS derivative_size_bytes,
        (SELECT GROUP_CONCAT(td.name, CHAR(31)) FROM asset_tags at JOIN tag_definitions td ON td.id = at.tag_id JOIN tag_categories tc ON tc.id = td.category_id WHERE at.asset_id = assets.id AND td.active = 1 AND tc.active = 1) AS relation_tags,
        '""" + display_version + """' AS display_version
    """
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM assets {derivative_join}{where}", params).fetchone()["count"]
        if sort in {"front_number_desc", "front_number_asc"}:
            rows = conn.execute(f"SELECT {select_fields} FROM assets {derivative_join}{where}", params).fetchall()
            if sort == "front_number_desc":
                rows.sort(key=lambda row: (front_number_value(row) is not None, front_number_value(row) or 0, str(row["library_name"])), reverse=True)
            else:
                rows.sort(key=lambda row: (front_number_value(row) is None, front_number_value(row) or 0, str(row["library_name"])))
            rows = rows[max(0, offset) : max(0, offset) + max(1, min(limit, 200))]
        else:
            rows = conn.execute(
                f"SELECT {select_fields} FROM assets {derivative_join}{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 200)), max(0, offset)],
            ).fetchall()
    message = "已从数据库读取图片。" if total else "数据库里还没有图片，请先在配置页连接测试并扫描入库。"
    return AssetListResponse(items=[asset_from_row(row) for row in rows], total=total, message=message)


@app.get("/api/tags")
def list_tags() -> dict[str, list[str]]:
    with get_db() as conn:
        rows = conn.execute("SELECT t.name FROM tag_definitions t JOIN asset_tags at ON at.tag_id=t.id JOIN tag_categories c ON c.id=t.category_id WHERE t.active=1 AND c.active=1 ORDER BY t.name COLLATE NOCASE").fetchall()
    return {"items": list(dict.fromkeys(row["name"] for row in rows))}


@app.get("/api/folders")
def list_folders() -> dict[str, list[str]]:
    with get_db() as conn:
        rows = conn.execute("SELECT source_path FROM assets ORDER BY source_path ASC").fetchall()
    folders = sorted({folder_from_source_path(row["source_path"]) for row in rows if row["source_path"]})
    return {"items": folders}


@app.get("/api/assets/filter-options")
def list_asset_filter_options(version: DisplayVersion = "raw") -> dict[str, list[str]]:
    """Return current database values, never the remembered export-form history."""
    display_version = normalize_display_version(version)
    with get_db() as conn:
        source_rows = conn.execute("SELECT source_path, source_author FROM assets WHERE source_path <> ''").fetchall()
        channel_rows = conn.execute("SELECT DISTINCT channel FROM asset_derivatives WHERE status = 'done' AND TRIM(channel) <> '' ORDER BY channel COLLATE NOCASE").fetchall()
        if display_version == "raw":
            authors = {
                str(row["source_author"]).strip() or author_from_source_path(str(row["source_path"]))
                for row in source_rows
            }
        else:
            author_rows = conn.execute(
                "SELECT DISTINCT author FROM asset_derivatives WHERE status = 'done' AND kind = ? AND TRIM(author) <> '' ORDER BY author COLLATE NOCASE",
                (display_version,),
            ).fetchall()
            authors = {str(row["author"]).strip() for row in author_rows}
    folders = {folder_from_source_path(row["source_path"]) for row in source_rows if row["source_path"]}
    return {
        "folders": sorted(folders, key=str.casefold),
        "authors": sorted((author for author in authors if author), key=str.casefold),
        "channels": [str(row["channel"]) for row in channel_rows],
    }


def unique_asset_ids(asset_ids: list[int]) -> list[int]:
    return [asset_id for asset_id in dict.fromkeys(asset_ids) if asset_id > 0]


def source_audit_result(row: sqlite3.Row) -> tuple[str, str]:
    source_path = row["source_path"]
    if not is_http_path(source_path):
        try:
            path = Path(source_path)
            return ("available", "文件可访问。") if path.exists() else ("missing", "原文件不存在，可能已改名、移动或删除。")
        except OSError as exc:
            return "network_error", str(exc)
    config = load_config().nas
    try:
        request = build_request(source_path, config, method="HEAD")
        with urllib.request.urlopen(request, timeout=8) as response:
            return "available", f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "missing", "HTTP 404：原链接不存在，可能已改名、移动或删除。"
        if exc.code in {401, 403}:
            return "unauthorized", f"HTTP {exc.code}：没有访问权限。"
        return "network_error", f"HTTP {exc.code}：{exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "network_error", str(exc)


@app.post("/api/database-review/audit")
def audit_asset_sources(payload: SourceAuditRequest) -> dict[str, object]:
    requested_ids = unique_asset_ids(payload.asset_ids)
    with get_db() as conn:
        if requested_ids:
            placeholders = ",".join("?" for _ in requested_ids)
            rows = conn.execute(f"SELECT * FROM assets WHERE id IN ({placeholders}) ORDER BY imported_at DESC", requested_ids).fetchall()
        else:
            rows = conn.execute("SELECT * FROM assets ORDER BY imported_at DESC").fetchall()
        results: list[dict[str, object]] = []
        checked_at = now_text()
        for row in rows:
            status, detail = source_audit_result(row)
            conn.execute(
                """
                INSERT INTO asset_source_audits(asset_id, status, detail, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET status = excluded.status, detail = excluded.detail, checked_at = excluded.checked_at
                """,
                (row["id"], status, detail, checked_at),
            )
            results.append({
                "id": row["id"], "detectedName": row["detected_name"], "libraryName": row["library_name"],
                "path": row["source_path"], "ocrStatus": row["ocr_status"], "importedAt": row["imported_at"],
                "status": status, "detail": detail,
            })
    missing = sum(1 for item in results if item["status"] == "missing")
    return {"checkedAt": checked_at, "items": results, "summary": {"total": len(results), "missing": missing, "available": sum(1 for item in results if item["status"] == "available"), "unauthorized": sum(1 for item in results if item["status"] == "unauthorized"), "networkError": sum(1 for item in results if item["status"] == "network_error")}}


@app.get("/api/database-review/latest")
def latest_source_audit() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT assets.*, audits.status AS audit_status, audits.detail AS audit_detail, audits.checked_at AS audit_checked_at
            FROM asset_source_audits audits JOIN assets ON assets.id = audits.asset_id
            ORDER BY CASE audits.status WHEN 'missing' THEN 0 WHEN 'unauthorized' THEN 1 WHEN 'network_error' THEN 2 ELSE 3 END, audits.checked_at DESC
            """
        ).fetchall()
    items = [{
        "id": row["id"], "detectedName": row["detected_name"], "libraryName": row["library_name"], "path": row["source_path"],
        "ocrStatus": row["ocr_status"], "importedAt": row["imported_at"], "status": row["audit_status"], "detail": row["audit_detail"], "checkedAt": row["audit_checked_at"],
    } for row in rows]
    return {"items": items}


@app.post("/api/database-review/remove")
def remove_assets_from_database(payload: DatabaseDeleteRequest) -> dict[str, object]:
    asset_ids = unique_asset_ids(payload.asset_ids)
    if not asset_ids:
        raise HTTPException(status_code=400, detail="未选择要从数据库移除的项目")
    with get_db() as conn:
        placeholders = ",".join("?" for _ in asset_ids)
        rows = conn.execute(f"SELECT * FROM assets WHERE id IN ({placeholders})", asset_ids).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="所选项目不存在")
        active_jobs = conn.execute(
            f"SELECT id, asset_id FROM background_jobs WHERE asset_id IN ({placeholders}) "
            "AND status IN ('queued', 'running', 'cancel_requested')",
            asset_ids,
        ).fetchall()
        if active_jobs:
            job_ids = ", ".join(str(job["id"]) for job in active_jobs)
            raise HTTPException(
                status_code=409,
                detail=f"所选图片仍有关联的待执行或进行中任务（任务 #{job_ids}）。请先等待完成或停止任务后再移除。",
            )
        removed_ids: list[int] = []
        for row in rows:
            derivatives = conn.execute("SELECT * FROM asset_derivatives WHERE asset_id = ?", (row["id"],)).fetchall()
            audit = conn.execute("SELECT * FROM asset_source_audits WHERE asset_id = ?", (row["id"],)).fetchone()
            tags = conn.execute("SELECT * FROM asset_tags WHERE asset_id = ?", (row["id"],)).fetchall()
            annotation = conn.execute("SELECT * FROM asset_annotations WHERE asset_id = ?", (row["id"],)).fetchone()
            visual_hash = conn.execute("SELECT * FROM asset_visual_hashes WHERE asset_id = ?", (row["id"],)).fetchone()
            snapshot = {
                "asset": dict(row),
                "derivatives": [dict(item) for item in derivatives],
                "audit": dict(audit) if audit else None,
                "tags": [dict(item) for item in tags],
                "annotation": dict(annotation) if annotation else None,
                "visual_hash": dict(visual_hash) if visual_hash else None,
            }
            conn.execute(
                "INSERT INTO deleted_assets(original_asset_id, snapshot, deleted_at, deleted_by) VALUES (?, ?, ?, ?)",
                (row["id"], json.dumps(snapshot, ensure_ascii=False), now_text(), payload.username.strip()),
            )
            # Keep completed task history, but detach it from the asset before
            # deleting the asset row so SQLite foreign-key checks remain valid.
            conn.execute("UPDATE job_events SET asset_id = NULL WHERE asset_id = ?", (row["id"],))
            conn.execute("UPDATE background_jobs SET asset_id = NULL WHERE asset_id = ?", (row["id"],))
            conn.execute("DELETE FROM duplicate_reviews WHERE asset_id = ? OR candidate_asset_id = ?", (row["id"], row["id"]))
            conn.execute("DELETE FROM asset_annotations WHERE asset_id = ?", (row["id"],))
            conn.execute("DELETE FROM asset_tags WHERE asset_id = ?", (row["id"],))
            conn.execute("DELETE FROM asset_visual_hashes WHERE asset_id = ?", (row["id"],))
            conn.execute("DELETE FROM asset_source_audits WHERE asset_id = ?", (row["id"],))
            conn.execute("DELETE FROM asset_derivatives WHERE asset_id = ?", (row["id"],))
            conn.execute("DELETE FROM assets WHERE id = ?", (row["id"],))
            removed_ids.append(row["id"])
    return {"status": "ok", "removed": len(removed_ids), "assetIds": removed_ids, "message": "已仅从数据库移除所选项目；NAS 文件未改动。"}


@app.get("/api/database-review/trash")
def list_deleted_assets() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM deleted_assets ORDER BY deleted_at DESC LIMIT 500").fetchall()
    items: list[dict[str, object]] = []
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot"])
            asset = snapshot.get("asset", {})
        except json.JSONDecodeError:
            asset = {}
        items.append({"deletedId": row["id"], "originalAssetId": row["original_asset_id"], "detectedName": asset.get("detected_name", "未知"), "path": asset.get("source_path", ""), "deletedAt": row["deleted_at"], "deletedBy": row["deleted_by"], "reason": row["reason"]})
    return {"items": items}


@app.post("/api/database-review/restore")
def restore_deleted_assets(payload: RestoreDeletedAssetsRequest) -> dict[str, object]:
    deleted_ids = unique_asset_ids(payload.deleted_ids)
    if not deleted_ids:
        raise HTTPException(status_code=400, detail="未选择要恢复的项目")
    restored: list[int] = []
    skipped: list[dict[str, object]] = []
    with get_db() as conn:
        placeholders = ",".join("?" for _ in deleted_ids)
        rows = conn.execute(f"SELECT * FROM deleted_assets WHERE id IN ({placeholders})", deleted_ids).fetchall()
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot"])
                asset = snapshot["asset"]
            except (json.JSONDecodeError, KeyError) as exc:
                skipped.append({"deletedId": row["id"], "reason": f"快照损坏：{exc}"})
                continue
            if conn.execute("SELECT 1 FROM assets WHERE source_path = ?", (asset["source_path"],)).fetchone():
                skipped.append({"deletedId": row["id"], "reason": "相同源路径已重新入库，未覆盖。"})
                continue
            recycle_path = snapshot.get("source_recycle_path", "")
            derivative_recycles = snapshot.get("derivative_recycles", [])
            if not isinstance(derivative_recycles, list):
                skipped.append({"deletedId": row["id"], "reason": "派生文件回收站快照格式错误。"})
                continue
            files_to_restore: list[tuple[Path, Path, str]] = []
            if recycle_path:
                source_file = Path(asset["source_path"])
                recycled_file = Path(recycle_path)
                if source_file.exists():
                    skipped.append({"deletedId": row["id"], "reason": "原路径已有文件，未覆盖恢复。"})
                    continue
                if not recycled_file.exists():
                    skipped.append({"deletedId": row["id"], "reason": "回收站中的 NAS 原图不存在，无法恢复。"})
                    continue
                files_to_restore.append((recycled_file, source_file, "NAS 原图"))
            invalid_derivative_recycle = False
            for item in derivative_recycles:
                if not isinstance(item, dict) or not item.get("original_path") or not item.get("recycle_path"):
                    skipped.append({"deletedId": row["id"], "reason": "派生文件回收站快照缺少路径。"})
                    invalid_derivative_recycle = True
                    break
                original_file = Path(str(item["original_path"]))
                recycled_file = Path(str(item["recycle_path"]))
                if original_file.exists():
                    skipped.append({"deletedId": row["id"], "reason": f"1280 输出路径已有文件，未覆盖恢复：{original_file.name}"})
                    invalid_derivative_recycle = True
                    break
                if not recycled_file.exists():
                    skipped.append({"deletedId": row["id"], "reason": f"回收站中的 1280 文件不存在，无法恢复：{original_file.name}"})
                    invalid_derivative_recycle = True
                    break
                files_to_restore.append((recycled_file, original_file, "1280 文件"))
            if invalid_derivative_recycle:
                continue
            moved_files: list[tuple[Path, Path]] = []
            try:
                for recycled_file, original_file, _kind in files_to_restore:
                    original_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(recycled_file), str(original_file))
                    moved_files.append((original_file, recycled_file))
            except OSError as exc:
                for original_file, recycled_file in reversed(moved_files):
                    try:
                        recycled_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(original_file), str(recycled_file))
                    except OSError:
                        pass
                skipped.append({"deletedId": row["id"], "reason": f"恢复文件失败，已尽力回滚：{exc}"})
                continue
            columns = ["source_path", "library_name", "detected_name", "resolution", "ratio", "file_size_bytes", "tags", "imported_at", "downloads", "clicks", "source_type", "ocr_text", "ocr_status", "ocr_error", "source_author"]
            values = [asset.get(column, "" if column in {"source_path", "library_name", "detected_name", "resolution", "ratio", "tags", "imported_at", "source_type", "ocr_text", "ocr_status", "ocr_error", "source_author"} else 0) for column in columns]
            conn.execute(f"INSERT INTO assets({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
            new_asset_id = conn.execute("SELECT id FROM assets WHERE source_path = ?", (asset["source_path"],)).fetchone()["id"]
            for derivative in snapshot.get("derivatives", []):
                try:
                    conn.execute(
                        """INSERT INTO asset_derivatives(asset_id, kind, output_path, sequence_number, title, author, channel, source_resolution, source_size_bytes, was_stretched, output_size_bytes, output_resolution, output_format, generated_at, status, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (new_asset_id, derivative.get("kind", "16:9"), derivative["output_path"], derivative["sequence_number"], derivative["title"], derivative.get("author", "未识别作者"), derivative.get("channel", ""), derivative.get("source_resolution", "未知"), derivative.get("source_size_bytes", 0), derivative.get("was_stretched", 0), derivative.get("output_size_bytes", 0), derivative.get("output_resolution", "1280 x 720"), derivative.get("output_format", ""), derivative.get("generated_at", now_text()), derivative.get("status", "done"), derivative.get("error", "")),
                    )
                except sqlite3.IntegrityError:
                    pass
            audit = snapshot.get("audit")
            if audit:
                conn.execute("INSERT OR REPLACE INTO asset_source_audits(asset_id, status, detail, checked_at) VALUES (?, ?, ?, ?)", (new_asset_id, audit.get("status", "missing"), audit.get("detail", ""), audit.get("checked_at", now_text())))
            for tag in snapshot.get("tags", []):
                if isinstance(tag, dict) and tag.get("tag_id"):
                    conn.execute(
                        "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id, created_at) VALUES (?, ?, ?)",
                        (new_asset_id, tag["tag_id"], tag.get("created_at", now_text())),
                    )
            annotation = snapshot.get("annotation")
            if isinstance(annotation, dict):
                conn.execute(
                    "INSERT OR REPLACE INTO asset_annotations(asset_id, confirmed_at) VALUES (?, ?)",
                    (new_asset_id, annotation.get("confirmed_at", now_text())),
                )
            visual_hash = snapshot.get("visual_hash")
            if isinstance(visual_hash, dict) and visual_hash.get("visual_hash"):
                conn.execute(
                    "INSERT OR REPLACE INTO asset_visual_hashes(asset_id, visual_hash, checked_at) VALUES (?, ?, ?)",
                    (new_asset_id, visual_hash["visual_hash"], visual_hash.get("checked_at", now_text())),
                )
            conn.execute("DELETE FROM deleted_assets WHERE id = ?", (row["id"],))
            restored.append(new_asset_id)
    return {"status": "ok", "restored": len(restored), "assetIds": restored, "skipped": skipped}


def parse_resolution(resolution: str) -> Optional[tuple[int, int]]:
    match = re.search(r"(\d+)\s*[xX×]\s*(\d+)", resolution or "")
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    return (width, height) if width > 0 and height > 0 else None


def derivative_output_root() -> Path:
    return Path(load_config().app.derivative_output_path)


def derivative_output_dir(kind: str) -> Path:
    spec = DERIVATIVE_SPECS.get(kind)
    if not spec:
        raise ValueError(f"不支持的派生比例：{kind}")
    return derivative_output_root() / str(spec["folder"])


def choose_derivative_spec(resolution: str) -> tuple[str, tuple[int, int], float] | None:
    size = parse_resolution(resolution)
    if not size:
        return None
    source_ratio = size[0] / size[1]
    candidates = [
        (kind, tuple(spec["size"]), abs(source_ratio / (spec["size"][0] / spec["size"][1]) - 1) * 100)
        for kind, spec in DERIVATIVE_SPECS.items()
    ]
    return min(candidates, key=lambda item: item[2])


def normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def is_path_within(path: str | Path, root: str | Path) -> bool:
    normalized_candidate = normalized_path(path)
    normalized_root = normalized_path(root).rstrip("\\/")
    return normalized_candidate == normalized_root or normalized_candidate.startswith(normalized_root + os.sep)


def normalize_author(author: str) -> str:
    return re.sub(r"\s+", " ", author).strip()[:120]


def normalize_channel(channel: str) -> str:
    return re.sub(r"\s+", " ", channel).strip()[:120]


def remember_derivative_author(author: str) -> None:
    normalized = normalize_author(author)
    if not normalized or normalized == "未识别作者":
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO derivative_authors(name, last_used_at) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET last_used_at = excluded.last_used_at",
            (normalized, now_text()),
        )


def remember_derivative_channel(channel: str) -> None:
    normalized = normalize_channel(channel)
    if not normalized:
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO derivative_channels(name, last_used_at) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET last_used_at = excluded.last_used_at",
            (normalized, now_text()),
        )


def author_from_source_path(source_path: str) -> str:
    normalized = urllib.parse.unquote(urllib.parse.urlparse(source_path).path) if is_http_path(source_path) else source_path
    parts = [part for part in re.split(r"[\\/]+", normalized) if part]
    try:
        marker = parts.index("图片素材")
    except ValueError:
        return "未识别作者"
    candidate = parts[marker + 1] if marker + 1 < len(parts) - 1 else "未识别作者"
    return candidate if candidate.casefold() != "database" else "未识别作者"


def display_author_for_row(row: sqlite3.Row) -> str:
    display_version = normalize_display_version(str(row["display_version"])) if "display_version" in row.keys() else "raw"
    derivative_author = str(row["derivative_author"] or "") if "derivative_author" in row.keys() else ""
    if display_version != "raw":
        return derivative_author if derivative_author and derivative_author != "未识别作者" else "未识别作者"
    source_author = str(row["source_author"] or "") if "source_author" in row.keys() else ""
    if source_author and source_author != "未识别作者":
        return source_author
    fallback = author_from_source_path(str(row["source_path"]))
    return fallback if fallback else "未识别作者"


def derivative_title(row: sqlite3.Row) -> str:
    title = (row["detected_name"] or row["library_name"] or Path(row["source_path"]).stem).strip()
    return safe_filename(title, f"图片-{row['id']}")[:120]


def inspect_sequence_numbers(output_root: Path) -> tuple[set[int], list[int], list[int]]:
    used: set[int] = set()
    counts: dict[int, int] = {}
    with get_db() as conn:
        rows = conn.execute("SELECT sequence_number, output_path FROM asset_derivatives").fetchall()
    registered_paths = {normalized_path(row["output_path"]) for row in rows if str(row["output_path"] or "").strip()}
    for row in rows:
        number = int(row["sequence_number"])
        used.add(number)
        counts[number] = counts.get(number, 0) + 1
    try:
        if output_root.exists():
            for file_path in output_root.rglob("*"):
                if not file_path.is_file():
                    continue
                if ".素材库回收站" in file_path.parts:
                    continue
                if normalized_path(file_path) in registered_paths:
                    continue
                match = re.match(r"^(\d+)-", file_path.name)
                if match:
                    number = int(match.group(1))
                    used.add(number)
                    counts[number] = counts.get(number, 0) + 1
    except OSError:
        pass
    maximum = max(used, default=0)
    missing = [number for number in range(1, maximum + 1) if number not in used]
    duplicates = sorted(number for number, count in counts.items() if count > 1)
    return used, missing, duplicates


def record_sequence_audit(output_root: Path, missing: list[int], duplicates: list[int], note: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sequence_audits(checked_at, output_root, missing_numbers, duplicate_numbers, note) VALUES (?, ?, ?, ?, ?)",
            (now_text(), str(output_root), json.dumps(missing, ensure_ascii=False), json.dumps(duplicates, ensure_ascii=False), note),
        )


def allocate_sequence(used: set[int]) -> tuple[int, bool]:
    number = 1
    while number in used:
        number += 1
    was_gap_fill = number < max(used, default=0)
    used.add(number)
    return number, was_gap_fill


def derivative_preflight(asset_ids: list[int], author_override: str = "", channel: str = "", force_jpeg: bool = False) -> dict[str, object]:
    requested_ids = unique_asset_ids(asset_ids)
    if not requested_ids:
        raise HTTPException(status_code=400, detail="未选择要导出的图片")
    with get_db() as conn:
        placeholders = ",".join("?" for _ in requested_ids)
        rows = conn.execute(
            f"""SELECT assets.*, d.id AS derivative_id, d.kind AS derivative_kind, d.output_path AS derivative_path,
            d.sequence_number AS derivative_sequence, d.status AS derivative_status, d.author AS derivative_author, d.channel AS derivative_channel
            FROM assets LEFT JOIN asset_derivatives d ON d.id = (
                SELECT existing.id FROM asset_derivatives existing
                WHERE existing.asset_id = assets.id
                ORDER BY CASE existing.status WHEN 'done' THEN 0 ELSE 1 END, existing.id ASC LIMIT 1
            )
            WHERE assets.id IN ({placeholders}) AND assets.hidden_at IS NULL ORDER BY assets.imported_at ASC""",
            requested_ids,
        ).fetchall()
    output_root = derivative_output_root()
    used, missing, duplicates = inspect_sequence_numbers(output_root)
    record_sequence_audit(output_root, missing, duplicates, "导出预检")
    output_root_available = output_root.exists()
    items: list[dict[str, object]] = []
    requested_author = normalize_author(author_override)
    requested_channel = normalize_channel(channel)
    for row in rows:
        # Older SMB scans stored "未知" without inspecting the image.  Repair
        # that stale metadata on demand so a user can export immediately after
        # upgrading, even before the next scheduled scan runs.
        if not parse_resolution(row["resolution"]) and not is_http_path(row["source_path"]):
            resolution, ratio = read_image_size(Path(row["source_path"]))
            if parse_resolution(resolution):
                with get_db() as conn:
                    conn.execute(
                        "UPDATE assets SET resolution=?,ratio=? WHERE id=? AND resolution=?",
                        (resolution, ratio, row["id"], row["resolution"]),
                    )
                row = dict(row)
                row["resolution"] = resolution
                row["ratio"] = ratio
        source_size = row["file_size_bytes"] or 0
        title = derivative_title(row)
        author = requested_author or row["source_author"] or author_from_source_path(row["source_path"])
        item: dict[str, object] = {
            "id": row["id"], "title": title, "author": author, "channel": requested_channel, "sourcePath": row["source_path"], "sourceResolution": row["resolution"],
            "sourceSizeMb": round(source_size / 1024 / 1024, 3), "status": "ready", "warnings": [], "message": "可生成。",
            "existingSequence": row["derivative_sequence"], "existingPath": row["derivative_path"] or "", "forceJpegIgnored": bool(force_jpeg),
        }
        if row["derivative_id"] and row["derivative_status"] == "done":
            item.update({"status": "already_generated", "message": f"已生成 {row['derivative_kind']} 派生图；每张原图只允许一个派生图。", "sequence": row["derivative_sequence"], "outputPath": row["derivative_path"], "author": row["derivative_author"] or author, "kind": row["derivative_kind"], "channel": row["derivative_channel"] or requested_channel})
            items.append(item)
            continue
        selected = choose_derivative_spec(row["resolution"])
        if not selected:
            item.update({"status": "unknown_resolution", "message": "无法从入库记录获得分辨率，已跳过。"})
        else:
            kind, target_size, deviation = selected
            native_size = parse_resolution(row["resolution"]) == target_size
            item["ratioDeviationPercent"] = round(deviation, 2)
            if deviation > 10:
                item.update({"status": "ratio_too_far", "message": f"与最接近的 {kind} 比例偏差 {deviation:.2f}% 超过 10%，不裁切也不拉伸。", "kind": kind})
            else:
                sequence, was_gap_fill = allocate_sequence(used)
                output_path = derivative_output_dir(kind) / f"{sequence}-{title}.jpg"
                item.update({"kind": kind, "targetResolution": f"{target_size[0]} x {target_size[1]}", "sequence": sequence, "willFillGap": was_gap_fill, "outputPath": str(output_path), "wasStretched": not native_size, "nativeSize": native_size, "outputFormat": "JPEG"})
                if native_size:
                    item["message"] = f"原图已是 {target_size[0]}×{target_size[1]}，不重采样，仅写入元数据并输出 JPG。"
                if source_size > 1024 * 1024:
                    item["warnings"].append("原图大于 1 MB。")
        items.append(item)
    return {
        "outputRoot": str(output_root), "outputRootExists": output_root_available, "missingNumbers": missing, "duplicateNumbers": duplicates,
        "items": items, "summary": {"total": len(items), "ready": sum(1 for item in items if item["status"] == "ready"), "alreadyGenerated": sum(1 for item in items if item["status"] == "already_generated"), "skipped": sum(1 for item in items if item["status"] not in {"ready", "already_generated"})},
    }


@app.post("/api/derivatives/preflight")
def preflight_derivatives(payload: DerivativePreflightRequest) -> dict[str, object]:
    return derivative_preflight(payload.asset_ids, payload.author, payload.channel, payload.force_jpeg)


@app.post("/api/derivatives/folder-preflight")
def preflight_folder_derivatives(payload: DerivativeFolderPreflightRequest) -> dict[str, object]:
    folder = payload.folder.strip()
    if not folder:
        raise HTTPException(status_code=400, detail="请选择要导出的文件夹")
    with get_db() as conn:
        rows = conn.execute("SELECT id, source_path FROM assets WHERE hidden_at IS NULL").fetchall()
    asset_ids = [row["id"] for row in rows if folder_from_source_path(row["source_path"]) == folder]
    if not asset_ids:
        raise HTTPException(status_code=404, detail="该文件夹没有已入库的图片，请先扫描入库。")
    result = derivative_preflight(asset_ids, payload.author, payload.channel, payload.force_jpeg)
    result["folder"] = folder
    return result


@app.get("/api/derivatives/sequence-audits")
def list_sequence_audits() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM sequence_audits ORDER BY checked_at DESC LIMIT 100").fetchall()
    items = [{"id": row["id"], "checkedAt": row["checked_at"], "outputRoot": row["output_root"], "missingNumbers": json.loads(row["missing_numbers"]), "duplicateNumbers": json.loads(row["duplicate_numbers"]), "note": row["note"]} for row in rows]
    return {"items": items}


def legacy_1280_migration_plan() -> list[dict[str, object]]:
    root = derivative_output_root()
    target_root = derivative_output_dir("16:9")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, asset_id, output_path, sequence_number, title FROM asset_derivatives WHERE kind = '16:9' AND status = 'done' ORDER BY sequence_number"
        ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows:
        source = Path(row["output_path"])
        target = target_root / source.name
        item: dict[str, object] = {"derivativeId": row["id"], "assetId": row["asset_id"], "sourcePath": str(source), "targetPath": str(target), "title": row["title"], "sequence": row["sequence_number"], "status": "ready", "reason": "可迁移；执行后可按日志回滚。"}
        if normalized_path(source) == normalized_path(target):
            item.update({"status": "already_migrated", "reason": "已在 DataBase\\16x9 目录。"})
        elif not is_path_within(source, root):
            item.update({"status": "outside_root", "reason": "登记文件不在当前 DataBase 根目录，不自动移动。"})
        elif not source.is_file():
            item.update({"status": "missing", "reason": "登记的旧 1280 文件不存在或不可访问。"})
        elif target.exists():
            item.update({"status": "target_exists", "reason": "目标路径已有文件，为避免覆盖而跳过。"})
        items.append(item)
    return items


@app.get("/api/derivatives/legacy-1280/preflight")
def preflight_legacy_1280_migration() -> dict[str, object]:
    items = legacy_1280_migration_plan()
    return {"outputRoot": str(derivative_output_root()), "targetRoot": str(derivative_output_dir("16:9")), "items": items, "summary": {"total": len(items), "ready": sum(item["status"] == "ready" for item in items), "skipped": sum(item["status"] != "ready" for item in items)}}


@app.post("/api/derivatives/legacy-1280/migrate")
def migrate_legacy_1280_files(payload: LegacyDerivativeMigrationRequest) -> dict[str, object]:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="旧 1280 文件迁移需要显式确认。请先执行预检并确认目标路径。")
    requested = set(payload.migration_ids)
    plan = [item for item in legacy_1280_migration_plan() if not requested or item["derivativeId"] in requested]
    results: list[dict[str, object]] = []
    for item in plan:
        derivative_id = int(item["derivativeId"])
        source = Path(str(item["sourcePath"]))
        target = Path(str(item["targetPath"]))
        if item["status"] != "ready":
            results.append({**item, "status": "skipped"})
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            try:
                with get_db() as conn:
                    conn.execute("UPDATE asset_derivatives SET output_path = ? WHERE id = ?", (str(target), derivative_id))
                    conn.execute("INSERT INTO legacy_derivative_migrations(derivative_id, source_path, target_path, action, status, reason, created_at, completed_at) VALUES (?, ?, ?, 'migrate', 'done', ?, ?, ?)", (derivative_id, str(source), str(target), "旧 1280 文件迁移至 16x9", now_text(), now_text()))
            except Exception:
                shutil.move(str(target), str(source))
                raise
            results.append({**item, "status": "done", "reason": "已迁移并写入可回滚日志。"})
        except Exception as exc:
            with get_db() as conn:
                conn.execute("INSERT INTO legacy_derivative_migrations(derivative_id, source_path, target_path, action, status, reason, created_at) VALUES (?, ?, ?, 'migrate', 'failed', ?, ?)", (derivative_id, str(source), str(target), str(exc), now_text()))
            results.append({**item, "status": "failed", "reason": str(exc)})
    return {"status": "ok", "migrated": sum(item["status"] == "done" for item in results), "skipped": sum(item["status"] == "skipped" for item in results), "failed": sum(item["status"] == "failed" for item in results), "items": results}


@app.get("/api/derivatives/legacy-1280/logs")
def list_legacy_1280_migration_logs() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM legacy_derivative_migrations ORDER BY id DESC LIMIT 200").fetchall()
    return {"items": [{"id": row["id"], "derivativeId": row["derivative_id"], "sourcePath": row["source_path"], "targetPath": row["target_path"], "action": row["action"], "status": row["status"], "reason": row["reason"], "createdAt": row["created_at"], "completedAt": row["completed_at"]} for row in rows]}


@app.post("/api/derivatives/legacy-1280/rollback")
def rollback_legacy_1280_migration(payload: LegacyDerivativeMigrationRequest) -> dict[str, object]:
    if not payload.confirm or not payload.migration_ids:
        raise HTTPException(status_code=400, detail="回滚需要显式确认并选择至少一条迁移日志。")
    placeholders = ",".join("?" for _ in payload.migration_ids)
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM legacy_derivative_migrations WHERE id IN ({placeholders}) AND action = 'migrate' AND status = 'done'", payload.migration_ids).fetchall()
    results: list[dict[str, object]] = []
    for row in rows:
        source, target = Path(row["source_path"]), Path(row["target_path"])
        try:
            if source.exists():
                raise RuntimeError("旧路径已有文件，未覆盖回滚。")
            if not target.is_file():
                raise RuntimeError("迁移后的文件不存在，无法回滚。")
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))
            with get_db() as conn:
                conn.execute("UPDATE asset_derivatives SET output_path = ? WHERE id = ?", (str(source), row["derivative_id"]))
                conn.execute("INSERT INTO legacy_derivative_migrations(derivative_id, source_path, target_path, action, status, reason, created_at, completed_at) VALUES (?, ?, ?, 'rollback', 'done', '按迁移日志恢复旧路径', ?, ?)", (row["derivative_id"], str(target), str(source), now_text(), now_text()))
            results.append({"id": row["id"], "status": "done"})
        except Exception as exc:
            results.append({"id": row["id"], "status": "failed", "reason": str(exc)})
    return {"status": "ok", "rolledBack": sum(item["status"] == "done" for item in results), "failed": sum(item["status"] == "failed" for item in results), "items": results}


@app.get("/api/derivatives/authors")
def list_derivative_authors() -> dict[str, list[str]]:
    with get_db() as conn:
        rows = conn.execute("SELECT name FROM derivative_authors ORDER BY last_used_at DESC, name COLLATE NOCASE ASC LIMIT 100").fetchall()
    return {"items": [row["name"] for row in rows]}


@app.get("/api/derivatives/channels")
def list_derivative_channels() -> dict[str, list[str]]:
    with get_db() as conn:
        rows = conn.execute("SELECT name FROM derivative_channels ORDER BY last_used_at DESC, name COLLATE NOCASE ASC LIMIT 100").fetchall()
    return {"items": [row["name"] for row in rows]}


def set_author_exif(exif, author: str, previous_author: str = "") -> None:
    if author.isascii():
        exif[315] = author  # EXIF Artist only reliably supports ASCII text.
    elif 315 in exif:
        del exif[315]  # Prevent Windows from preferring the garbled legacy Artist value.
    exif[40093] = author.encode("utf-16le") + b"\x00\x00"  # Windows XPAuthor supports Chinese.
    old_keyword_value = exif.get(40094)
    if previous_author and isinstance(old_keyword_value, bytes):
        old_keyword_text = old_keyword_value.decode("utf-16le", errors="ignore").rstrip("\x00").strip()
        if old_keyword_text == previous_author:
            del exif[40094]  # Remove only the old version's mistaken XPKeywords value.


def write_derivative_metadata(output_path: Path, author: str, channel: str, previous_author: str = "") -> tuple[int, str]:
    try:
        from PIL import Image, ImageOps, PngImagePlugin
    except ModuleNotFoundError as exc:
        raise RuntimeError("未安装 Pillow，无法更新文件作者或获取渠道元数据。请运行 python -m pip install Pillow。") from exc
    temp_path = output_path.with_suffix(output_path.suffix + ".partial")
    try:
        with Image.open(output_path) as source:
            image = ImageOps.exif_transpose(source)
            exif = source.getexif()
            set_author_exif(exif, author, previous_author)
            exif[40092] = f"获取渠道：{channel}".encode("utf-16le") + b"\x00\x00"
            suffix = output_path.suffix.lower()
            if suffix in {".jpg", ".jpeg"}:
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(temp_path, format="JPEG", quality=95, optimize=True, exif=exif.tobytes())
                output_format = "JPEG"
            elif suffix == ".png":
                metadata = PngImagePlugin.PngInfo()
                metadata.add_text("Author", author)
                metadata.add_text("Comment", f"获取渠道：{channel}")
                metadata.add_text("Channel", channel)
                image.save(temp_path, format="PNG", optimize=True, pnginfo=metadata)
                output_format = "PNG"
            elif suffix == ".webp":
                image.save(temp_path, format="WEBP", quality=95, method=6, exif=exif.tobytes())
                output_format = "WEBP"
            else:
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(temp_path, format="JPEG", quality=95, optimize=True, exif=exif.tobytes())
                output_format = "JPEG"
        os.replace(temp_path, output_path)
        return output_path.stat().st_size, output_format
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def save_derivative(row: sqlite3.Row, output_path: Path, target_size: tuple[int, int], author: str, channel: str) -> tuple[int, str]:
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as exc:
        raise RuntimeError("未安装 Pillow，无法生成派生图。请运行 python -m pip install Pillow。") from exc
    _, data = read_asset_bytes(row)
    temp_path = output_path.with_suffix(output_path.suffix + ".partial")
    try:
        with Image.open(io.BytesIO(data)) as source:
            exif = source.getexif()
            set_author_exif(exif, author)
            exif[270] = f"作者：{author}；获取渠道：{channel}；来源：{row['source_path']}"
            exif[40092] = f"获取渠道：{channel}".encode("utf-16le") + b"\x00\x00"
            image = ImageOps.exif_transpose(source)
            if image.size != target_size:
                image = image.resize(target_size, Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(temp_path, format="JPEG", quality=95, optimize=True, exif=exif.tobytes())
            output_format = "JPEG"
        os.replace(temp_path, output_path)
        return output_path.stat().st_size, output_format
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


@app.post("/api/derivatives/generate")
def generate_derivatives(payload: DerivativeGenerateRequest) -> dict[str, object]:
    channel = normalize_channel(payload.channel)
    if not channel:
        raise HTTPException(status_code=400, detail="请填写获取渠道后再生成派生图。")
    plan = derivative_preflight(payload.asset_ids, payload.author, channel, payload.force_jpeg)
    ready_items = [item for item in plan["items"] if item["status"] == "ready"]
    if not ready_items:
        return {"status": "ok", "generated": 0, "failed": 0, "items": [], "message": "没有可生成的项目；请先处理比例偏差或已生成项。"}
    try:
        for kind in {str(item["kind"]) for item in ready_items}:
            derivative_output_dir(kind).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"无法创建或访问输出目录：{exc}") from exc
    ready_ids = [int(item["id"]) for item in ready_items]
    with get_db() as conn:
        placeholders = ",".join("?" for _ in ready_ids)
        source_rows = conn.execute(f"SELECT * FROM assets WHERE id IN ({placeholders})", ready_ids).fetchall()
    rows_by_id = {row["id"]: row for row in source_rows}
    results: list[dict[str, object]] = []
    for item in ready_items:
        asset_id = int(item["id"])
        row = rows_by_id.get(asset_id)
        output_path = Path(str(item["outputPath"]))
        if not row:
            results.append({"id": asset_id, "status": "failed", "message": "原始数据库记录不存在。"})
            continue
        if output_path.exists():
            results.append({"id": asset_id, "status": "failed", "message": f"输出文件已存在，未覆盖：{output_path}"})
            continue
        try:
            kind = str(item["kind"])
            target_size = tuple(DERIVATIVE_SPECS[kind]["size"])
            output_size, output_format = save_derivative(row, output_path, target_size, str(item["author"]), channel)
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO asset_derivatives(asset_id, kind, output_path, sequence_number, title, author, channel, source_resolution, source_size_bytes, was_stretched, output_size_bytes, output_resolution, output_format, generated_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done')""",
                    (asset_id, kind, str(output_path), int(item["sequence"]), str(item["title"]), str(item["author"]), channel, row["resolution"], row["file_size_bytes"], 1 if item.get("wasStretched") else 0, output_size, f"{target_size[0]} x {target_size[1]}", output_format, now_text()),
                )
            remember_derivative_author(str(item["author"]))
            remember_derivative_channel(channel)
            warnings = list(item.get("warnings", []))
            if output_size > 1024 * 1024:
                warnings.append("生成版本大于 1 MB。")
            results.append({"id": asset_id, "status": "done", "sequence": item["sequence"], "outputPath": str(output_path), "outputSizeMb": round(output_size / 1024 / 1024, 3), "warnings": warnings})
        except Exception as exc:
            results.append({"id": asset_id, "status": "failed", "message": str(exc)})
    output_root = derivative_output_root()
    used, missing, duplicates = inspect_sequence_numbers(output_root)
    record_sequence_audit(output_root, missing, duplicates, f"导出完成：成功 {sum(1 for item in results if item['status'] == 'done')}，失败 {sum(1 for item in results if item['status'] == 'failed')}")
    return {"status": "ok", "generated": sum(1 for item in results if item["status"] == "done"), "failed": sum(1 for item in results if item["status"] == "failed"), "items": results, "message": "生成完成。"}


@app.post("/api/derivatives/update-author")
def update_derivative_author(payload: DerivativeAuthorUpdateRequest) -> dict[str, object]:
    requested_ids = unique_asset_ids(payload.asset_ids)
    author = normalize_author(payload.author)
    channel = normalize_channel(payload.channel)
    if not requested_ids:
        raise HTTPException(status_code=400, detail="未选择已导出的图片。")
    if not author and not channel:
        raise HTTPException(status_code=400, detail="请至少填写作者或获取渠道。")
    placeholders = ",".join("?" for _ in requested_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, asset_id, kind, output_path, author, channel FROM asset_derivatives WHERE status = 'done' AND asset_id IN ({placeholders})",
            requested_ids,
        ).fetchall()
    derivatives_by_asset = {row["asset_id"]: row for row in rows}
    results: list[dict[str, object]] = []
    for asset_id in requested_ids:
        derivative = derivatives_by_asset.get(asset_id)
        if not derivative:
            results.append({"id": asset_id, "status": "skipped", "message": "尚未生成派生图。"})
            continue
        output_path = Path(derivative["output_path"])
        if not output_path.is_file():
            results.append({"id": asset_id, "status": "failed", "message": "已导出文件不存在，未更新数据库。"})
            continue
        try:
            next_author = author or derivative["author"] or "未识别作者"
            next_channel = channel or derivative["channel"] or ""
            output_size, output_format = write_derivative_metadata(output_path, next_author, next_channel, derivative["author"] or "")
            with get_db() as conn:
                conn.execute(
                    "UPDATE asset_derivatives SET author = ?, channel = ?, output_size_bytes = ?, output_format = ? WHERE id = ?",
                    (next_author, next_channel, output_size, output_format, derivative["id"]),
                )
            results.append({"id": asset_id, "status": "updated", "kind": derivative["kind"], "message": "作者、获取渠道元数据和数据库记录已更新。"})
        except Exception as exc:
            results.append({"id": asset_id, "status": "failed", "message": str(exc)})
    if any(item["status"] == "updated" for item in results):
        if author:
            remember_derivative_author(author)
        if channel:
            remember_derivative_channel(channel)
    return {
        "status": "ok",
        "updated": sum(1 for item in results if item["status"] == "updated"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "items": results,
        "message": "已导出文件信息更新完成。",
    }


def append_folder_and_ratio_filters(filters: list[str], params: list[object], folder: str, ratio: str) -> None:
    """Apply the same source-folder and ratio scope to OCR and duplicate review."""
    if folder.strip():
        normalized_folder = folder.strip().replace("/", "\\").rstrip("\\")
        filters.append("LOWER(REPLACE(source_path, '/', '\\')) LIKE LOWER(?)")
        params.append(f"{normalized_folder}\\%")
    normalized_ratio = ratio.strip()
    standard_ratios = {"16:9", "1:1", "9:16"}
    if normalized_ratio in standard_ratios:
        filters.append("ratio = ?")
        params.append(normalized_ratio)
    elif normalized_ratio == "other":
        filters.append("(ratio NOT IN ('16:9', '1:1', '9:16') OR ratio IS NULL OR ratio = '')")


@app.get("/api/ocr/summary", response_model=OcrSummaryResponse)
def get_ocr_summary(exclude_path: str = "", folder: str = "", ratio: str = "") -> OcrSummaryResponse:
    unrecognized_where = "(ocr_status IS NULL OR ocr_status = '' OR ocr_status IN ('pending', 'empty', 'failed', 'missing_dependency') OR ocr_text = '')"
    filters: list[str] = ["hidden_at IS NULL"]
    params: list[object] = []
    if exclude_path.strip():
        normalized_exclude_path = exclude_path.strip().replace("/", "\\").rstrip("\\")
        if normalized_exclude_path:
            exclude_prefix = f"{normalized_exclude_path}\\"
            filters.append("SUBSTR(LOWER(source_path), 1, LENGTH(?)) != LOWER(?)")
            params.extend([exclude_prefix, exclude_prefix])
    append_folder_and_ratio_filters(filters, params, folder, ratio)
    filter_sql = f" AND {' AND '.join(filters)}" if filters else ""
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM assets WHERE 1 = 1{filter_sql}", params).fetchone()["count"]
        done = conn.execute(f"SELECT COUNT(*) AS count FROM assets WHERE ocr_status = 'done' AND ocr_text != ''{filter_sql}", params).fetchone()["count"]
        failed = conn.execute(f"SELECT COUNT(*) AS count FROM assets WHERE ocr_status IN ('failed', 'missing_dependency'){filter_sql}", params).fetchone()["count"]
        running = conn.execute(f"SELECT COUNT(*) AS count FROM assets WHERE ocr_status = 'running'{filter_sql}", params).fetchone()["count"]
        rows = conn.execute(f"SELECT id FROM assets WHERE {unrecognized_where}{filter_sql} ORDER BY imported_at DESC", params).fetchall()
        failed_rows = conn.execute(
            f"SELECT id, detected_name, library_name, ocr_error, ocr_status FROM assets WHERE ocr_status IN ('failed', 'missing_dependency'){filter_sql} ORDER BY imported_at DESC",
            params,
        ).fetchall()
    return OcrSummaryResponse(
        total=total,
        unrecognized=len(rows),
        done=done,
        failed=failed,
        running=running,
        unrecognizedIds=[row["id"] for row in rows],
        failedIds=[row["id"] for row in failed_rows],
        failedItems=[
            OcrFailureItem(
                id=row["id"],
                name=row["detected_name"] or row["library_name"] or f"图片 {row['id']}",
                reason=row["ocr_error"] or "未记录具体原因，请重新识别后查看。",
                status=row["ocr_status"],
            )
            for row in failed_rows
        ],
    )


@app.get("/api/ocr/ignore-phrases")
def get_ocr_ignore_phrases() -> dict[str, list[str]]:
    return {"items": load_ocr_ignore_phrases()}


def normalized_duplicate_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def visual_signature(row: sqlite3.Row) -> str:
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as exc:
        raise RuntimeError("未安装 Pillow，无法进行画面查重。请运行 python -m pip install Pillow。") from exc
    _, data = read_asset_bytes(row)
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB").resize((24, 14), Image.Resampling.LANCZOS)
        pixel_data = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
        pixels = bytes(channel for pixel in pixel_data for channel in pixel)
    # Versioned raw colour fingerprint retains small visual differences that dHash discarded.
    return "v2:" + pixels.hex()


def visual_difference_percent(left: str, right: str) -> float:
    if not left.startswith("v2:") or not right.startswith("v2:"):
        return 100.0
    left_bytes = bytes.fromhex(left[3:])
    right_bytes = bytes.fromhex(right[3:])
    if len(left_bytes) != len(right_bytes) or not left_bytes:
        return 100.0
    return round(sum(abs(a - b) for a, b in zip(left_bytes, right_bytes)) * 100 / (255 * len(left_bytes)), 2)


def visual_source_fingerprint(row: sqlite3.Row) -> str:
    database_fingerprint = "|".join(
        str(row[key] or "") if key in row.keys() else ""
        for key in ("path_key", "file_modified_at", "file_size_fingerprint", "file_size_bytes")
    )
    source_path = str(row["source_path"] or "")
    if not source_path or is_http_path(source_path):
        return f"db:{database_fingerprint}"
    try:
        stat = Path(source_path).stat()
    except OSError:
        return f"db:{database_fingerprint}|stat:unavailable"
    return f"db:{database_fingerprint}|stat:{stat.st_size}:{stat.st_mtime_ns}"


def duplicate_text_quick_ratio(left: Counter[str], right: Counter[str], total_length: int) -> float:
    if total_length <= 0:
        return 0.0
    matches = sum(min(count, right.get(character, 0)) for character, count in left.items())
    return (2.0 * matches) / total_length


def run_duplicate_analysis(
    folder: str = "",
    ratio: str = "",
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, object]:
    with get_db() as conn:
        filters = ["hidden_at IS NULL"]
        params: list[object] = []
        append_folder_and_ratio_filters(filters, params, folder, ratio)
        rows = conn.execute(f"SELECT * FROM assets WHERE {' AND '.join(filters)} ORDER BY imported_at ASC", params).fetchall()
        existing_hashes = {
            row["asset_id"]: (row["visual_hash"], row["source_fingerprint"])
            for row in conn.execute("SELECT asset_id, visual_hash, source_fingerprint FROM asset_visual_hashes").fetchall()
        }
    comparable_count = sum(1 for row in rows if normalized_duplicate_text(row["ocr_text"]))
    pair_total = comparable_count * (comparable_count - 1) // 2
    total_work = len(rows) + pair_total
    if progress:
        progress(0, total_work, "准备画面指纹")
    hashes: dict[int, str] = {}
    errors: list[str] = []
    pending_hashes: list[tuple[int, str, str, str]] = []

    def flush_hashes() -> None:
        if not pending_hashes:
            return
        with get_db() as conn:
            conn.executemany(
                "INSERT INTO asset_visual_hashes(asset_id, visual_hash, checked_at, source_fingerprint) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET visual_hash=excluded.visual_hash, checked_at=excluded.checked_at, source_fingerprint=excluded.source_fingerprint",
                pending_hashes,
            )
        pending_hashes.clear()

    for index, row in enumerate(rows, start=1):
        try:
            fingerprint = visual_source_fingerprint(row)
            cached_hash, cached_fingerprint = existing_hashes.get(row["id"], ("", ""))
            visual_hash = cached_hash if cached_hash.startswith("v2:") and cached_fingerprint == fingerprint else ""
            if not visual_hash:
                visual_hash = visual_signature(row)
                pending_hashes.append((row["id"], visual_hash, now_text(), fingerprint))
                if len(pending_hashes) >= 24:
                    flush_hashes()
            hashes[row["id"]] = visual_hash
        except Exception as exc:
            label = row["detected_name"] or row["library_name"] or f"图片 {row['id']}"
            errors.append(f"{label}：{exc}")
        if progress and (index == len(rows) or index % 8 == 0):
            progress(index, total_work, f"生成画面指纹 {index}/{len(rows)}")
    flush_hashes()

    pairs: list[dict[str, object]] = []
    comparable_rows = []
    for row in rows:
        text = normalized_duplicate_text(row["ocr_text"])
        if row["id"] in hashes and text:
            comparable_rows.append((row, text, Counter(text)))
    compared = 0
    for index, left in enumerate(comparable_rows):
        left_row, left_text, left_counts = left
        for right_row, right_text, right_counts in comparable_rows[index + 1 :]:
            compared += 1
            combined_length = len(left_text) + len(right_text)
            theoretical_ratio = (2.0 * min(len(left_text), len(right_text))) / combined_length
            if theoretical_ratio >= 0.94 and duplicate_text_quick_ratio(left_counts, right_counts, combined_length) >= 0.94:
                similarity = difflib.SequenceMatcher(None, left_text, right_text).ratio()
                if similarity >= 0.94:
                    difference = visual_difference_percent(hashes[left_row["id"]], hashes[right_row["id"]])
                    if difference <= 5.0:
                        pairs.append({"leftId": left_row["id"], "rightId": right_row["id"], "visualDifferencePercent": difference, "textSimilarity": round(similarity * 100, 1)})
            if progress and (compared == pair_total or compared % 256 == 0):
                progress(len(rows) + compared, total_work, f"比较候选 {compared}/{pair_total}")
    rows_by_id = {row["id"]: row for row in rows}
    quality_order = sorted(rows_by_id, key=lambda asset_id: (-(parse_resolution(rows_by_id[asset_id]["resolution"]) or (0, 0))[0] * (parse_resolution(rows_by_id[asset_id]["resolution"]) or (0, 0))[1], -rows_by_id[asset_id]["file_size_bytes"]))
    pair_by_id: dict[int, list[dict[str, object]]] = {}
    for pair in pairs:
        pair_by_id.setdefault(int(pair["leftId"]), []).append(pair)
        pair_by_id.setdefault(int(pair["rightId"]), []).append(pair)
    groups = []
    assigned: set[int] = set()
    direct_pair_keys = {frozenset((int(pair["leftId"]), int(pair["rightId"]))) for pair in pairs}
    for reference_id in quality_order:
        if reference_id in assigned:
            continue
        direct_pairs = pair_by_id.get(reference_id, [])
        candidate_ids = {int(pair["rightId"] if pair["leftId"] == reference_id else pair["leftId"]) for pair in direct_pairs} - assigned
        compatible_ids: list[int] = []
        for candidate_id in sorted(candidate_ids, key=lambda asset_id: (-(parse_resolution(rows_by_id[asset_id]["resolution"]) or (0, 0))[0] * (parse_resolution(rows_by_id[asset_id]["resolution"]) or (0, 0))[1], -rows_by_id[asset_id]["file_size_bytes"])):
            if all(frozenset((candidate_id, existing_id)) in direct_pair_keys for existing_id in compatible_ids):
                compatible_ids.append(candidate_id)
        if not compatible_ids:
            continue
        ordered_ids = [reference_id, *compatible_ids]
        assigned.update(ordered_ids)
        group_pairs = [pair for pair in pairs if int(pair["leftId"]) in ordered_ids and int(pair["rightId"]) in ordered_ids]
        groups.append({"items": [asset_from_row(rows_by_id[asset_id]).model_dump() for asset_id in ordered_ids], "pairs": group_pairs})
    groups.sort(key=lambda group: -len(group["items"]))
    return {"status": "ok", "scanned": len(hashes), "groups": groups, "errors": errors, "message": f"已完成 {len(hashes)} 张图片的画面与 OCR 联合查重，发现 {len(groups)} 组候选。"}


@app.post("/api/duplicates/analyze")
def analyze_duplicates(payload: DuplicateAnalysisRequest) -> dict[str, int]:
    job_id = enqueue("duplicate", payload={"folder": payload.folder.strip(), "ratio": payload.ratio.strip()})
    return {"job_id": job_id}


@app.post("/api/duplicates/retain")
def retain_duplicate(payload: DuplicateRetainRequest) -> dict[str, object]:
    hidden_ids = [asset_id for asset_id in unique_asset_ids(payload.candidate_asset_ids) if asset_id != payload.keep_asset_id]
    if payload.keep_asset_id <= 0 or not hidden_ids:
        raise HTTPException(status_code=400, detail="请选择保留图片及至少一张要隐藏的候选。")
    now = now_text()
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM assets WHERE id = ?", (payload.keep_asset_id,)).fetchone():
            raise HTTPException(status_code=404, detail="要保留的图片不存在。")
        placeholders = ",".join("?" for _ in hidden_ids)
        existing = conn.execute(f"SELECT id FROM assets WHERE id IN ({placeholders}) AND hidden_at IS NULL", hidden_ids).fetchall()
        applicable = [row["id"] for row in existing]
        if applicable:
            applicable_placeholders = ",".join("?" for _ in applicable)
            conn.execute(f"UPDATE assets SET hidden_at = ?, hidden_reason = '重复审查保留' WHERE id IN ({applicable_placeholders})", [now, *applicable])
            for candidate_id in applicable:
                conn.execute("INSERT INTO duplicate_reviews(asset_id, candidate_asset_id, status, reason, created_at, updated_at) VALUES (?, ?, 'retained', '人工保留另一张候选', ?, ?) ON CONFLICT(asset_id, candidate_asset_id) DO UPDATE SET status='retained', reason='人工保留另一张候选', updated_at=excluded.updated_at", (payload.keep_asset_id, candidate_id, now, now))
    return {"status": "ok", "kept": payload.keep_asset_id, "hidden": len(applicable), "message": f"已保留该图片；仅隐藏其余 {len(applicable)} 条数据库记录，不移动或删除 NAS 文件。"}


@app.get("/api/duplicates/history")
def duplicate_history() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute("SELECT dr.asset_id AS keep_id, dr.updated_at, COUNT(*) AS hidden_count FROM duplicate_reviews dr WHERE dr.status = 'retained' GROUP BY dr.asset_id, dr.updated_at ORDER BY dr.updated_at DESC").fetchall()
    return {"items": [{"keepAssetId": row["keep_id"], "hiddenCount": row["hidden_count"], "updatedAt": row["updated_at"]} for row in rows]}


@app.post("/api/duplicates/undo")
def undo_retain_duplicate(payload: DuplicateUndoRequest) -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute("SELECT candidate_asset_id FROM duplicate_reviews WHERE asset_id = ? AND status = 'retained'", (payload.keep_asset_id,)).fetchall()
        ids = [row["candidate_asset_id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"UPDATE assets SET hidden_at = NULL, hidden_reason = '' WHERE id IN ({placeholders})", ids)
            conn.execute("UPDATE duplicate_reviews SET status = 'undone', updated_at = ? WHERE asset_id = ? AND status = 'retained'", (now_text(), payload.keep_asset_id))
    return {"status": "ok", "restored": len(ids), "message": f"已恢复 {len(ids)} 条重复审查隐藏记录。"}


@app.put("/api/ocr/ignore-phrases")
def replace_ocr_ignore_phrases(payload: OcrIgnorePhrasesRequest) -> dict[str, list[str]]:
    return {"items": save_ocr_ignore_phrases(payload.phrases)}


@app.post("/api/ocr/ignore-phrases")
def add_ocr_ignore_phrase(payload: OcrIgnorePhraseRequest) -> dict[str, list[str]]:
    phrase = clean_ocr_ignore_phrase(payload.phrase)
    if not phrase:
        raise HTTPException(status_code=400, detail="无效词不能为空")
    phrases = load_ocr_ignore_phrases()
    phrases.append(phrase)
    return {"items": save_ocr_ignore_phrases(phrases)}


@app.delete("/api/ocr/ignore-phrases")
def delete_ocr_ignore_phrase(phrase: str = Query(...)) -> dict[str, list[str]]:
    target = clean_ocr_ignore_phrase(phrase)
    phrases = [item for item in load_ocr_ignore_phrases() if item != target]
    return {"items": save_ocr_ignore_phrases(phrases)}


@app.post("/api/assets/{asset_id}/click", response_model=AssetResponse)
def record_click(asset_id: int) -> AssetResponse:
    with get_db() as conn:
        conn.execute("UPDATE assets SET clicks = clicks + 1 WHERE id = ?", (asset_id,))
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="图片不存在")
    return asset_from_row(row)


def sync_derivative_detected_name(asset_id: int, detected_name: str) -> None:
    """Keep an already exported derivative aligned with the canonical OCR title."""
    moved_paths: tuple[Path, Path] | None = None
    try:
        with get_db() as conn:
            derivative = conn.execute(
                "SELECT * FROM asset_derivatives WHERE asset_id = ? AND status = 'done'", (asset_id,)
            ).fetchone()
            if not derivative or derivative["title"] == detected_name:
                return
            original_path = Path(derivative["output_path"])
            renamed_path = original_path.with_name(
                f"{derivative['sequence_number']}-{safe_filename(detected_name, '未命名')}{original_path.suffix}"
            )
            if original_path.exists() and original_path != renamed_path:
                if renamed_path.exists():
                    raise HTTPException(status_code=409, detail=f"DataBase 中已存在同名文件，未修改：{renamed_path.name}")
                try:
                    os.replace(original_path, renamed_path)
                except OSError as exc:
                    raise HTTPException(status_code=502, detail=f"无法同步修改 DataBase 文件名：{exc}") from exc
                moved_paths = (renamed_path, original_path)
                conn.execute(
                    "UPDATE asset_derivatives SET output_path = ?, title = ? WHERE id = ?",
                    (str(renamed_path), detected_name, derivative["id"]),
                )
            else:
                conn.execute("UPDATE asset_derivatives SET title = ? WHERE id = ?", (detected_name, derivative["id"]))
    except Exception as exc:
        if moved_paths:
            renamed_path, original_path = moved_paths
            try:
                if original_path.exists():
                    raise OSError(f"回滚目标已存在：{original_path}")
                if renamed_path.exists():
                    os.replace(renamed_path, original_path)
            except OSError as rollback_exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"派生文件数据库更新失败，且文件名回滚失败：{rollback_exc}",
                ) from exc
        raise


@app.patch("/api/assets/{asset_id}", response_model=AssetResponse)
def update_asset_metadata(asset_id: int, payload: AssetUpdateRequest) -> AssetResponse:
    library_name = payload.libraryName.strip()
    detected_name = payload.detectedName.strip()
    tags = [tag.strip() for tag in payload.tags if tag.strip()]
    if not library_name:
        raise HTTPException(status_code=400, detail="库中命名不能为空")
    if not detected_name:
        detected_name = library_name
    with get_db() as conn:
        previous = conn.execute("SELECT library_name, detected_name, tags FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not previous:
            raise HTTPException(status_code=404, detail="图片不存在")
        conn.execute(
            "UPDATE assets SET library_name = ?, detected_name = ?, tags = ? WHERE id = ?",
            (library_name, detected_name, json.dumps(tags or ["未标注"], ensure_ascii=False), asset_id),
        )
    try:
        sync_derivative_detected_name(asset_id, detected_name)
    except Exception:
        with get_db() as conn:
            conn.execute(
                "UPDATE assets SET library_name = ?, detected_name = ?, tags = ? WHERE id = ?",
                (previous["library_name"], previous["detected_name"], previous["tags"], asset_id),
            )
        raise
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return asset_from_row(row)


def recycle_root(kind: str) -> Path:
    return Path(load_config().nas.base_path) / ".素材库回收站" / kind


def move_to_recycle(source_path: Path, kind: str) -> Path:
    root = recycle_root(kind)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{source_path.name}"
    counter = 1
    while destination.exists():
        destination = root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{counter}-{source_path.name}"
        counter += 1
    shutil.move(str(source_path), str(destination))
    return destination


def find_derivative_orphans() -> tuple[Path, list[dict[str, object]]]:
    output_root = derivative_output_root()
    if not output_root.exists():
        return output_root, []
    if not output_root.is_dir():
        raise HTTPException(status_code=400, detail="1280 输出目录不是文件夹，无法检测孤儿文件")
    with get_db() as conn:
        rows = conn.execute("SELECT output_path FROM asset_derivatives WHERE TRIM(COALESCE(output_path, '')) <> ''").fetchall()
    registered_paths = {normalized_path(row["output_path"]) for row in rows}
    recycle_directory = output_root / ".素材库回收站"
    items: list[dict[str, object]] = []
    try:
        for file_path in output_root.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if is_path_within(file_path, recycle_directory):
                continue
            if normalized_path(file_path) in registered_paths:
                continue
            stat = file_path.stat()
            items.append({
                "path": str(file_path),
                "name": file_path.name,
                "sizeBytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"扫描 1280 输出目录失败：{exc}") from exc
    return output_root, sorted(items, key=lambda item: str(item["path"]).casefold())


@app.get("/api/database-review/derivative-orphans")
def list_derivative_orphans() -> dict[str, object]:
    output_root, items = find_derivative_orphans()
    return {"outputRoot": str(output_root), "items": items}


@app.post("/api/database-review/derivative-orphans/delete")
def delete_derivative_orphans(payload: OrphanDerivativeDeleteRequest) -> dict[str, object]:
    requested_paths = {normalized_path(path) for path in payload.paths if str(path).strip()}
    if not requested_paths:
        raise HTTPException(status_code=400, detail="未选择要清理的 1280 孤儿文件")
    _output_root, items = find_derivative_orphans()
    current_orphans = {normalized_path(str(item["path"])): item for item in items}
    moved: list[str] = []
    skipped: list[dict[str, str]] = []
    for requested_path in requested_paths:
        item = current_orphans.get(requested_path)
        if not item:
            skipped.append({"path": requested_path, "reason": "文件已被登记、已不存在，或不在 1280 孤儿清单中。"})
            continue
        try:
            recycled_path = move_to_recycle(Path(str(item["path"])), "1280-孤儿")
            moved.append(str(recycled_path))
        except OSError as exc:
            skipped.append({"path": str(item["path"]), "reason": f"移入回收站失败：{exc}"})
    return {
        "status": "ok",
        "moved": len(moved),
        "skipped": skipped,
        "message": f"已将 {len(moved)} 个 1280 孤儿文件移入 NAS 回收站。",
    }


@app.post("/api/assets/{asset_id}/delete-derivative")
def delete_derivative_file(asset_id: int, payload: DeleteRequest) -> dict[str, object]:
    with get_db() as conn:
        if payload.kind:
            derivative = conn.execute("SELECT * FROM asset_derivatives WHERE asset_id = ? AND kind = ? AND status = 'done'", (asset_id, payload.kind)).fetchone()
        else:
            derivative = conn.execute("SELECT * FROM asset_derivatives WHERE asset_id = ? AND status = 'done'", (asset_id,)).fetchone()
        if not derivative:
            raise HTTPException(status_code=404, detail="没有可删除的派生图")
        original_path = Path(derivative["output_path"])
        if not original_path.exists():
            raise HTTPException(status_code=404, detail="派生图文件不存在或无权访问")
        try:
            recycled_path = move_to_recycle(original_path, f"派生图-{derivative['kind']}")
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"无法移动 {derivative['kind']} 派生图到回收站：{exc}") from exc
        conn.execute("INSERT INTO recycled_derivatives(asset_id, snapshot, original_path, recycle_path, deleted_at, deleted_by) VALUES (?, ?, ?, ?, ?, ?)", (asset_id, json.dumps(dict(derivative), ensure_ascii=False), str(original_path), str(recycled_path), now_text(), payload.username.strip()))
        conn.execute("DELETE FROM asset_derivatives WHERE id = ?", (derivative["id"],))
    return {"status": "ok", "message": f"已移除 {derivative['kind']} 派生图，并放入回收站。"}


@app.get("/api/recycle/derivatives")
def list_recycled_derivatives() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM recycled_derivatives ORDER BY deleted_at DESC LIMIT 500").fetchall()
    items = []
    for row in rows:
        snapshot = json.loads(row["snapshot"])
        items.append({"id": row["id"], "assetId": row["asset_id"], "title": snapshot.get("title", "未知"), "originalPath": row["original_path"], "deletedAt": row["deleted_at"], "deletedBy": row["deleted_by"]})
    return {"items": items}


@app.post("/api/recycle/derivatives/{recycle_id}/restore")
def restore_recycled_derivative(recycle_id: int) -> dict[str, object]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM recycled_derivatives WHERE id = ?", (recycle_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="回收站记录不存在")
        if not conn.execute("SELECT 1 FROM assets WHERE id = ?", (row["asset_id"],)).fetchone():
            raise HTTPException(status_code=409, detail="原数据库记录已不存在，无法恢复该 1280 文件")
        original_path = Path(row["original_path"])
        recycle_path = Path(row["recycle_path"])
        if original_path.exists():
            raise HTTPException(status_code=409, detail="原输出路径已有文件，未覆盖恢复")
        if not recycle_path.exists():
            raise HTTPException(status_code=404, detail="回收站文件不存在")
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(recycle_path), str(original_path))
        snapshot = json.loads(row["snapshot"])
        conn.execute(
            """INSERT INTO asset_derivatives(asset_id, kind, output_path, sequence_number, title, author, channel, source_resolution, source_size_bytes, was_stretched, output_size_bytes, output_resolution, output_format, generated_at, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["asset_id"], snapshot.get("kind", "16:9"), str(original_path), snapshot["sequence_number"], snapshot["title"], snapshot.get("author", "未识别作者"), snapshot.get("channel", ""), snapshot.get("source_resolution", "未知"), snapshot.get("source_size_bytes", 0), snapshot.get("was_stretched", 0), snapshot.get("output_size_bytes", 0), snapshot.get("output_resolution", "1280 x 720"), snapshot.get("output_format", ""), snapshot.get("generated_at", now_text()), snapshot.get("status", "done"), snapshot.get("error", "")),
        )
        conn.execute("DELETE FROM recycled_derivatives WHERE id = ?", (recycle_id,))
    return {"status": "ok", "message": "已恢复派生图文件。"}


@app.post("/api/assets/{asset_id}/hide")
def hide_asset_from_website(asset_id: int, payload: DeleteRequest) -> dict[str, object]:
    with get_db() as conn:
        row = conn.execute("SELECT id, hidden_at FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="图片不存在")
        if row["hidden_at"]:
            return {"status": "ok", "message": "该图片已处于隐藏状态，NAS 文件未改动。"}
        conn.execute("UPDATE assets SET hidden_at = ?, hidden_reason = '网站手动隐藏' WHERE id = ?", (now_text(), asset_id))
    return {"status": "ok", "message": "已从网站总览隐藏该记录；NAS 原图、派生图和数据库记录均未删除，可在隐藏记录中恢复。"}


@app.get("/api/assets/hidden")
def list_hidden_assets() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute("SELECT id, detected_name, library_name, source_path, hidden_at, hidden_reason FROM assets WHERE hidden_at IS NOT NULL ORDER BY hidden_at DESC LIMIT 500").fetchall()
    return {"items": [{"id": row["id"], "name": row["detected_name"] or row["library_name"] or f"图片 {row['id']}", "path": row["source_path"], "hiddenAt": row["hidden_at"], "reason": row["hidden_reason"]} for row in rows]}


@app.post("/api/assets/{asset_id}/restore-hidden")
def restore_hidden_asset(asset_id: int) -> dict[str, object]:
    with get_db() as conn:
        changed = conn.execute("UPDATE assets SET hidden_at = NULL, hidden_reason = '' WHERE id = ? AND hidden_at IS NOT NULL", (asset_id,)).rowcount
    if not changed:
        raise HTTPException(status_code=404, detail="未找到可恢复的隐藏记录")
    return {"status": "ok", "message": "已恢复网站隐藏记录。"}


@app.post("/api/assets/{asset_id}/remove-database")
def remove_asset_database_from_detail(asset_id: int, payload: DeleteRequest) -> dict[str, object]:
    return remove_assets_from_database(DatabaseDeleteRequest(asset_ids=[asset_id], username=payload.username))


@app.post("/api/assets/{asset_id}/delete-source")
def delete_source_to_recycle(asset_id: int, payload: DeleteRequest) -> dict[str, object]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="图片不存在")
        if is_http_path(row["source_path"]):
            raise HTTPException(status_code=400, detail="HTTP 图片来源不支持网页内删除，请在原站点处理。")
        source_path = Path(row["source_path"])
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="NAS 原图不存在或无权访问")
        derivatives = conn.execute("SELECT * FROM asset_derivatives WHERE asset_id = ?", (asset_id,)).fetchall()
        derivative_recycles: list[dict[str, str]] = []
        moved_derivatives: list[tuple[Path, Path]] = []
        try:
            for derivative in derivatives:
                if not str(derivative["output_path"] or "").strip():
                    continue
                output_path = Path(derivative["output_path"])
                if not output_path.is_file():
                    continue
                recycled_derivative_path = move_to_recycle(output_path, f"派生图-{derivative['kind']}")
                moved_derivatives.append((output_path, recycled_derivative_path))
                derivative_recycles.append({"original_path": str(output_path), "recycle_path": str(recycled_derivative_path)})
            recycled_path = move_to_recycle(source_path, "原图")
        except Exception as exc:
            for output_path, recycled_derivative_path in reversed(moved_derivatives):
                try:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(recycled_derivative_path), str(output_path))
                except OSError:
                    pass
            raise HTTPException(
                status_code=502,
                detail=(
                    f"无法完整移动原图和派生文件到回收站：{type(exc).__name__}: {exc}。"
                    "请确认运行服务的 Windows 账号对来源目录及 .素材库回收站拥有创建文件夹、修改和删除权限。"
                ),
            ) from exc
        audit = conn.execute("SELECT * FROM asset_source_audits WHERE asset_id = ?", (asset_id,)).fetchone()
        snapshot = {
            "asset": dict(row),
            "derivatives": [dict(item) for item in derivatives],
            "audit": dict(audit) if audit else None,
            "source_recycle_path": str(recycled_path),
            "derivative_recycles": derivative_recycles,
        }
        conn.execute("INSERT INTO deleted_assets(original_asset_id, snapshot, deleted_at, deleted_by, reason) VALUES (?, ?, ?, ?, ?)", (asset_id, json.dumps(snapshot, ensure_ascii=False), now_text(), payload.username.strip(), "NAS 原图移入回收站"))
        conn.execute("DELETE FROM asset_source_audits WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM asset_derivatives WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    return {"status": "ok", "message": f"NAS 原图及 {len(derivative_recycles)} 个已登记派生图已移入回收站，数据库记录已移入回收站。"}

@app.get("/api/assets/{asset_id}/file")
def asset_file(asset_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="图片不存在")
    source_path = row["source_path"]
    if is_http_path(source_path):
        config = load_config().nas
        request = build_request(source_path, config)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        media_type = response.headers.get("Content-Type") or mimetypes.guess_type(source_path)[0] or "application/octet-stream"
        return StreamingResponse(response, media_type=media_type)
    path = Path(source_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="原图文件不存在或无权访问")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


@app.get("/api/assets/{asset_id}/derivative-file")
def derivative_asset_file(asset_id: int, kind: Literal["16:9", "1:1", "9:16"] = "16:9"):
    with get_db() as conn:
        row = conn.execute("SELECT output_path FROM asset_derivatives WHERE asset_id = ? AND kind = ? AND status = 'done'", (asset_id, kind)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"尚未生成 {kind} 派生版本")
    path = Path(row["output_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="派生图文件不存在或无权访问")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


@app.get("/api/assets/{asset_id}/download")
def download_asset_file(asset_id: int, version: DisplayVersion = "raw"):
    display_version = normalize_display_version(version)
    with get_db() as conn:
        asset = conn.execute("SELECT source_path FROM assets WHERE id = ? AND hidden_at IS NULL", (asset_id,)).fetchone()
        derivative = None if display_version == "raw" else conn.execute("SELECT output_path FROM asset_derivatives WHERE asset_id = ? AND kind = ? AND status = 'done'", (asset_id, display_version)).fetchone()
    if not asset:
        raise HTTPException(status_code=404, detail="图片不存在或已隐藏")
    selected_path = asset["source_path"] if display_version == "raw" else (derivative["output_path"] if derivative else "")
    if not selected_path:
        raise HTTPException(status_code=404, detail=f"尚未生成 {display_version} 派生版本")
    parsed_name = Path(urllib.parse.unquote(urllib.parse.urlparse(selected_path).path)).name
    filename = safe_filename(parsed_name, f"asset-{asset_id}.jpg")
    if is_http_path(selected_path):
        try:
            response = urllib.request.urlopen(build_request(selected_path, load_config().nas), timeout=20)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取图片失败：{exc}") from exc
        media_type = response.headers.get("Content-Type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        disposition = f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"
        return StreamingResponse(
            stream_remote_download(response, asset_id),
            media_type=media_type,
            headers={"Content-Disposition": disposition},
        )
    path = Path(selected_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="下载文件不存在或无权访问")
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(path.name)[0],
        filename=filename,
        background=BackgroundTask(record_asset_download, asset_id),
    )


def record_asset_download(asset_id: int) -> None:
    """Record a completed response without making file delivery depend on statistics."""
    try:
        with get_db() as conn:
            conn.execute("UPDATE assets SET downloads = downloads + 1 WHERE id = ?", (asset_id,))
    except Exception as exc:
        print(f"Download count update failed for asset {asset_id}: {exc}")


def stream_remote_download(response, asset_id: int):
    """Yield bounded chunks, close the upstream response, and count only full transfers."""
    completed = False
    try:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                completed = True
                break
            yield chunk
    finally:
        response.close()
        if completed:
            record_asset_download(asset_id)


@app.get("/api/assets/{asset_id}/variant", response_model=AssetResponse)
def get_asset_variant(asset_id: int, version: DisplayVersion = "raw") -> AssetResponse:
    display_version = normalize_display_version(version)
    join = f"JOIN asset_derivatives d ON d.asset_id = assets.id AND d.kind = '{display_version}' AND d.status = 'done'" if display_version != "raw" else "LEFT JOIN asset_derivatives d ON d.asset_id = assets.id AND d.kind = '16:9' AND d.status = 'done'"
    with get_db() as conn:
        row = conn.execute(
            f"""SELECT assets.*, CASE WHEN d.id IS NULL THEN 0 ELSE 1 END AS derivative_available,
            COALESCE(d.output_path, '') AS derivative_path, COALESCE(d.status, '') AS derivative_status,
            COALESCE(d.author, '') AS derivative_author, COALESCE(d.channel, '') AS derivative_channel, d.sequence_number AS derivative_sequence,
            COALESCE(d.generated_at, '') AS derivative_generated_at, COALESCE(d.output_size_bytes, 0) AS derivative_size_bytes,
            (SELECT GROUP_CONCAT(td.name, CHAR(31)) FROM asset_tags at JOIN tag_definitions td ON td.id = at.tag_id JOIN tag_categories tc ON tc.id = td.category_id WHERE at.asset_id = assets.id AND td.active = 1 AND tc.active = 1) AS relation_tags,
            '{display_version}' AS display_version
            FROM assets {join} WHERE assets.id = ?""",
            (asset_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="图片或所选版本不存在")
    return asset_from_row(row)



def safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', '_', name).strip().strip('.')
    return cleaned or fallback


def read_asset_bytes(row: sqlite3.Row) -> tuple[str, bytes]:
    source_path = row["source_path"]
    original_name = Path(urllib.parse.urlparse(source_path).path).name or f"asset-{row['id']}"
    filename = safe_filename(original_name, f"asset-{row['id']}.jpg")
    if is_http_path(source_path):
        config = load_config().nas
        request = build_request(source_path, config)
        with urllib.request.urlopen(request, timeout=20) as response:
            return filename, response.read()
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(source_path)
    return filename, path.read_bytes()


DEFAULT_OCR_IGNORE_PHRASES = [
    "全部免费",
    "全集免费",
    "进来看看",
    "点进来看看",
    "点这里",
    "精彩不容错过",
    "本剧情纯属虚构",
    "本故事纯属虚构",
    "赶紧看看",
    "全程免费",
    "继续",
    "点击查看",
    "已完结",
    "本故事情节纯属虚构",
    "剧情演绎这部短剧真好看",
    "打开看看",
    "免费短剧",
    "热门短剧",
    "热门好剧",
    "热门好看的短剧合集",
    "纯属虚构",
    "本故事纯属虚构",
    "请勿模仿",
    "请勿相信",
    "切勿模仿",
    "如有雷同",
    "素材来源",
    "广告",
]


def clean_ocr_ignore_phrase(phrase: str) -> str:
    return re.sub(r"\s+", "", phrase or "").strip()


def normalize_ocr_ignore_phrases(phrases: list[str]) -> list[str]:
    cleaned = [clean_ocr_ignore_phrase(phrase) for phrase in phrases]
    return sorted(dict.fromkeys(phrase for phrase in cleaned if phrase), key=lambda item: (len(item), item))


def load_ocr_ignore_phrases() -> list[str]:
    if not OCR_IGNORE_PATH.exists():
        return normalize_ocr_ignore_phrases(DEFAULT_OCR_IGNORE_PHRASES)
    try:
        data = json.loads(OCR_IGNORE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return normalize_ocr_ignore_phrases(DEFAULT_OCR_IGNORE_PHRASES)
    if not isinstance(data, list):
        return normalize_ocr_ignore_phrases(DEFAULT_OCR_IGNORE_PHRASES)
    return normalize_ocr_ignore_phrases([str(item) for item in data])


def save_ocr_ignore_phrases(phrases: list[str]) -> list[str]:
    cleaned = normalize_ocr_ignore_phrases(phrases)
    OCR_IGNORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OCR_IGNORE_PATH.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned


def normalize_ocr_text(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text or "")
    cleaned = re.sub(r"[A-Za-z]:?\\?Users\\?.*", "", cleaned)
    cleaned = re.sub(r"CUsers.*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r".*AppDataLocalTemp.*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"tmp[a-zA-Z0-9_-]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(jpg|jpeg|png|webp|general)$", "", cleaned, flags=re.IGNORECASE)
    for phrase in load_ocr_ignore_phrases():
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"[\u3000\s`~!@#$%^&*()_+\-=\[\]{};:'\",.<>/?\\|，。！？、；：“”‘’（）【】《》·…—]+", "", cleaned)
    return cleaned.strip()


def should_drop_ocr_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return True
    if re.search(r"(AppData|LocalTemp|CUsers|Users\\|tmp[a-zA-Z0-9_-]*|\\.(jpg|jpeg|png|webp))", compact, re.IGNORECASE):
        return True
    return any(phrase in compact for phrase in load_ocr_ignore_phrases())


BOOK_TITLE_PATTERNS = (
    re.compile(r"《\s*[^《》\r\n]{1,120}?\s*》"),
    re.compile(r"〈\s*[^〈〉\r\n]{1,120}?\s*〉"),
)


def detected_book_title_mark(text: str) -> str:
    for pattern in BOOK_TITLE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip()[:140]
    return ""


def bbox_from_value(value) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x1, y1, x2, y2 = [float(item) for item in value]
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
    points: list[tuple[float, float]] = []
    for point in value:
        if isinstance(point, (list, tuple)) and len(point) >= 2 and isinstance(point[0], (int, float)) and isinstance(point[1], (int, float)):
            points.append((float(point[0]), float(point[1])))
    if points:
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return min(xs), min(ys), max(xs), max(ys)
    return None


def ocr_candidate_score(text: str, bbox: tuple[float, float, float, float] | None) -> float:
    cleaned = normalize_ocr_text(text)
    if not cleaned or should_drop_ocr_text(text):
        return -1
    if not bbox:
        return min(len(cleaned), 60)
    x1, y1, x2, y2 = bbox
    width = max(1.0, x2 - x1)
    height = max(1.0, y2 - y1)
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    center_penalty = 1.0 / (1.0 + abs(cx - 0.5) * 1.2 + abs(cy - 0.55) * 1.0)
    size_score = (height ** 2.0) * (width ** 0.35)
    return size_score * center_penalty * (1.0 + min(len(cleaned), 24) / 80)


def extract_ocr_candidates(value) -> list[tuple[str, tuple[float, float, float, float] | None, float]]:
    candidates: list[tuple[str, tuple[float, float, float, float] | None, float]] = []
    if isinstance(value, dict):
        texts = value.get("rec_texts") or value.get("texts")
        boxes = value.get("rec_boxes") or value.get("rec_polys") or value.get("dt_polys") or value.get("boxes")
        scores = value.get("rec_scores") or value.get("scores") or []
        if isinstance(texts, list):
            for index, item in enumerate(texts):
                raw_text = str(item).strip()
                box = bbox_from_value(boxes[index]) if isinstance(boxes, list) and index < len(boxes) else None
                score = float(scores[index]) if isinstance(scores, list) and index < len(scores) and isinstance(scores[index], (int, float)) else 1.0
                candidates.append((raw_text, box, score))
        for item in value.values():
            candidates.extend(extract_ocr_candidates(item))
        return candidates
    if isinstance(value, list):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                box = bbox_from_value(item[0])
                text_part = item[1]
                if box and isinstance(text_part, (list, tuple)) and text_part:
                    raw_text = str(text_part[0]).strip()
                    score = float(text_part[1]) if len(text_part) > 1 and isinstance(text_part[1], (int, float)) else 1.0
                    candidates.append((raw_text, box, score))
                    continue
            candidates.extend(extract_ocr_candidates(item))
        return candidates
    for attr in ["json", "to_dict"]:
        method = getattr(value, attr, None)
        if callable(method):
            try:
                candidates.extend(extract_ocr_candidates(method()))
                return candidates
            except Exception:
                pass
    data = getattr(value, "res", None)
    if data is not None:
        candidates.extend(extract_ocr_candidates(data))
    return candidates


def select_primary_ocr_text(result) -> str:
    raw_candidates = extract_ocr_candidates(result)
    prepared: list[dict[str, object]] = []
    max_x = 1.0
    max_y = 1.0
    for raw_text, bbox, confidence in raw_candidates:
        cleaned = normalize_ocr_text(raw_text)
        if not cleaned or should_drop_ocr_text(raw_text):
            continue
        if bbox:
            max_x = max(max_x, bbox[2])
            max_y = max(max_y, bbox[3])
        prepared.append({"raw": raw_text, "text": cleaned, "bbox": bbox, "confidence": float(confidence or 1.0)})

    boxed: list[dict[str, object]] = []
    for item in prepared:
        bbox = item["bbox"]
        if not bbox:
            continue
        x1, y1, x2, y2 = bbox  # type: ignore[misc]
        width = max(1.0, x2 - x1)
        height = max(1.0, y2 - y1)
        cx = ((x1 + x2) / 2) / max_x
        cy = ((y1 + y2) / 2) / max_y
        center_score = 1.0 / (1.0 + abs(cx - 0.5) * 1.4 + abs(cy - 0.55) * 1.1)
        size_score = (height ** 2.0) * (width ** 0.35)
        confidence = max(0.2, min(float(item["confidence"]), 1.0))
        score = size_score * center_score * confidence
        item.update({"width": width, "height": height, "cx": cx, "cy": cy, "score": score})
        boxed.append(item)

    if boxed:
        boxed.sort(key=lambda item: float(item["score"]), reverse=True)
        primary = boxed[0]
        primary_height = float(primary["height"])
        primary_cx = float(primary["cx"])
        primary_cy = float(primary["cy"])
        primary_score = float(primary["score"])
        grouped: list[dict[str, object]] = []
        for item in boxed:
            height = float(item["height"])
            cx = float(item["cx"])
            cy = float(item["cy"])
            score = float(item["score"])
            similar_large_text = height >= primary_height * 0.55 or score >= primary_score * 0.35
            same_title_zone = abs(cy - primary_cy) <= 0.34 and (abs(cx - primary_cx) <= 0.55 or float(item["width"]) / max_x >= 0.25)
            central_enough = 0.04 <= cx <= 0.96 and 0.08 <= cy <= 0.98
            if similar_large_text and same_title_zone and central_enough:
                grouped.append(item)
        grouped.sort(key=lambda item: (float(item["bbox"][1]), float(item["bbox"][0])))  # type: ignore[index]
        joined = "".join(str(item["text"]) for item in grouped)
        return normalize_ocr_text(joined)

    fallback = [normalize_ocr_text(item) for item in collect_ocr_text(result) if not should_drop_ocr_text(item)]
    fallback = [item for item in fallback if item]
    if not fallback:
        return ""
    return max(fallback, key=lambda item: (len(item), item))
def get_paddle_ocr():
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except Exception as exc:
        raise RuntimeError("PaddleOCR 未安装。请先按 docs/ocr-local.md 安装本地 OCR 依赖。") from exc
    if not hasattr(get_paddle_ocr, "instance"):
        init_attempts = [
            lambda: PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                engine="paddle",
                device="cpu",
                enable_mkldnn=True,
                cpu_threads=OCR_CPU_THREADS,
                **paddle_ocr_model_options(),
            ),
            lambda: PaddleOCR(
                lang="ch",
                use_angle_cls=True,
                show_log=False,
                enable_mkldnn=True,
                cpu_threads=OCR_CPU_THREADS,
            ),
            lambda: PaddleOCR(lang="ch", enable_mkldnn=True, cpu_threads=OCR_CPU_THREADS),
        ]
        last_error: Exception | None = None
        for create_engine in init_attempts:
            try:
                get_paddle_ocr.instance = create_engine()
                break
            except Exception as exc:
                last_error = exc
        if not hasattr(get_paddle_ocr, "instance"):
            raise RuntimeError(f"PaddleOCR 初始化失败：{last_error}")
    return get_paddle_ocr.instance

def collect_ocr_text(value) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            texts.append(stripped)
        return texts
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        stripped = value[0].strip()
        if stripped:
            texts.append(stripped)
        return texts
    if isinstance(value, dict):
        found_text_field = False
        for key in ["rec_texts", "rec_text", "text", "texts"]:
            item = value.get(key)
            if isinstance(item, list):
                texts.extend(str(part).strip() for part in item if str(part).strip())
                found_text_field = True
            elif isinstance(item, str) and item.strip():
                texts.append(item.strip())
                found_text_field = True
        if found_text_field:
            return texts
        # PaddleOCR result dictionaries also include input paths, model names,
        # configuration values, and other metadata.  Only descend into nested
        # containers; never treat arbitrary string values as recognized text.
        for item in value.values():
            if isinstance(item, (dict, list, tuple)):
                texts.extend(collect_ocr_text(item))
        return texts
    if isinstance(value, list):
        for item in value:
            texts.extend(collect_ocr_text(item))
        return texts
    for attr in ["json", "to_dict"]:
        method = getattr(value, attr, None)
        if callable(method):
            try:
                texts.extend(collect_ocr_text(method()))
                return texts
            except Exception:
                pass
    data = getattr(value, "res", None)
    if data is not None:
        texts.extend(collect_ocr_text(data))
    return texts

def run_paddle_ocr_on_asset(row: sqlite3.Row) -> str:
    filename, data = read_asset_bytes(row)
    suffix = Path(filename).suffix or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_file.write(data)
        temp_path = temp_file.name
    try:
        engine = get_paddle_ocr()
        try:
            result = engine.predict(input=temp_path)
        except (AttributeError, TypeError):
            try:
                result = engine.ocr(temp_path, cls=True)
            except TypeError:
                result = engine.ocr(temp_path)
        pieces = list(dict.fromkeys(collect_ocr_text(result)))
        return "\n".join(pieces).strip()
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def apply_ocr_result(asset_id: int, text: str) -> tuple[AssetResponse, str, str]:
    title = normalize_ocr_text(text)
    status = "done" if text else "empty"
    book_title_mark = detected_book_title_mark(text)
    hidden_reason = f"OCR 自动隐藏：识别到书名号内容 {book_title_mark}" if book_title_mark else ""
    with get_db() as conn:
        if title:
            conn.execute(
                "UPDATE assets SET ocr_text = ?, ocr_status = ?, ocr_error = '', detected_name = ?, library_name = CASE WHEN library_name = '' OR library_name LIKE 'mock-%' THEN ? ELSE library_name END WHERE id = ?",
                (text, status, title[:120], title[:120], asset_id),
            )
        else:
            conn.execute("UPDATE assets SET ocr_text = ?, ocr_status = ?, ocr_error = '' WHERE id = ?", (text, status, asset_id))
        if hidden_reason:
            conn.execute(
                "UPDATE assets SET hidden_at = ?, hidden_reason = ? WHERE id = ?",
                (now_text(), hidden_reason, asset_id),
            )
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="图片不存在")
    sync_warning = ""
    if title:
        try:
            sync_derivative_detected_name(asset_id, title[:120])
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            sync_warning = f"派生文件命名未同步：{detail}"
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return asset_from_row(row), hidden_reason, sync_warning


@app.get("/api/ocr/status")
def ocr_status() -> dict[str, object]:
    try:
        get_paddle_ocr()
        return {"available": True, "engine": "PaddleOCR", "message": "本地 OCR 可用。"}
    except RuntimeError as exc:
        return {"available": False, "engine": "PaddleOCR", "message": str(exc)}


@app.post("/api/assets/{asset_id}/ocr", response_model=OcrResponse)
def recognize_asset_text(asset_id: int) -> OcrResponse:
    # Keep legacy direct callers serialized with the persistent OCR worker.
    with OCR_EXECUTION_LOCK:
        return _recognize_asset_text_unlocked(asset_id)


def _recognize_asset_text_unlocked(asset_id: int) -> OcrResponse:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ? AND hidden_at IS NULL", (asset_id,)).fetchone()
        if row:
            conn.execute("UPDATE assets SET ocr_status = ?, ocr_error = '' WHERE id = ?", ("running", asset_id))
    if not row:
        raise HTTPException(status_code=404, detail="图片不存在")
    asset_name = row["detected_name"] or row["library_name"] or f"图片 {asset_id}"
    try:
        text = run_paddle_ocr_on_asset(row)
        asset, hidden_reason, sync_warning = apply_ocr_result(asset_id, text)
        message = "OCR 识别完成。"
        if hidden_reason:
            message = f"OCR 识别完成；{hidden_reason}，图片已从默认列表隐藏。"
        if sync_warning:
            message = f"{message} {sync_warning}"
        return OcrResponse(
            status="ok",
            message=message,
            text=text,
            asset=asset,
            assetId=asset_id,
            name=asset_name,
            autoHidden=bool(hidden_reason),
            hiddenReason=hidden_reason,
            syncWarning=sync_warning,
        )
    except RuntimeError as exc:
        reason = str(exc)
        with get_db() as conn:
            conn.execute("UPDATE assets SET ocr_status = ?, ocr_error = ? WHERE id = ?", ("missing_dependency", reason, asset_id))
        return OcrResponse(status="missing_dependency", message=reason, text="", assetId=asset_id, name=asset_name)
    except Exception as exc:
        if isinstance(exc, FileNotFoundError):
            reason = f"原图文件不存在或无权访问：{exc}"
        elif isinstance(exc, PermissionError):
            reason = f"没有读取原图的权限：{exc}"
        elif isinstance(exc, urllib.error.HTTPError):
            reason = f"读取图片失败，HTTP 状态码 {exc.code}：{exc.reason}"
        elif isinstance(exc, urllib.error.URLError):
            reason = f"读取图片失败，网络连接异常：{exc.reason}"
        else:
            reason = f"OCR 运行失败：{exc}"
        with get_db() as conn:
            conn.execute("UPDATE assets SET ocr_status = ?, ocr_error = ? WHERE id = ?", ("failed", reason, asset_id))
        return OcrResponse(status="failed", message=reason, text="", assetId=asset_id, name=asset_name)


@app.post("/api/assets/ocr-batch", response_model=OcrBatchResponse)
def recognize_assets_text_batch(payload: OcrBatchRequest) -> OcrBatchResponse:
    processed = 0
    succeeded = 0
    failed = 0
    auto_hidden = 0
    sync_warnings: list[str] = []
    for asset_id in list(dict.fromkeys(payload.asset_ids)):
        processed += 1
        result = recognize_asset_text(asset_id)
        if result.status == "ok":
            succeeded += 1
            auto_hidden += int(result.autoHidden)
            if result.syncWarning:
                sync_warnings.append(f"{result.name or f'图片 {asset_id}'}：{result.syncWarning}")
        else:
            failed += 1
            if result.status == "missing_dependency":
                return OcrBatchResponse(status="failed", message=result.message, processed=processed, succeeded=succeeded, failed=failed, autoHidden=auto_hidden, syncWarnings=sync_warnings)
    return OcrBatchResponse(status="ok", message=f"批量 OCR 完成；自动隐藏 {auto_hidden} 张；派生命名同步警告 {len(sync_warnings)} 项。", processed=processed, succeeded=succeeded, failed=failed, autoHidden=auto_hidden, syncWarnings=sync_warnings)

if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
