"""Importer for a CSV resource published by the Russian Open Data portal."""
from __future__ import annotations

import csv
import io
import re
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote

from ..config import ssl_contexts
from ..importer import import_rows

ALIASES = {
    "source_id": ("source_id", "regnumber", "purchasenumber", "номер закупки", "номер извещения", "реестровый номер", "номер извещения о закупке", "икз", "номер"),
    "title": ("title", "purchaseobjectinfo", "objectinfo", "наименование закупки", "объект закупки", "название закупки", "предмет контракта", "предмет закупки"),
    "price": ("price", "nmck", "начальная (максимальная) цена контракта", "начальная максимальная цена", "нмцк", "цена контракта"),
    "address": ("address", "deliveryplace", "placeofwork", "место поставки", "место выполнения работ", "адрес объекта"),
    "customer": ("customer", "customername", "customerfullname", "заказчик", "наименование заказчика", "полное наименование заказчика"),
    "deadline": ("deadline", "applicationenddate", "срок подачи заявок", "дата окончания подачи заявок", "окончание подачи заявок"),
    "contact_name": ("contact_name", "contactperson", "контактное лицо", "фио контактного лица"),
    "contact_phone": ("contact_phone", "contactphone", "телефон", "номер телефона"),
    "contact_email": ("contact_email", "contactemail", "электронная почта", "e-mail", "email"),
    "url": ("url", "href", "ссылка", "ссылка на закупку", "ссылка на извещение", "размещено в единой информационной системе в сфере закупок"),
    "region": ("region", "regionname", "регион", "субъект российской федерации", "субъект рф"),
    "description": ("description", "описание объекта закупки", "описание закупки", "дополнительная информация"),
    "published_at": ("published_at", "publishdate", "дата публикации", "дата размещения", "дата публикации извещения"),
}

def _norm(value: str) -> str:
    # "№" is a number sign, not a letter: keep it as the word "номер" so a
    # column named "№" does not normalize to an empty string.
    text = (value or "").lower().replace("ё", "е").replace("№", "номер")
    return re.sub(r"[^a-zа-яё0-9]+", "", text)

def _read_csv(payload: bytes) -> list[dict]:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Не удалось определить кодировку CSV (ожидается UTF-8 или Windows-1251)")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t,")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("В CSV не найдена строка заголовков")
    columns = {_norm(name): name for name in reader.fieldnames if name}
    mapping = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            if _norm(alias) in columns:
                mapping[field] = columns[_norm(alias)]
                break
    missing = [field for field in ("source_id", "title") if field not in mapping]
    if missing:
        raise ValueError("Не распознаны обязательные поля: " + ", ".join(missing) + ". Проверьте заголовки файла открытых данных.")
    rows = []
    for source in reader:
        row = {field: (source.get(column) or "").strip() for field, column in mapping.items()}
        regnum = row.get("source_id", "")
        if regnum:
            # Open-data exports often prefix the number with the sign itself.
            regnum = re.sub(r"^№\s*", "", regnum).strip()
            row["source_id"] = regnum
        if not row.get("url") and regnum:
            row["url"] = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=" + quote(regnum)
        rows.append(row)
    return rows

def _download(source: str) -> bytes:
    if source.lower().startswith(("http://", "https://")):
        # TLS: системное доверие, затем bundled CA (российские госсайты).
        # Проверка сертификатов не отключается ни в одном из контекстов.
        last_error = None
        for context in ssl_contexts():
            try:
                request = urllib.request.Request(source, headers={"User-Agent": "TenderMonitorMVP/0.2 (public open-data importer)"})
                with urllib.request.urlopen(request, timeout=45, context=context) as response:
                    payload = response.read(250 * 1024 * 1024 + 1)
                break
            except Exception as exc:  # noqa: BLE001 - пробуем следующий контекст
                last_error = exc
        else:
            raise last_error if last_error else RuntimeError("не удалось получить URL")
    else:
        payload = Path(source).read_bytes()
    if len(payload) > 250 * 1024 * 1024:
        raise ValueError("Файл больше допустимого лимита 250 МБ")
    if not payload:
        raise ValueError("Источник вернул пустой файл")
    return payload

def import_open_data(source: str) -> tuple[int, int, int]:
    """Load CSV or ZIP containing CSV from an official public resource URL or local file."""
    payload = _download(source)
    files: list[bytes]
    if zipfile.is_zipfile(io.BytesIO(payload)):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv") and not n.startswith(("__MACOSX/", "."))]
            files = [archive.read(name) for name in names]
    else:
        files = [payload]
    if not files:
        raise ValueError("В ZIP-архиве не найден CSV-файл")
    imported = skipped = parsed = 0
    for content in files:
        rows = _read_csv(content)
        added, updated, rejected = import_rows(rows)
        imported += added + updated
        skipped += rejected
        parsed += len(rows)
    return imported, skipped, parsed
