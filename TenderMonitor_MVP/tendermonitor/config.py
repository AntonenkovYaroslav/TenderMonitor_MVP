from pathlib import Path
import json
import ssl
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"
DB = DATA / "tenders.db"
def settings():
    with (ROOT / "config" / "keywords.json").open(encoding="utf-8") as f:
        return json.load(f)

# ЕИС принимает recordsPerPage из этого набора; сервер фактически отдаёт не более
# 200 записей на страницу (_500 тоже возвращает 200). Значение вне набора
# округляется к ближайшему допустимому.
_PER_PAGE_ALLOWED = (10, 20, 50, 100, 200)


def search_settings(cfg=None):
    """Глубина и сроки поиска с безопасными значениями по умолчанию.

    Старый конфиг без блока "search" продолжает работать: используются
    defaults. Параметры переопределяются флагами CLI monitor-eis
    (--pages/--per-page/--days) и формой запуска в веб-панели.
    """
    cfg = cfg if cfg is not None else settings()
    raw = cfg.get("search") or {}

    def _int(key, default, lo, hi):
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(lo, min(hi, value))

    per_page = _int("records_per_page", 100, 10, 200)
    if per_page not in _PER_PAGE_ALLOWED:
        per_page = min(_PER_PAGE_ALLOWED, key=lambda x: abs(x - per_page))
    return {
        "records_per_page": per_page,
        "max_pages": _int("max_pages", 5, 1, 50),
        "max_cards": _int("max_cards", 1000, 0, 100000),
        "published_days": _int("published_days", 30, 0, 3650),
        "skip_expired": bool(raw.get("skip_expired", True)),
        "ca_bundle": str(raw.get("ca_bundle") or "").strip(),
    }


def ssl_contexts():
    """Контексты TLS для исходящих запросов: системное доверие, затем bundled CA.

    Проверка сертификатов никогда не отключается. Относительный путь
    ca_bundle из настроек разрешается от ROOT, чтобы работать после clone/ZIP.
    Используется реестром источников и импортом открытых данных.
    """
    yield ssl.create_default_context()
    bundle = search_settings().get("ca_bundle") or ""
    if bundle:
        path = Path(bundle)
        if not path.is_absolute():
            path = ROOT / path
        if path.exists():
            yield ssl.create_default_context(cafile=str(path))
