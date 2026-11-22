"""Маршруты веб-панели Tender Monitor (Flask blueprints)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from flask import (Blueprint, abort, flash, redirect, render_template, request, url_for)

from ..classifier import explain, weights
from ..config import settings, search_settings
from ..db import init_db, session as db_session
from . import appdb, auth, cfg, jobs

bp = Blueprint("web", __name__)

BASE_FROM = (
    "FROM tenders AS current WHERE current.id = ("
    "SELECT MAX(previous.id) FROM tenders AS previous "
    "WHERE COALESCE(previous.canonical_key, 'row:' || previous.id) = "
    "COALESCE(current.canonical_key, 'row:' || current.id))"
)
# Ключи фильтров списка закупок. В шаблон передаются только они: посторонние
# параметры запроса (page, csrf_token, мусор из старых ссылок) не должны
# попадать ни в значения формы, ни в ссылки пагинации — иначе url_for
# получает конфликтующий аргумент page и страница падает с Ошибкой 500.
FILTER_KEYS = (
    "q", "category", "relevance", "region_check", "region",
    "price_min", "price_max", "date_from", "date_to",
)

# Артефакты парсера: метки страницы ЕИС, попавшие в значения полей
# («Регион» вместо названия заказчика и т.п.). Скрываются только при
# отображении по точному совпадению; данные в БД не изменяются.
_LABEL_JUNK = {
    "регион",
    "субъект российской федерации",
    "наименование заказчика",
    "место нахождения",
    "дата начала исполнения контракта",
    "применение национального режима по ст. 14 закона № 44-фз",
    "нмцк",
}

# Склеенный текст карточки: по нему работает поиск по словоформам.
SEARCH_TEXT_SQL = (
    "(COALESCE(current.title,'') || ' ' || COALESCE(current.description,'') || ' ' "
    "|| COALESCE(current.customer,'') || ' ' || COALESCE(current.organizer,'') || ' ' "
    "|| COALESCE(current.object_address,'') || ' ' || COALESCE(current.address,'') || ' ' "
    "|| COALESCE(current.source_id,''))"
)


# --- общие помощники ----------------------------------------------------------

def _float(value):
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _search_variants(query: str) -> list[list[str]]:
    """Варианты написания для поиска по тексту (по одному списку на слово).

    Поиск в панели нечувствителен к словоформам: «песок» находит «поставка
    песка», «ремонт» — «ремонта дороги». Для каждого слова берутся префиксы
    на 1 и 2 символа короче (минимум 3 символа); варианты одного слова
    объединяются через OR, разные слова — через AND.
    """
    groups = []
    for token in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", query or ""):
        token = token.lower()
        variants = [token[:size] for size in (len(token), len(token) - 1, len(token) - 2) if size >= 3]
        if variants:
            groups.append(variants)
    return groups


def _filter_parts(include_relevance: bool = True):
    """Условия WHERE из query-строки (фильтры панели)."""
    where, args = [], []
    for group in _search_variants(request.args.get("q") or ""):
        where.append("(" + " OR ".join(f"{SEARCH_TEXT_SQL} LIKE ?" for _ in group) + ")")
        args.extend([f"%{variant}%" for variant in group])
    category = (request.args.get("category") or "").strip()
    if category:
        where.append("category = ?")
        args.append(category)
    if include_relevance:
        relevance = (request.args.get("relevance") or "").strip()
        if relevance:
            where.append("relevance = ?")
            args.append(relevance)
    region_check = (request.args.get("region_check") or "").strip()
    if region_check:
        where.append("region_check = ?")
        args.append(region_check)
    region = (request.args.get("region") or "").strip()
    if region:
        where.append("(region LIKE ? OR COALESCE(NULLIF(object_address,''), address) LIKE ?)")
        args.extend([f"%{region}%"] * 2)
    price_min = _float(request.args.get("price_min"))
    if price_min is not None:
        where.append("price IS NOT NULL AND price >= ?")
        args.append(price_min)
    price_max = _float(request.args.get("price_max"))
    if price_max is not None:
        where.append("price IS NOT NULL AND price <= ?")
        args.append(price_max)
    date_from = (request.args.get("date_from") or "").strip()
    if date_from:
        where.append("date(current.first_seen, 'localtime') >= ?")
        args.append(date_from)
    date_to = (request.args.get("date_to") or "").strip()
    if date_to:
        where.append("date(current.first_seen, 'localtime') <= ?")
        args.append(date_to)
    return where, args


def _local_datetime(value) -> str:
    """Дата из базы (UTC) в местном времени: «22.11.2026 00:30»."""
    if not value:
        return ""
    try:
        moment = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(value)
    return moment.astimezone().strftime("%d.%m.%Y %H:%M")


@bp.app_template_filter("localdt")
def localdt(value):
    return _local_datetime(value)


@bp.app_template_filter("money")
def money(value) -> str:
    """Денежная сумма в русском формате: «2 874 509,65»."""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


@bp.app_template_filter("clean")
def clean_text(value) -> str:
    """Текст поля для отображения: схлопывает пробелы (включая неразрывные)
    и скрывает явные артефакты парсера — метки страницы вместо значения.
    Только отображение: исходные данные в БД не трогаются."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text.lower() in _LABEL_JUNK:
        return ""
    return text


@bp.app_template_filter("ddate")
def ddate(value) -> str:
    """Дата в едином виде «ДД.ММ.ГГГГ» (с временем, если оно есть).

    В базе встречаются и «10.12.2026», и ISO-строки «2026-12-02 10:00»;
    нераспознанное значение выводится как есть, без падения страницы."""
    text = clean_text(value)
    if not text:
        return ""
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.strftime("%d.%m.%Y %H:%M" if "%H" in fmt else "%d.%m.%Y")
    return text


def _where_clause(where) -> str:
    return (" AND " + " AND ".join(where)) if where else ""


def _run_filters(include_relevance: bool = True):
    where, args = _filter_parts(include_relevance)
    return BASE_FROM + _where_clause(where), args


def _count(sql: str, args) -> int:
    with db_session() as con:
        return con.execute(f"SELECT COUNT(*) AS n {sql}", args).fetchone()["n"]


def _categories() -> list[str]:
    names = list((settings().get("groups") or {}).keys()) + ["Не определено", "Ключевые слова"]
    with db_session() as con:
        for row in con.execute("SELECT DISTINCT category FROM tenders WHERE category <> ''").fetchall():
            if row["category"] not in names:
                names.append(row["category"])
    return names


@bp.app_context_processor
def inject_globals():
    user = auth.current_user()
    return {
        "current_user": user,
        "can_run": auth.has_permission(user, "run"),
        "can_edit": auth.has_permission(user, "edit"),
        "can_manage": auth.has_permission(user, "manage"),
        "role_labels": appdb.ROLE_LABELS,
        "csrf_token": auth.csrf_token,
        "job_running": appdb.running_job(),
    }


@bp.before_app_request
def csrf_guard():
    if request.method == "POST" and not auth.check_csrf():
        abort(400, description="Недействительный CSRF-токен. Обновите страницу и повторите действие.")


# Страницы, доступные до смены временного пароля.
_PASSWORD_CHANGE_ALLOWED = {"web.account_password", "web.logout", "static"}


@bp.before_app_request
def password_change_guard():
    """Пользователь с временным паролем работает только со сменой пароля."""
    user = auth.current_user()
    if not user or not user["must_change_password"]:
        return None
    if request.endpoint in _PASSWORD_CHANGE_ALLOWED:
        return None
    flash("Сначала смените временный пароль.", "warning")
    return redirect(url_for("web.account_password"))


# --- вход и выход -------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_value = (request.form.get("login") or "").strip()
        password = request.form.get("password") or ""
        user = appdb.get_user_by_login(login_value)
        if user and user["is_active"] and auth.verify_password(user["password_hash"], password):
            auth.login_user(user)
            target = request.args.get("next") or request.form.get("next") or ""
            if not target.startswith("/"):
                target = url_for("web.tenders")
            if user["must_change_password"]:
                flash("Смените пароль, чтобы продолжить работу.", "warning")
                return redirect(url_for("web.account_password"))
            return redirect(target)
        flash("Неверный логин или пароль.", "error")
    return render_template("login.html", next_target=request.args.get("next", ""))


@bp.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    return redirect(url_for("web.login"))


# --- список закупок -----------------------------------------------------------

@bp.route("/")
@auth.login_required
def index():
    return redirect(url_for("web.tenders"))


@bp.route("/tenders")
@auth.login_required
def tenders():
    init_db()
    sql, args = _run_filters()
    total = _count(sql, args)
    per_page = _int(appdb.get_setting("ui.per_page", "50"), 50) or 50
    page = max(1, _int(request.args.get("page", "1"), 1))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    with db_session() as con:
        rows = con.execute(
            f"SELECT current.* {sql} ORDER BY current.score DESC, current.first_seen DESC LIMIT ? OFFSET ?",
            args + [per_page, (page - 1) * per_page],
        ).fetchall()
        items = [dict(r) for r in rows]
        agg_sql, agg_args = _run_filters(include_relevance=False)
        counts = {}
        for row in con.execute(
            f"SELECT current.relevance AS rel, COUNT(*) AS n {agg_sql} GROUP BY current.relevance", agg_args
        ).fetchall():
            counts[row["rel"] or "—"] = row["n"]
        region_rows = con.execute(
            f"SELECT COUNT(*) AS n {agg_sql} AND current.region_check = ?", agg_args + ["Проверить регион"]
        ).fetchone()["n"]
    # Значения формы — только известные ключи фильтров; ссылки пагинации
    # строятся из query_args (без page и пустых полей), чтобы url_for не
    # получил второй аргумент page из query-строки.
    filters = {key: (request.args.get(key) or "").strip() for key in FILTER_KEYS}
    query_args = {key: value for key, value in filters.items() if value}
    config = settings()
    scoring = {
        "thresholds": config.get("thresholds") or {},
        "region": (config.get("region") or "").strip(),
        "regions": list(config.get("regions") or []),
        "groups": list((config.get("groups") or {}).keys()),
        "weights": weights(config),
        "stop_phrases": list(config.get("stop_phrases") or []),
    }
    # Для нулевых карточек показываем точную причину: стоп-фразы профиля или
    # отсутствие совпадений. Пересчёт только для score==0 (≤ per_page строк).
    for item in items:
        if item.get("score") == 0:
            item["stop_phrases"] = explain(item, cfg=config)["stop_phrases"]
    return render_template(
        "tenders.html",
        items=items,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        counts=counts,
        region_check_count=region_rows,
        categories=_categories(),
        filters=filters,
        query_args=query_args,
        scoring=scoring,
    )


@bp.route("/tender/<int:tender_id>")
@auth.login_required
def tender(tender_id: int):
    init_db()
    with db_session() as con:
        row = con.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        item = dict(row) if row else None
    if not item:
        abort(404, description="Закупка не найдена")
    return render_template("tender.html", item=item, explanation=explain(item))


@bp.route("/tender/<int:tender_id>/note", methods=["POST"])
@auth.role_required("edit")
def save_note(tender_id: int):
    init_db()
    note = (request.form.get("note") or "").strip()
    with db_session() as con:
        cur = con.execute("UPDATE tenders SET note=? WHERE id=?", (note, tender_id))
        con.commit()
        if cur.rowcount == 0:
            abort(404, description="Закупка не найдена")
    flash("Заметка сохранена.", "success")
    return redirect(url_for("web.tender", tender_id=tender_id))


# --- запуск сбора и задания ---------------------------------------------------

@bp.route("/run", methods=["GET", "POST"])
@auth.role_required("run")
def run():
    config = settings()
    if request.method == "POST":
        search = search_settings(config)
        hard_cap = max(500, min(5000, search["max_cards"] or 5000))
        params = {
            "region": (request.form.get("region") or "").strip(),
            "limit": max(1, min(hard_cap, _int(request.form.get("limit"), 300))),
            "keywords": (request.form.get("keywords") or "").strip(),
            "category": (request.form.get("category") or "").strip(),
        }
        for key in ("pages", "per_page"):
            value = _int(request.form.get(key), 0)
            if value > 0:
                params[key] = value
        days_raw = (request.form.get("days") or "").strip()
        if days_raw != "":
            params["days"] = max(0, _int(days_raw, 0))
        job_id, error = jobs.start_job(jobs.COLLECT_KIND, params, auth.current_user()["login"])
        if error:
            flash(error, "error")
        else:
            flash(f"Задание #{job_id} запущено: сбор идёт в фоне, следите за статусом.", "success")
            return redirect(url_for("web.job", job_id=job_id))
    return render_template(
        "run.html",
        config=config,
        search=search_settings(config),
        categories=_categories(),
        defaults={
            "region": appdb.get_setting("collect.region", config.get("region", "")),
            "limit": appdb.get_setting("collect.limit", "300"),
        },
        running=appdb.running_job(),
    )


@bp.route("/export", methods=["POST"])
@auth.role_required("run")
def export():
    job_id, error = jobs.start_job(jobs.EXPORT_KIND, {}, auth.current_user()["login"])
    if error:
        flash(error, "error")
    else:
        flash(f"Задание #{job_id} запущено: экспорт Excel.", "success")
    return redirect(url_for("web.jobs"))


@bp.route("/jobs")
@auth.role_required("run")
def job_list():
    return render_template("jobs.html", items=appdb.list_jobs(limit=100))


@bp.route("/jobs/<int:job_id>")
@auth.role_required("run")
def job(job_id: int):
    item = appdb.get_job(job_id)
    if not item:
        abort(404, description="Задание не найдено")
    return render_template("job.html", item=item, log=jobs.read_log_tail(item))


@bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@auth.role_required("run")
def job_cancel(job_id: int):
    message = jobs.cancel_job(job_id)
    flash(message, "success" if "остановлено" in message.lower() else "error")
    return redirect(url_for("web.job", job_id=job_id))


# --- пользователи (администратор) ---------------------------------------------

@bp.route("/users")
@auth.role_required("manage")
def users():
    return render_template("users.html", items=appdb.list_users(), roles=appdb.ROLES)


@bp.route("/users/create", methods=["POST"])
@auth.role_required("manage")
def user_create():
    login_value = (request.form.get("login") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role") or "viewer"
    if not login_value or not password:
        flash("Логин и пароль обязательны.", "error")
    elif len(password) < 6:
        flash("Пароль должен быть не короче 6 символов.", "error")
    elif appdb.get_user_by_login(login_value):
        flash("Пользователь с таким логином уже есть.", "error")
    else:
        appdb.create_user(login_value, auth.hash_password(password), role,
                          request.form.get("comment") or "", must_change_password=1)
        flash(f"Пользователь {login_value} создан.", "success")
    return redirect(url_for("web.users"))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@auth.role_required("manage")
def user_toggle(user_id: int):
    user = appdb.get_user(user_id)
    if not user:
        abort(404)
    if user["id"] == auth.current_user()["id"]:
        flash("Нельзя отключить собственную учётную запись.", "error")
    else:
        appdb.set_active(user_id, not user["is_active"])
        flash("Состояние пользователя изменено.", "success")
    return redirect(url_for("web.users"))


@bp.route("/users/<int:user_id>/role", methods=["POST"])
@auth.role_required("manage")
def user_role(user_id: int):
    user = appdb.get_user(user_id)
    if not user:
        abort(404)
    role = request.form.get("role") or "viewer"
    if user["id"] == auth.current_user()["id"] and role != "admin":
        flash("Нельзя снять роль администратора у себя.", "error")
    else:
        appdb.set_role(user_id, role)
        flash("Роль обновлена.", "success")
    return redirect(url_for("web.users"))


@bp.route("/users/<int:user_id>/password", methods=["POST"])
@auth.role_required("manage")
def user_password(user_id: int):
    user = appdb.get_user(user_id)
    if not user:
        abort(404)
    password = request.form.get("password") or ""
    if len(password) < 6:
        flash("Пароль должен быть не короче 6 символов.", "error")
    else:
        appdb.set_password(user_id, auth.hash_password(password), must_change_password=1)
        flash(f"Пароль пользователя {user['login']} сброшен; при следующем входе потребуется смена.", "success")
    return redirect(url_for("web.users"))


# --- настройки (администратор) -------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@auth.role_required("manage")
def settings_page():
    config = cfg.read_config()
    if request.method == "POST":
        if request.form.get("form") == "panel":
            appdb.set_setting("ui.per_page", _int(request.form.get("per_page"), 50) or 50)
            appdb.set_setting("collect.region", (request.form.get("collect_region") or "").strip())
            appdb.set_setting("collect.limit", max(1, min(5000, _int(request.form.get("collect_limit"), 300) or 300)))
            flash("Настройки панели сохранены.", "success")
            return redirect(url_for("web.settings_page"))
        updated, errors = cfg.apply_form(config, request.form)
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            cfg.write_config(updated)
            flash("Конфигурация сбора сохранена (резервная копия: config/keywords.json.bak).", "success")
            return redirect(url_for("web.settings_page"))
        config = updated
    panel = appdb.all_settings()
    return render_template("settings.html", config=config, panel=panel)


# --- личный кабинет -----------------------------------------------------------

@bp.route("/account/password", methods=["GET", "POST"])
@auth.login_required
def account_password():
    user = auth.current_user()
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        repeat = request.form.get("repeat_password") or ""
        if not auth.verify_password(user["password_hash"], current):
            flash("Текущий пароль неверен.", "error")
        elif len(new) < 6:
            flash("Новый пароль должен быть не короче 6 символов.", "error")
        elif new != repeat:
            flash("Новые пароли не совпадают.", "error")
        else:
            appdb.set_password(user["id"], auth.hash_password(new), must_change_password=0)
            flash("Пароль изменён.", "success")
            return redirect(url_for("web.tenders"))
    return render_template("password.html", user=user)


# --- обработчики ошибок -------------------------------------------------------

@bp.app_errorhandler(400)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
@bp.app_errorhandler(500)
def error_page(error):
    return render_template("error.html", error=error), getattr(error, "code", 500)
