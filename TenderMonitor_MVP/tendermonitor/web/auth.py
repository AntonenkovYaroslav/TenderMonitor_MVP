"""Аутентификация и разграничение прав веб-панели.

Сессия — штатная подписанная cookie Flask (секретный ключ лежит в
`data/app_secret.key` и переживает перезапуск сервера). В cookie хранится
только идентификатор пользователя; пароли — в виде хешей Werkzeug
(pbkdf2), в открытом виде не сохраняются никогда.
"""
from __future__ import annotations

import secrets
from functools import wraps

from flask import abort, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import appdb

ROLE_LABELS = appdb.ROLE_LABELS


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return check_password_hash(password_hash, password or "")
    except ValueError:
        return False


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf() -> bool:
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    stored = session.get("csrf") or ""
    return bool(sent) and bool(stored) and secrets.compare_digest(str(sent), str(stored))


def current_user():
    user_id = session.get("uid")
    if not user_id:
        return None
    user = appdb.get_user(user_id)
    if not user or not user["is_active"]:
        session.clear()
        return None
    return user


def has_permission(user, permission: str) -> bool:
    if not user:
        return False
    return permission in appdb.PERMISSIONS.get(user.get("role", ""), set())


def login_user(user):
    session.clear()
    session["uid"] = user["id"]
    session.permanent = True
    session["csrf"] = secrets.token_urlsafe(32)
    appdb.touch_login(user["id"])


def logout_user():
    session.clear()


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("web.login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapper


def role_required(*permissions):
    """Доступ только для пользователей с указанным правом (view/run/edit/manage)."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("web.login", next=request.full_path))
            if not has_permission(user, permissions[0]):
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator
