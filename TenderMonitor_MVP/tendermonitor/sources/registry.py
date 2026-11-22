"""Реестр официальных источников из config/source_urls.txt и сбор по ним.

Файл содержит строки «Метка: URL». Каждый URL классифицируется:

* ``eis``            — портал ЕИС (zakupki.gov.ru): сбор через eis_browser.run_once;
* ``open_data_file`` — прямая ссылка на файл данных (.csv/.zip/.xlsx): импорт
                       через open_data.import_open_data;
* ``open_data_catalog`` — страница/каталог открытых данных (data.gov.ru,
                       rlw.gov.ru/opendata): поиск прямых файлов данных,
                       проверка ближайшего файла на «закупочные» колонки;
                       наборы-указатели и SPA-каталоги аккуратно пропускаются;
* ``portal_unsupported`` — портал закупок без адаптера (zakupki.gov39.ru):
                       проверка доступности и пропуск с логом;
* ``unknown``        — прочее: проверка доступности и пропуск.

Ошибка одного источника не валит весь сбор: collect_all собирает статусы по
каждому источнику (найдено/импортировано/отфильтровано/пропущено и лимиты)
и в конце один раз обновляет Excel.
"""
from __future__ import annotations

import logging
import re
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..config import ROOT, ssl_contexts
from ..db import init_db
from ..exporter import export

SOURCE_FILE = ROOT / "config" / "source_urls.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TenderMonitorMVP/0.3 (source registry)"
_DATA_FILE_RE = re.compile(r'href=["\']([^"\']+\.(?:csv|zip|xlsx))["\']', re.I)
_PROBE_TIMEOUT = 40
_PROBE_MAX_BYTES = 3 * 1024 * 1024


def read_sources(path: str | Path | None = None) -> list[dict]:
    """Разобрать config/source_urls.txt в список {label, url, kind}.

    Пустые строки и комментарии (#) пропускаются. Метка — текст до URL;
    если метки нет, используется хост. Отсутствующий файл — пустой список.
    """
    path = Path(path) if path is not None else SOURCE_FILE
    if not path.exists():
        logging.warning("source registry: файл %s не найден", path)
        return []
    text = path.read_text(encoding="utf-8-sig")
    sources = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.search(r"https?://\S+", line)
        if not match:
            logging.warning("source registry: строка без URL пропущена: %r", line)
            continue
        url = match.group(0).rstrip(".,;)")
        label = line[:match.start()].strip().rstrip(":").strip()
        if not label:
            label = urlsplit(url).netloc or url
        sources.append({"label": label, "url": url, "kind": classify_source(url)})
    return sources


def classify_source(url: str) -> str:
    host = (urlsplit(url).netloc or "").lower().split(":")[0]
    path = (urlsplit(url).path or "").lower()
    if host == "zakupki.gov.ru" or host.endswith(".zakupki.gov.ru"):
        return "eis"
    if path.endswith((".csv", ".zip", ".xlsx")):
        return "open_data_file"
    if host == "data.gov.ru" or host.endswith(".data.gov.ru"):
        return "open_data_catalog"
    if "opendata" in path or host.startswith("opendata."):
        return "open_data_catalog"
    if "zakupki" in host or host.endswith("gov39.ru"):
        return "portal_unsupported"
    return "unknown"


def _fetch(url: str, timeout: int = _PROBE_TIMEOUT, max_bytes: int = _PROBE_MAX_BYTES):
    """GET с системным доверием, затем с bundled CA. Проверка не отключается.

    Возвращает (status, content_type, body, final_url). Сетевые/TLS ошибки
    пробрасываются — вызывающий код решает, как их залогировать.
    """
    last_error = None
    for context in ssl_contexts():
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
                body = resp.read(max_bytes + 1)
                return resp.status, resp.headers.get("Content-Type", ""), body, resp.geturl()
        except Exception as exc:  # noqa: BLE001 - пробуем следующий контекст
            last_error = exc
    raise last_error if last_error else RuntimeError("не удалось получить URL")


def _is_data_file(ctype: str, body: bytes) -> bool:
    low = (ctype or "").lower()
    if body[:2] == b"PK":  # ZIP
        return True
    return "csv" in low or "zip" in low or "spreadsheet" in low or "excel" in low


def _result(src, status, found=0, imported=0, filtered=0, skipped=0, detail="", limits=None):
    return {
        "label": src["label"], "url": src["url"], "kind": src["kind"],
        "status": status, "found": found, "imported": imported,
        "filtered": filtered, "skipped": skipped, "detail": detail,
        "limits": limits or {},
    }


def _collect_eis(src, region, limit, pages, per_page, days):
    from .eis_browser import run_once

    stats: dict = {}
    found, imported, _report = run_once(
        region=region, limit=limit, pages=pages, per_page=per_page, days=days, stats=stats,
    )
    return _result(
        src, "ok", found=found, imported=imported,
        filtered=stats.get("filtered_out", 0) + stats.get("expired_out", 0),
        skipped=stats.get("skipped", 0),
        detail=f"метод={stats.get('method', '?')}; уже видели={stats.get('already_seen', 0)}",
        limits=stats.get("limits", {}),
    )


def _collect_open_data_file(src):
    from .open_data import import_open_data

    limits = {"timeout_s": 45, "max_mb": 250}
    try:
        imported, skipped, parsed = import_open_data(src["url"])
    except ValueError as exc:
        # Файл скачан, но это не закупки (колонки/кодировка/формат) —
        # аккуратный пропуск, а не ошибка всего сбора.
        return _result(src, "skipped",
                       detail=f"файл не распознан как закупки ({exc}); пропущен",
                       limits=limits)
    return _result(
        src, "ok", found=parsed, imported=imported, filtered=skipped,
        detail="прямой файл открытых данных",
        limits=limits,
    )


def _collect_catalog(src):
    """Каталог открытых данных: найти прямые файлы, проверить ближайший.

    Файл импортируется только если его колонки распознаются как закупочные
    (open_data требует source_id+title). Наборы-указатели (например
    rlw list.csv: property,title,value,format) и SPA-каталога без прямых
    файлов аккуратно пропускаются с пояснением в логе.
    """
    from .open_data import import_open_data

    limits = {"timeout_s": _PROBE_TIMEOUT, "max_mb": _PROBE_MAX_BYTES // (1024 * 1024),
              "files_inspected": 1}
    status, ctype, body, final = _fetch(src["url"])
    if _is_data_file(ctype, body):
        try:
            imported, skipped, parsed = import_open_data(final)
        except ValueError as exc:
            return _result(src, "skipped",
                           detail=f"каталог отдал файл, но это не закупки ({exc}); пропущен",
                           limits=limits)
        return _result(src, "ok", found=parsed, imported=imported, filtered=skipped,
                       detail="каталог отдал прямой файл данных", limits=limits)

    text = body.decode("utf-8", "replace")
    links = []
    for href in _DATA_FILE_RE.findall(text):
        if href not in links:
            links.append(href)
    if not links:
        return _result(
            src, "skipped",
            detail="каталог/SPA без прямых файлов данных в HTML "
                   "(нужна прямая ссылка на CSV/ZIP с закупками или API)",
            limits=limits,
        )
    # Относительные ссылки разрешаем от финального URL (после редиректов).
    candidate = urljoin(final, links[0])
    try:
        imported, skipped, parsed = import_open_data(candidate)
    except ValueError as exc:
        return _result(
            src, "skipped", found=len(links),
            detail=f"найдено ссылок на наборы: {len(links)}; ближайший файл не закупки "
                   f"({exc}); пропущен",
            limits=limits,
        )
    return _result(
        src, "ok", found=parsed, imported=imported, filtered=skipped,
        detail=f"каталог: импортирован набор {candidate}", limits=limits,
    )


def _probe_only(src, reason):
    limits = {"timeout_s": _PROBE_TIMEOUT, "max_mb": _PROBE_MAX_BYTES // (1024 * 1024)}
    try:
        status, ctype, body, final = _fetch(src["url"])
    except Exception as exc:  # noqa: BLE001 - источник недоступен, не валим сбор
        return _result(src, "skipped", detail=f"{reason}; недоступен: {exc}", limits=limits)
    return _result(
        src, "skipped",
        detail=f"{reason}; страница доступна (HTTP {status}, {len(body)} байт), "
               f"адаптер не реализован",
        limits=limits,
    )


def collect_source(src, region=None, limit=None, pages=None, per_page=None, days=None):
    kind = src.get("kind") or classify_source(src["url"])
    src = dict(src, kind=kind)
    if kind == "eis":
        return _collect_eis(src, region, limit, pages, per_page, days)
    if kind == "open_data_file":
        return _collect_open_data_file(src)
    if kind == "open_data_catalog":
        return _collect_catalog(src)
    if kind == "portal_unsupported":
        return _probe_only(src, "портал закупок без адаптера")
    return _probe_only(src, "неподдерживаемый источник")


def collect_all(region=None, limit=None, pages=None, per_page=None, days=None, sources=None):
    """Сбор по всем источникам реестра. Ошибка одного не валит остальные.

    Возвращает (results, report_path): список статусов по источникам и путь
    к обновлённому Excel.
    """
    init_db()
    sources = sources if sources is not None else read_sources()
    if not sources:
        logging.warning("collect-sources: реестр источников пуст")
    results = []
    for src in sources:
        logging.info("source[%s] kind=%s url=%s — старт", src["label"], src["kind"], src["url"])
        try:
            result = collect_source(src, region=region, limit=limit, pages=pages,
                                    per_page=per_page, days=days)
        except Exception as exc:  # noqa: BLE001 - изоляция ошибки источника
            logging.exception("source[%s] failed", src["label"])
            result = _result(src, "error", detail=f"{type(exc).__name__}: {exc}")
        results.append(result)
        logging.info(
            "source[%s] status=%s found=%s imported=%s filtered=%s skipped=%s limits=%s detail=%s",
            result["label"], result["status"], result["found"], result["imported"],
            result["filtered"], result["skipped"], result["limits"], result["detail"],
        )
    report, total = export()
    logging.info("collect-sources done: источников=%d записей в базе=%d report=%s",
                 len(results), total, report)
    return results, report