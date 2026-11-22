"""Хранилище веб-панели: пользователи, роли, настройки панели и задания.

Почему отдельный файл `data/app.db`, а не таблица в `data/tenders.db`:

- сборщик и панель работают параллельно: панель читает `tenders.db`, а импорт
  пишет в него же. Разные файлы не блокируют друг друга (обе базы в режиме WAL);
- миграции веб-части не зависят от миграций доменной части закупок;
- учётные данные пользователей не лежат рядом с открытыми данными закупок.

Схема таблиц (SQLite, без ORM — только `sqlite3` из стандартной библиотеки):
- `users`      — логины, хеши паролей, роли, признак активности;
- `settings`   — настройки самой панели (ключ -> значение);
- `jobs`       — задания фонового запуска (сбор, экспорт) и их статус.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA

APP_DB = DATA / "app.db"

ROLES = ("admin", "operator", "viewer")
ROLE_LABELS = {
    "admin": "Администратор",
    "operator": "Оператор",
    "viewer": "Наблюдатель",
}
# Права по ролям: просмотр есть у всех, запуск сбора — у оператора и админа,
# управление пользователями и настройками — только у администратора.
PERMISSIONS = {
    "viewer": {"view"},
    "operator": {"view", "run", "edit"},
    "admin": {"view", "run", "edit", "manage"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    login TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    params TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT '',
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(APP_DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return con


def init_db():
    with connect() as con:
        con.executescript(SCHEMA)
        con.commit()


def row_to_dict(row):
    return dict(row) if row is not None else None


# --- пользователи -------------------------------------------------------------

def get_user(user_id):
    with connect() as con:
        return row_to_dict(con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_login(login: str):
    with connect() as con:
        return row_to_dict(con.execute("SELECT * FROM users WHERE login=?", (login.strip(),)).fetchone())


def list_users() -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM users ORDER BY login").fetchall()]


def create_user(login: str, password_hash: str, role: str = "viewer", comment: str = "",
                must_change_password: int = 1) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO users (login, password_hash, role, comment, must_change_password) VALUES (?,?,?,?,?)",
            (login.strip(), password_hash, role if role in ROLES else "viewer", comment.strip(), int(must_change_password)),
        )
        con.commit()
        return cur.lastrowid


def set_password(user_id: int, password_hash: str, must_change_password: int = 0):
    with connect() as con:
        con.execute(
            "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
            (password_hash, int(must_change_password), user_id),
        )
        con.commit()


def set_role(user_id: int, role: str):
    with connect() as con:
        con.execute("UPDATE users SET role=? WHERE id=?", (role if role in ROLES else "viewer", user_id))
        con.commit()


def set_active(user_id: int, is_active: bool):
    with connect() as con:
        con.execute("UPDATE users SET is_active=? WHERE id=?", (1 if is_active else 0, user_id))
        con.commit()


def set_comment(user_id: int, comment: str):
    with connect() as con:
        con.execute("UPDATE users SET comment=? WHERE id=?", (comment.strip(), user_id))
        con.commit()


def touch_login(user_id: int):
    with connect() as con:
        con.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user_id))
        con.commit()


def count_users() -> int:
    with connect() as con:
        return con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# --- настройки панели ---------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with connect() as con:
        con.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), now_iso()),
        )
        con.commit()


def all_settings() -> dict[str, str]:
    with connect() as con:
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings").fetchall()}


# --- задания ------------------------------------------------------------------

def create_job(kind: str, params: dict, created_by: str, log_path: str = "") -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO jobs (kind, status, params, created_by, log_path) VALUES (?,?,?,?,?)",
            (kind, "queued", json.dumps(params, ensure_ascii=False), created_by, log_path),
        )
        con.commit()
        return cur.lastrowid


def update_job(job_id: int, **fields):
    if not fields:
        return
    allowed = {"status", "started_at", "finished_at", "exit_code", "summary", "log_path", "pid", "params"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(job_id)
    with connect() as con:
        con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", values)
        con.commit()


def get_job(job_id: int):
    with connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["params"] = json.loads(data.get("params") or "{}")
        except (ValueError, TypeError):
            data["params"] = {}
        return data


def list_jobs(limit: int = 50) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        try:
            data["params"] = json.loads(data.get("params") or "{}")
        except (ValueError, TypeError):
            data["params"] = {}
        result.append(data)
    return result


def running_job(kind: str | None = None):
    """Активное задание (для защиты от параллельных сборов)."""
    with connect() as con:
        if kind:
            row = con.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') AND kind=? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row_to_dict(row)


def mark_interrupted_jobs():
    """Задания, оставшиеся в статусе running после перезапуска сервера."""
    with connect() as con:
        cur = con.execute(
            "UPDATE jobs SET status='interrupted', finished_at=?, summary=? "
            "WHERE status IN ('queued','running')",
            (now_iso(), "Сервер был перезапущен — задание прервано"),
        )
        con.commit()
        return cur.rowcount


def delete_job(job_id: int):
    with connect() as con:
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        con.commit()
