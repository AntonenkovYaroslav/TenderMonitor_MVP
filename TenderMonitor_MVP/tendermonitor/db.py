import sqlite3
from .config import DB, DATA

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
}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        con.execute(BASE_SCHEMA)
        existing = {
            row["name"] for row in con.execute("PRAGMA table_info(tenders)").fetchall()
        }
        for column, sql_type in NEW_COLUMNS.items():
            if column not in existing:
                con.execute(f"ALTER TABLE tenders ADD COLUMN {column} {sql_type}")
        con.commit()
