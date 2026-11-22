"""Чтение и аккуратная запись `config/keywords.json` из веб-панели.

Редактируется только безопасное подмножество полей (регион, пороги
релевантности, режимы, расписание). Перед записью делается резервная копия
`.bak`, файл пишется атомарно (через временный файл и `os.replace`), поэтому
сбой записи не оставит конфиг в нерабочем состоянии.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..config import ROOT

CONFIG_PATH = ROOT / "config" / "keywords.json"
BACKUP_PATH = ROOT / "config" / "keywords.json.bak"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def read_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def write_config(data: dict) -> None:
    if BACKUP_PATH.exists():
        BACKUP_PATH.unlink()
    CONFIG_PATH.replace(BACKUP_PATH)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def parse_times(raw: str) -> tuple[list[str], list[str]]:
    """Строка «08:00, 20:00» -> (список времени, список ошибок)."""
    times, errors = [], []
    for part in (raw or "").replace(";", ",").split(","):
        value = part.strip()
        if not value:
            continue
        if not TIME_RE.match(value):
            errors.append(f"Некорректное время «{value}» (нужен формат ЧЧ:ММ)")
            continue
        if value not in times:
            times.append(value)
    if not times and not errors:
        errors.append("Укажите хотя бы одно время в формате ЧЧ:ММ")
    return sorted(times), errors


def apply_form(data: dict, form) -> tuple[dict, list[str]]:
    """Применяет значения формы к конфигурации; возвращает (конфиг, ошибки)."""
    errors: list[str] = []
    result = json.loads(json.dumps(data))  # глубокая копия

    region = (form.get("region") or "").strip()
    if not region:
        errors.append("Укажите регион поиска")
    result["region"] = region

    for key, label in (("high", "Порог «Высокая»"), ("medium", "Порог «Средняя»")):
        raw = (form.get(key) or "").strip()
        try:
            value = int(raw)
        except ValueError:
            errors.append(f"{label}: нужно целое число")
            continue
        if not 0 <= value <= 100:
            errors.append(f"{label}: значение вне диапазона 0..100")
            continue
        result.setdefault("thresholds", {})[key] = value

    result["headless"] = bool(form.get("headless"))
    result["demo_include_all"] = bool(form.get("demo_include_all"))

    times, time_errors = parse_times(form.get("schedule_times") or "")
    errors.extend(time_errors)
    if times:
        result.setdefault("schedule", {})["times"] = times

    # Глубина и сроки поиска (блок "search"). Пустое поле — значение по умолчанию.
    search = result.setdefault("search", {})

    def _search_int(field, key, default, lo, hi):
        raw = (form.get(field) or "").strip()
        if raw == "":
            search.setdefault(key, default)
            return
        try:
            value = int(raw)
        except ValueError:
            errors.append(f"{field}: нужно целое число")
            return
        if not lo <= value <= hi:
            errors.append(f"{field}: значение вне диапазона {lo}..{hi}")
            return
        search[key] = value

    _search_int("search_per_page", "records_per_page", 100, 10, 200)
    if search.get("records_per_page") not in (10, 20, 50, 100, 200):
        search["records_per_page"] = min((10, 20, 50, 100, 200),
                                         key=lambda x: abs(x - search["records_per_page"]))
    _search_int("search_max_pages", "max_pages", 5, 1, 50)
    _search_int("search_max_cards", "max_cards", 1000, 0, 100000)
    _search_int("search_published_days", "published_days", 30, 0, 3650)
    if any(key in form for key in ("search_per_page", "search_max_pages", "search_skip_expired")):
        search["skip_expired"] = bool(form.get("search_skip_expired"))
    if "search_ca_bundle" in form:
        search["ca_bundle"] = (form.get("search_ca_bundle") or "").strip()

    thresholds = result.get("thresholds", {})
    if thresholds.get("medium", 0) > thresholds.get("high", 100):
        errors.append("Порог «Средняя» не должен быть выше порога «Высокая»")

    return result, errors
