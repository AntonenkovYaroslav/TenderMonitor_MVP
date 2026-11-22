"""Канонический ключ закупки для сквозной дедупликации между источниками.

Одна и та же закупка приходит из разных каналов (ЕИС RSS/браузер, региональные
порталы, открытые данные, CSV/XLSX/XML) с разными URL и форматами номера.
Ключ строится так:

1. ``num:<цифры>`` — нормализованный номер извещения/закупки, извлечённый из
   URL (``regNumber=``, ``purchaseNumber=``, ``noticeNumber=``, ``purchaseId=``,
   ``noticeId=``, числовой ``searchString=``) или из ``source_id``. Длина не
   фиксирована: принимаются цифровые строки от 8 до 25 знаков (19-значные
   номера ЕИС, 20-значные нового формата, более короткие региональные).
   36-значный ИКЗ номером НЕ считается: это код позиции плана-графика, он
   длиннее номера извещения и склеивал бы разные закупки одного плана.
2. ``guid:<uuid>`` — если номера нет, но в URL/source_id есть guid извещения
   (устойчивый идентификатор документа ЕИС).
3. ``pair:<source_id>|<нормализованный url>`` — безопасный fallback: запись
   не теряется и не склеивается с другими, даже если номер извлечь не удалось.

Нормализация URL для fallback: нижний регистр схемы/хоста, без ``www.``,
без фрагмента и трекинговых параметров (utm_*, fbclid, gclid, yclid),
параметры запроса отсортированы, завершающий ``/`` пути убран.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# Параметры URL, в которых ЕИС и агрегаторы передают номер извещения.
_NUMBER_PARAMS = ("regnumber", "purchasenumber", "noticenumber", "purchaseid", "noticeid")
# searchString — поисковая строка; номером считается только чисто цифровое значение.
_SEARCH_PARAM = "searchstring"
_GUID_PARAMS = ("guid",)

_MIN_NUM_LEN = 8
_MAX_NUM_LEN = 25
_IKZ_LEN = 36
# Цифровая последовательность внутри текстового source_id («Извещение № ...»):
# короче 11 знаков слишком легко спутать с датами/кодами, поэтому не берём.
_EMBEDDED_MIN_LEN = 11

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "utm_id", "fbclid", "gclid", "yclid", "_openstat"}
_UUID_RE = re.compile(r"^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NUM_SIGN_RE = re.compile(r"^№\s*")


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _number_ok(digits: str) -> bool:
    return _MIN_NUM_LEN <= len(digits) <= _MAX_NUM_LEN and len(digits) != _IKZ_LEN


def _query_params(url: str) -> list[tuple[str, str]]:
    try:
        return parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return []


def number_from_url(url: str) -> str:
    """Номер извещения из параметров URL; пустая строка, если не найден."""
    params = {key.lower(): value for key, value in _query_params(url)}
    for key in _NUMBER_PARAMS:
        digits = _digits_only(params.get(key, ""))
        if _number_ok(digits):
            return digits
    search = params.get(_SEARCH_PARAM, "").strip()
    if re.fullmatch(r"\d+", search) and _number_ok(search):
        return search
    return ""


def number_from_source_id(source_id: str) -> str:
    """Номер из source_id: чистое число (возможно с «№») или длинная цифровая
    вставка в тексте. ИКЗ (36 знаков) и короткие коды номером не считаются."""
    text = _NUM_SIGN_RE.sub("", (source_id or "").strip())
    text = re.sub(r"\s+", "", text)
    if re.fullmatch(r"\d+", text) and _number_ok(text):
        return text
    runs = [run for run in re.findall(r"\d+", text)
            if len(run) >= _EMBEDDED_MIN_LEN and _number_ok(run)]
    return max(runs, key=len) if runs else ""


def _normalize_guid(value: str) -> str:
    text = (value or "").strip().lower()
    return text if _UUID_RE.match(text) else ""


def guid_from_url(url: str) -> str:
    params = {key.lower(): value for key, value in _query_params(url)}
    for key in _GUID_PARAMS:
        guid = _normalize_guid(params.get(key, ""))
        if guid:
            return guid
    return ""


def guid_from_source_id(source_id: str) -> str:
    return _normalize_guid(source_id)


def normalize_url(url: str) -> str:
    """Устойчивое представление URL для fallback-ключа (не для перехода)."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    if not parts.scheme or not parts.netloc:
        return url.lower()
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    params = sorted((key, value) for key, value in
                    parse_qsl(parts.query, keep_blank_values=True)
                    if key.lower() not in _TRACKING_PARAMS)
    query = "&".join(f"{key}={value}" for key, value in params)
    return urlunsplit((scheme, host, path, query, ""))


def canonical_key(source_id: str, url: str) -> str:
    """Канонический ключ записи: num -> guid -> pair (fallback)."""
    source_id = (source_id or "").strip()
    url = (url or "").strip()
    number = number_from_url(url) or number_from_source_id(source_id)
    if number:
        return "num:" + number
    guid = guid_from_url(url) or guid_from_source_id(source_id)
    if guid:
        return "guid:" + guid
    return "pair:" + source_id + "|" + normalize_url(url)


def merge_values(existing: dict, incoming: dict, fields) -> dict:
    """Слияние полей: новое непустое значение wins, пустое не затирает старое.

    ``note`` и ``first_seen`` не входят в fields — они сохраняются за записью.
    """
    merged = dict(existing)
    for field in fields:
        value = incoming.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        merged[field] = value
    return merged