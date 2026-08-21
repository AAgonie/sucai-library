"""SQLite connection, V2 migrations, and read-only migration reporting."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "short-drama-ad-library.sqlite3"
_existing_database_paths = sorted((PROJECT_ROOT / "data").glob("*.sqlite3"))
# Keep using a single pre-existing local database after an application rename,
# without publishing an environment-specific historical file name.
DB_PATH = _existing_database_paths[0] if len(_existing_database_paths) == 1 else DEFAULT_DB_PATH
MIGRATION_V2_01 = "V2-01"
MIGRATION_V2_02 = "V2-02"
MIGRATION_V2_03 = "V2-03"
MIGRATION_V2_03_1 = "V2-03.1"
MIGRATION_V2_05 = "V2-05"
MIGRATION_V2_99_1 = "V2-99.1"
MIGRATION_V2_99_2 = "V2-99.2"
MIGRATION_V2_13 = "V2-13"
SQLITE_BUSY_TIMEOUT_MS = 5_000


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3, then always release the file handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def utc_now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def get_db(db_path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection configured for concurrent local readers/writers."""
    path = (db_path or DB_PATH).resolve()
    if readonly:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, factory=ClosingSQLiteConnection
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, factory=ClosingSQLiteConnection)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if not readonly:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def normalize_path_key(source_path: str) -> str:
    """Return the cross-source path key required by V2.

    It deliberately normalizes path *keys* only; source_path itself remains the
    original value so V1 file access keeps working unchanged.
    """
    value = (source_path or "").strip().replace("\\", "/")
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        path = re.sub(r"/+", "/", parsed.path).rstrip("/")
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")).casefold()
    value = re.sub(r"/+", "/", value).rstrip("/")
    return value.casefold()


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute the simple DDL scripts below without sqlite3.executescript().

    sqlite3.Connection.executescript() commits any pending transaction before
    running. That would make a failed migration partially visible, so these DDL
    scripts are intentionally executed statement by statement.
    """
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _create_v1_compatible_schema(connection: sqlite3.Connection) -> None:
    """Create the former schema on a new install before applying V2 migrations."""
    _execute_schema_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            library_name TEXT NOT NULL,
            detected_name TEXT NOT NULL,
            resolution TEXT NOT NULL DEFAULT '未知',
            ratio TEXT NOT NULL DEFAULT '未知',
            file_size_bytes INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '[]',
            imported_at TEXT NOT NULL,
            downloads INTEGER NOT NULL DEFAULT 0,
            clicks INTEGER NOT NULL DEFAULT 0,
            source_type TEXT NOT NULL DEFAULT 'local',
            ocr_text TEXT NOT NULL DEFAULT '',
            ocr_status TEXT NOT NULL DEFAULT 'pending',
            ocr_error TEXT NOT NULL DEFAULT '',
            source_author TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_assets_imported ON assets(imported_at);
        CREATE INDEX IF NOT EXISTS idx_assets_clicks ON assets(clicks);
        CREATE INDEX IF NOT EXISTS idx_assets_downloads ON assets(downloads);
        CREATE TABLE IF NOT EXISTS asset_source_audits (
            asset_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deleted_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, original_asset_id INTEGER NOT NULL,
            snapshot TEXT NOT NULL, deleted_at TEXT NOT NULL,
            deleted_by TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '数据库审查'
        );
        CREATE TABLE IF NOT EXISTS asset_derivatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT '1280', output_path TEXT NOT NULL UNIQUE,
            sequence_number INTEGER NOT NULL UNIQUE, title TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '未识别作者', channel TEXT NOT NULL DEFAULT '',
            source_resolution TEXT NOT NULL DEFAULT '未知', source_size_bytes INTEGER NOT NULL DEFAULT 0,
            was_stretched INTEGER NOT NULL DEFAULT 0, output_size_bytes INTEGER NOT NULL DEFAULT 0,
            output_resolution TEXT NOT NULL DEFAULT '1280 x 720', output_format TEXT NOT NULL DEFAULT '',
            generated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'done',
            error TEXT NOT NULL DEFAULT '', UNIQUE(asset_id, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_derivatives_asset ON asset_derivatives(asset_id);
        CREATE TABLE IF NOT EXISTS asset_visual_hashes (
            asset_id INTEGER PRIMARY KEY, visual_hash TEXT NOT NULL, checked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recycled_derivatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL,
            snapshot TEXT NOT NULL, original_path TEXT NOT NULL, recycle_path TEXT NOT NULL,
            deleted_at TEXT NOT NULL, deleted_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS derivative_authors (name TEXT PRIMARY KEY, last_used_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS derivative_channels (name TEXT PRIMARY KEY, last_used_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sequence_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL, output_root TEXT NOT NULL,
            missing_numbers TEXT NOT NULL DEFAULT '[]', duplicate_numbers TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT ''
        );
        """
    )
    _ensure_column(connection, "assets", "ocr_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "assets", "ocr_status", "TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(connection, "assets", "ocr_error", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "assets", "source_author", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "asset_derivatives", "channel", "TEXT NOT NULL DEFAULT ''")


def _backup_database(connection: sqlite3.Connection, db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{db_path.stem}-before-{MIGRATION_V2_01}-{timestamp}{db_path.suffix}"
    with sqlite3.connect(backup_path) as target:
        connection.backup(target)
    return backup_path


def _create_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL,
            backup_path TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _migration_applied(connection: sqlite3.Connection, version: str) -> bool:
    return _has_table(connection, "schema_migrations") and connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone() is not None


def _migrate_derivatives(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT asset_id, COUNT(*) AS count FROM asset_derivatives "
        "GROUP BY asset_id HAVING COUNT(*) > 1"
    ).fetchall()
    if existing:
        ids = ", ".join(str(row["asset_id"]) for row in existing)
        raise RuntimeError(f"无法迁移：同一原图存在多个派生图（asset_id: {ids}）。")

    connection.execute("ALTER TABLE asset_derivatives RENAME TO asset_derivatives_v1")
    connection.execute(
        """
        CREATE TABLE asset_derivatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            kind TEXT NOT NULL CHECK (kind IN ('16:9', '1:1', '9:16')),
            output_path TEXT NOT NULL UNIQUE,
            sequence_number INTEGER NOT NULL UNIQUE,
            title TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '未识别作者',
            channel TEXT NOT NULL DEFAULT '',
            source_resolution TEXT NOT NULL DEFAULT '未知',
            source_size_bytes INTEGER NOT NULL DEFAULT 0,
            was_stretched INTEGER NOT NULL DEFAULT 0,
            output_size_bytes INTEGER NOT NULL DEFAULT 0,
            output_resolution TEXT NOT NULL DEFAULT '1280 x 720',
            output_format TEXT NOT NULL DEFAULT '',
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            error TEXT NOT NULL DEFAULT '',
            UNIQUE(asset_id),
            UNIQUE(asset_id, kind)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO asset_derivatives (
            id, asset_id, kind, output_path, sequence_number, title, author, channel,
            source_resolution, source_size_bytes, was_stretched, output_size_bytes,
            output_resolution, output_format, generated_at, status, error
        )
        SELECT id, asset_id,
               CASE kind WHEN '1280' THEN '16:9' ELSE kind END,
               output_path, sequence_number, title, author, channel,
               source_resolution, source_size_bytes, was_stretched, output_size_bytes,
               output_resolution, output_format, generated_at, status, error
        FROM asset_derivatives_v1
        """
    )
    connection.execute("DROP TABLE asset_derivatives_v1")
    connection.execute("CREATE INDEX idx_derivatives_asset ON asset_derivatives(asset_id)")


def _migrate_tags(connection: sqlite3.Connection) -> None:
    now = utc_now_text()
    connection.execute(
        "INSERT INTO tag_categories(name, created_at, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO NOTHING",
        ("历史标签（待整理）", now, now),
    )
    category_id = connection.execute(
        "SELECT id FROM tag_categories WHERE name = ?", ("历史标签（待整理）",)
    ).fetchone()["id"]
    for asset in connection.execute("SELECT id, tags FROM assets"):
        try:
            raw_tags = json.loads(asset["tags"])
        except (TypeError, json.JSONDecodeError):
            raw_tags = []
        if not isinstance(raw_tags, list):
            continue
        for raw_tag in raw_tags:
            tag = str(raw_tag).strip()
            if not tag or tag == "未标注":
                continue
            connection.execute(
                "INSERT INTO tag_definitions(category_id, name, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(category_id, name) DO NOTHING",
                (category_id, tag, now, now),
            )
            tag_id = connection.execute(
                "SELECT id FROM tag_definitions WHERE category_id = ? AND name = ?", (category_id, tag)
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO asset_tags(asset_id, tag_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(asset_id, tag_id) DO NOTHING",
                (asset["id"], tag_id, now),
            )


def _migrate_legacy_derivative_snapshots(connection: sqlite3.Connection) -> None:
    """Keep V1 recycle snapshots restorable after the derivative-kind rename."""
    for table in ("deleted_assets", "recycled_derivatives"):
        if not _has_table(connection, table) or "snapshot" not in _columns(connection, table):
            continue
        for row in connection.execute(f"SELECT id, snapshot FROM {table}"):
            try:
                snapshot = json.loads(row["snapshot"])
            except (TypeError, json.JSONDecodeError):
                continue
            changed = False

            def replace_kind(value: object) -> None:
                nonlocal changed
                if isinstance(value, dict):
                    if value.get("kind") == "1280":
                        value["kind"] = "16:9"
                        changed = True
                    for child in value.values():
                        replace_kind(child)
                elif isinstance(value, list):
                    for child in value:
                        replace_kind(child)

            replace_kind(snapshot)
            if changed:
                connection.execute(
                    f"UPDATE {table} SET snapshot = ? WHERE id = ?",
                    (json.dumps(snapshot, ensure_ascii=False), row["id"]),
                )


def _apply_v2_01(connection: sqlite3.Connection, backup_path: Path | None) -> None:
    _create_v1_compatible_schema(connection)
    _ensure_column(connection, "assets", "data_source_id", "INTEGER")
    _ensure_column(connection, "assets", "path_key", "TEXT")
    _ensure_column(connection, "assets", "file_modified_at", "TEXT")
    _ensure_column(connection, "assets", "file_size_fingerprint", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "assets", "hidden_at", "TEXT")
    _ensure_column(connection, "assets", "hidden_reason", "TEXT NOT NULL DEFAULT ''")
    _execute_schema_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL DEFAULT 'legacy',
            root_path TEXT NOT NULL DEFAULT '',
            root_path_key TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS background_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            payload TEXT NOT NULL DEFAULT '{}',
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES background_jobs(id),
            event_type TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tag_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tag_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES tag_categories(id),
            name TEXT NOT NULL COLLATE NOCASE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(category_id, name)
        );
        CREATE TABLE IF NOT EXISTS asset_tags (
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            tag_id INTEGER NOT NULL REFERENCES tag_definitions(id),
            created_at TEXT NOT NULL,
            PRIMARY KEY (asset_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS duplicate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            candidate_asset_id INTEGER NOT NULL REFERENCES assets(id),
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(asset_id, candidate_asset_id),
            CHECK (asset_id <> candidate_asset_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON background_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag_id, asset_id);
        CREATE INDEX IF NOT EXISTS idx_duplicate_reviews_status ON duplicate_reviews(status, updated_at);
        """
    )
    now = utc_now_text()
    connection.execute(
        "INSERT INTO data_sources(name, source_type, root_path, root_path_key, created_at, updated_at) "
        "VALUES (?, 'legacy', '', 'legacy-default', ?, ?) ON CONFLICT(name) DO NOTHING",
        ("V1 既有数据", now, now),
    )
    legacy_source_id = connection.execute(
        "SELECT id FROM data_sources WHERE name = ?", ("V1 既有数据",)
    ).fetchone()["id"]
    used_keys: set[str] = set()
    for asset in connection.execute("SELECT id, source_path, file_size_bytes, path_key FROM assets ORDER BY id"):
        key = normalize_path_key(asset["source_path"])
        if key in used_keys:
            key = f"{key}#legacy-{asset['id']}"
        used_keys.add(key)
        connection.execute(
            "UPDATE assets SET data_source_id = COALESCE(data_source_id, ?), path_key = ?, "
            "file_size_fingerprint = CASE WHEN file_size_fingerprint = '' THEN CAST(file_size_bytes AS TEXT) ELSE file_size_fingerprint END "
            "WHERE id = ?",
            (legacy_source_id, key, asset["id"]),
        )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_path_key_unique ON assets(path_key)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_data_source ON assets(data_source_id)")
    _migrate_derivatives(connection)
    _migrate_legacy_derivative_snapshots(connection)
    _migrate_tags(connection)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description, backup_path) VALUES (?, ?, ?, ?)",
        (MIGRATION_V2_01, now, "V2 数据库迁移与基础架构", str(backup_path or "")),
    )


def _apply_v2_02(connection: sqlite3.Connection) -> None:
    _ensure_column(connection, "data_sources", "recursive", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "data_sources", "scan_interval_minutes", "INTEGER NOT NULL DEFAULT 10")
    _ensure_column(connection, "data_sources", "auto_ocr", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "data_sources", "default_author", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "data_sources", "last_scanned_at", "TEXT")
    _ensure_column(connection, "data_sources", "last_scan_error", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "background_jobs", "data_source_id", "INTEGER REFERENCES data_sources(id)")
    _ensure_column(connection, "background_jobs", "asset_id", "INTEGER REFERENCES assets(id)")
    _ensure_column(connection, "background_jobs", "current_path", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "background_jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "job_events", "asset_id", "INTEGER REFERENCES assets(id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue ON background_jobs(job_type, status, created_at)")
    connection.execute("UPDATE background_jobs SET status = 'queued', started_at = NULL WHERE status = 'running'")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
        (MIGRATION_V2_02, utc_now_text(), "多数据源扫描、任务队列与 OCR"),
    )


INITIAL_TAGS: dict[str, tuple[str, ...]] = {
    "男性": ("国际", "历史", "军事", "美女", "擦边", "武器", "社会", "官场", "乡村"),
    "女性": ("婚恋", "婚礼", "青春", "霸总", "浪漫", "相亲", "领证", "校园", "孕事", "萌宝"),
    "中性": ("健康", "婚姻", "两性", "时事", "饮食", "晚恋", "奇闻", "养老", "家庭", "金钱", "婆媳", "职场", "夫妻", "玄幻", "子女", "伦理", "古言", "悬疑"),
}


def _apply_v2_03(connection: sqlite3.Connection) -> None:
    _ensure_column(connection, "tag_categories", "active", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "tag_definitions", "active", "INTEGER NOT NULL DEFAULT 1")
    now = utc_now_text()
    for category, tags in INITIAL_TAGS.items():
        connection.execute(
            "INSERT INTO tag_categories(name, active, created_at, updated_at) VALUES (?, 1, ?, ?) ON CONFLICT(name) DO NOTHING",
            (category, now, now),
        )
        category_id = connection.execute("SELECT id FROM tag_categories WHERE name = ?", (category,)).fetchone()["id"]
        for tag in tags:
            connection.execute(
                "INSERT INTO tag_definitions(category_id, name, active, created_at, updated_at) VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(category_id, name) DO NOTHING",
                (category_id, tag, now, now),
            )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tag_definitions_active ON tag_definitions(active, category_id, name)")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
        (MIGRATION_V2_03, now, "标签库与人工标注"),
    )


def _apply_v2_03_1(connection: sqlite3.Connection) -> None:
    now = utc_now_text()
    for category, tags in INITIAL_TAGS.items():
        category_id = connection.execute("SELECT id FROM tag_categories WHERE name=?", (category,)).fetchone()["id"]
        for tag in tags:
            row = connection.execute("SELECT id FROM tag_definitions WHERE name=? COLLATE NOCASE ORDER BY id LIMIT 1", (tag,)).fetchone()
            if row:
                connection.execute("UPDATE tag_definitions SET category_id=?, active=1, updated_at=? WHERE id=?", (category_id, now, row["id"]))
            else:
                connection.execute("INSERT INTO tag_definitions(category_id,name,active,created_at,updated_at) VALUES (?,?,1,?,?)", (category_id, tag, now, now))
    connection.execute("INSERT INTO schema_migrations(version,applied_at,description) VALUES (?,?,?)", (MIGRATION_V2_03_1, now, "按标注.txt校正初始标签分类"))


def _apply_v2_05(connection: sqlite3.Connection) -> None:
    _execute_schema_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS legacy_derivative_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            derivative_id INTEGER NOT NULL REFERENCES asset_derivatives(id),
            source_path TEXT NOT NULL,
            target_path TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_derivative_migrations_derivative ON legacy_derivative_migrations(derivative_id, created_at);
        """,
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
        (MIGRATION_V2_05, utc_now_text(), "多比例导出、元数据与旧 1280 文件迁移日志"),
    )


def _apply_v2_99_1(connection: sqlite3.Connection) -> None:
    """Record explicit per-image annotation confirmation.

    Existing tag relations are intentionally not copied here: the annotation
    queue also treats any existing tag relation as already handled, while this
    table records an explicit confirmation even when the user confirms no tags.
    """
    _execute_schema_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS asset_annotations (
            asset_id INTEGER PRIMARY KEY REFERENCES assets(id),
            confirmed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_asset_annotations_confirmed_at ON asset_annotations(confirmed_at);
        """,
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
        (MIGRATION_V2_99_1, utc_now_text(), "逐图标注确认状态"),
    )


def _apply_v2_99_2(connection: sqlite3.Connection) -> None:
    """Add durable ownership metadata for one shared SQLite job queue."""
    _ensure_column(connection, "background_jobs", "worker_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "background_jobs", "heartbeat_at", "TEXT")
    _execute_schema_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS job_worker_leases (
            job_type TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_background_jobs_owner
            ON background_jobs(job_type, status, worker_id, heartbeat_at);
        """,
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
        (MIGRATION_V2_99_2, utc_now_text(), "跨终端任务队列租约与心跳"),
    )


def _apply_v2_13(connection: sqlite3.Connection) -> None:
    """Invalidate cached visual hashes when the source image changes."""
    _ensure_column(connection, "asset_visual_hashes", "source_fingerprint", "TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
        (MIGRATION_V2_13, utc_now_text(), "重复审查后台队列与画面指纹缓存校验"),
    )


def init_db(db_path: Path | None = None) -> Path | None:
    """Ensure V2 schema. A pre-V2 database is backed up before any migration."""
    path = (db_path or DB_PATH).resolve()
    existed_with_data = path.exists() and path.stat().st_size > 0
    connection = get_db(path)
    try:
        backup_path = None
        if not _migration_applied(connection, MIGRATION_V2_01):
            backup_path = _backup_database(connection, path) if existed_with_data else None
            with transaction(connection):
                _create_migration_table(connection)
                _apply_v2_01(connection, backup_path)
        if not _migration_applied(connection, MIGRATION_V2_02):
            with transaction(connection):
                _apply_v2_02(connection)
        if not _migration_applied(connection, MIGRATION_V2_03):
            with transaction(connection):
                _apply_v2_03(connection)
        if not _migration_applied(connection, MIGRATION_V2_03_1):
            with transaction(connection):
                _apply_v2_03_1(connection)
        if not _migration_applied(connection, MIGRATION_V2_05):
            with transaction(connection):
                _apply_v2_05(connection)
        if not _migration_applied(connection, MIGRATION_V2_99_1):
            with transaction(connection):
                _apply_v2_99_1(connection)
        if not _migration_applied(connection, MIGRATION_V2_99_2):
            with transaction(connection):
                _apply_v2_99_2(connection)
        if not _migration_applied(connection, MIGRATION_V2_13):
            with transaction(connection):
                _apply_v2_13(connection)
        # Date filtering intentionally compares the calendar-day prefix so
        # legacy space-separated timestamps and V2 ISO timestamps coexist.
        # This expression index keeps that compatibility query responsive.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_imported_day ON assets(substr(imported_at, 1, 10))"
        )
        return backup_path
    finally:
        connection.close()


def migration_report(db_path: Path | None = None) -> dict[str, object]:
    """Read database migration state without modifying the database."""
    connection = get_db(db_path, readonly=True)
    try:
        tables = [row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )]
        migrations = []
        if "schema_migrations" in tables:
            migrations = [dict(row) for row in connection.execute(
                "SELECT version, applied_at, description, backup_path FROM schema_migrations ORDER BY applied_at"
            )]
        counts = {
            "assets": connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] if "assets" in tables else 0,
            "asset_derivatives": connection.execute("SELECT COUNT(*) FROM asset_derivatives").fetchone()[0]
            if "asset_derivatives" in tables else 0,
            "downloads": connection.execute("SELECT COALESCE(SUM(downloads), 0) FROM assets").fetchone()[0]
            if "assets" in tables else 0,
            "clicks": connection.execute("SELECT COALESCE(SUM(clicks), 0) FROM assets").fetchone()[0]
            if "assets" in tables else 0,
        }
        return {"tables": tables, "migrations": migrations, "counts": counts}
    finally:
        connection.close()


if __name__ == "__main__":
    # Deliberately read-only: this is the handoff inspection command.
    print(json.dumps(migration_report(), ensure_ascii=False, indent=2))
