"""Веб-панель Tender Monitor: локальная админ-панель на Flask + Jinja2.

Стек выбран из расчёта на локальный/внутренний MVP: Flask (легкий сервер с
шаблонами Jinja2) и обычные HTML/CSS/JS без сборщика — панель открывается
двойным щелчком по `web_panel.bat` и не требует Node.js.

Запуск: `python -m tendermonitor.cli web --port 8000`.
"""
from __future__ import annotations

import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask
from werkzeug.security import generate_password_hash

from ..config import DATA
from . import appdb
from .views import bp as web_bp

WEB_DIR = Path(__file__).resolve().parent
SECRET_FILE = DATA / "app_secret.key"
DEFAULT_ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"


def _secret_key() -> str:
    """Ключ подписи cookie-сессии; создаётся один раз и хранится на диске."""
    DATA.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        value = SECRET_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    SECRET_FILE.write_text(value, encoding="utf-8")
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return value


def ensure_admin() -> str:
    """Создаёт учётную запись администратора при первом запуске. Возвращает пароль."""
    appdb.init_db()
    if appdb.count_users():
        return ""
    password = os.environ.get("TM_ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD
    appdb.create_user(DEFAULT_ADMIN_LOGIN, generate_password_hash(password),
                      "admin", "учётная запись по умолчанию", must_change_password=1)
    return password


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(WEB_DIR / "templates"),
        static_folder=str(WEB_DIR / "static"),
    )
    app.config.update(
        SECRET_KEY=_secret_key(),
        SESSION_COOKIE_NAME="tm_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        TEMPLATES_AUTO_RELOAD=True,
        JSON_SORT_KEYS=False,
    )
    app.register_blueprint(web_bp)
    appdb.init_db()
    # Задания, оставшиеся в статусе running, прерваны перезапуском сервера.
    appdb.mark_interrupted_jobs()
    return app


def add_user(login: str, role: str, password: str | None = None) -> tuple[str, str]:
    """Создание пользователя из консоли. Возвращает (пароль, ошибка)."""
    from . import appdb
    from werkzeug.security import generate_password_hash

    appdb.init_db()
    login = (login or "").strip()
    if not login:
        return "", "Логин не указан"
    if appdb.get_user_by_login(login):
        return "", "Пользователь с таким логином уже есть"
    password = password or secrets.token_urlsafe(9)
    appdb.create_user(login, generate_password_hash(password), role, "создан из консоли", must_change_password=1)
    return password, ""


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="tender-monitor-web", description="Веб-панель Tender Monitor")
    parser.add_argument("--host", default="127.0.0.1", help="адрес прослушивания (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    parser.add_argument("--debug", action="store_true", help="режим отладки Flask")
    args = parser.parse_args(argv)

    app = create_app()
    password = ensure_admin()
    print(f"Веб-панель Tender Monitor: http://{args.host}:{args.port}/", flush=True)
    if password:
        print(f"Создан администратор: логин «{DEFAULT_ADMIN_LOGIN}», пароль «{password}» — смените его при первом входе.", flush=True)
    print("Остановка: Ctrl+C", flush=True)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True, use_reloader=False)
    return 0
