import logging
import sqlite3
from contextlib import contextmanager
from .config import DB, DATA
from .dedup import canonical_key, merge_values

# Keep the original columns and add new fields without deleting existing records.
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL,
    address TEXT,
    customer TEXT,
    deadline TEXT,
    contact_name TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    url TEXT,
    region TEXT,
    description TEXT,
    published_at TEXT,
    category TEXT,
    score INTEGER,
    relevance TEXT,
    region_check TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id,url)
)
"""

NEW_COLUMNS = {
    "organizer": "TEXT",
    "deadline_time": "TEXT",
    "object_address": "TEXT",
    # Заметка оператора из веб-панели; импорт её не перезаписывает.
    "note": "TEXT",
    # Канонический ключ сквозной дедупликации (num:/guid:/pair:). Заполняется
    # при импорте и миграцией; по нему записи из разных источников сливаются.
    "canonical_key": "TEXT",
}

# Поля, которые сливаются при дедупликации (непустое новое значение побеждает,
# пустое не затирает заполненное). note/first_seen/source_id/id сюда не входят.
MERGE_FIELDS = (
    "title", "price", "address", "customer", "deadline", "contact_name",
    "contact_phone", "contact_email", "url", "region", "description",
    "published_at", "organizer", "deadline_time", "object_address",
)

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL: веб-панель читает базу, пока сборщик в неё пишет.
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return con

@contextmanager
def session():
    """Transaction context that always closes the connection.

    `with connect() as con` only commits/rolls back; the connection itself stays
    open while `con` is alive, which keeps data/tenders.db locked on Windows.
    """
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def init_db():
    with session() as con:
        con.execute(BASE_SCHEMA)
        existing = {
            row["name"] for row in con.execute("PRAGMA table_info(tenders)").fetchall()
        }
        for column, sql_type in NEW_COLUMNS.items():
            if column not in existing:
                con.execute(f"ALTER TABLE tenders ADD COLUMN {column} {sql_type}")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenders_canonical ON tenders(canonical_key)"
        )
        _backfill_canonical(con)
        con.commit()


def _backfill_canonical(con):
    """Проставить canonical_key старым записям и слить накопленные дубли.

    Идемпотентно: ключ вычисляется только для строк с NULL; сливаются только
    группы, где несколько строк делят один канонический ключ. Сохраняются
    note, first_seen и заполненные поля; классификация пересчитывается.
    """
    rows = con.execute(
        "SELECT id, source_id, url FROM tenders WHERE canonical_key IS NULL"
    ).fetchall()
    for row in rows:
        key = canonical_key(row["source_id"], row["url"])
        con.execute(
            "UPDATE tenders SET canonical_key=? WHERE id=?", (key, row["id"])
        )
    if rows:
        logging.info("dedup: backfilled canonical_key for %d existing rows", len(rows))

    from .classifier import classify

    groups = con.execute(
        "SELECT canonical_key FROM tenders WHERE canonical_key IS NOT NULL "
        "GROUP BY canonical_key HAVING COUNT(*) > 1"
    ).fetchall()
    removed = 0
    for (key,) in groups:
        duplicates = con.execute(
            "SELECT * FROM tenders WHERE canonical_key=? ORDER BY id", (key,)
        ).fetchall()
        keep = dict(duplicates[0])
        for other in duplicates[1:]:
            keep = merge_values(keep, dict(other), MERGE_FIELDS)
            if not keep.get("note") and other["note"]:
                keep["note"] = other["note"]
            if other["first_seen"] and (
                not keep.get("first_seen") or other["first_seen"] < keep["first_seen"]
            ):
                keep["first_seen"] = other["first_seen"]
        classified = classify(keep)
        keep.update(classified)
        fields = MERGE_FIELDS + ("note", "first_seen", "category", "score",
                                 "relevance", "region_check")
        assignments = ", ".join(f"{field}=?" for field in fields)
        con.execute(
            f"UPDATE tenders SET {assignments} WHERE id=?",
            [keep.get(field) for field in fields] + [keep["id"]],
        )
        con.execute(
            "DELETE FROM tenders WHERE canonical_key=? AND id<>?", (key, keep["id"])
        )
        removed += len(duplicates) - 1
    if removed:
        logging.info("dedup: merged %d legacy duplicate rows", removed)
