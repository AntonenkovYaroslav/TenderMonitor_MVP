import csv
import re
from pathlib import Path
from .db import session, init_db, MERGE_FIELDS
from .dedup import canonical_key, merge_values
from .classifier import classify
FIELDS = ["source_id","title","price","address","customer","deadline","contact_name","contact_phone","contact_email","url","region","description","published_at"]
# Extra columns filled by the EIS adapters; kept through normalization so the
# organizer, submission time and object address are not silently dropped.
EXTRA_FIELDS = ["organizer","deadline_time","object_address"]

def parse_price(value):
    """Число из строки вида «2 874 509,65 ₽».

    Учитывает обычные/неразрывные/узкие пробелы, символы и слова валют (₽, руб.,
    RUB и т.п. — просто отбрасываются вместе с нецифровым контекстом), запятую и
    точку как разделитель дробной части. Если разделителей несколько, дробным
    считается последний, остальные — разделители тысяч («1.234.567,89»).
    Одиночный разделитель с тремя цифрами после него трактуется как тысячи
    («1.234» -> 1234): цены ЕИС публикуются с двумя знаками после запятой.
    Возвращает float или None, если числа в строке нет.
    """
    text = str(value or "").replace(chr(0xA0), " ").replace(chr(0x202F), " ").replace(chr(0x2009), " ")
    match = re.search(r"\d[\d ]*(?:[.,]\d+)*", text)
    if not match:
        return None
    num = match.group(0).replace(" ", "")
    if "," in num and "." in num:
        cut = max(num.rfind(","), num.rfind("."))
        num = num[:cut].replace(",", "").replace(".", "") + "." + num[cut + 1:]
    else:
        sep = "," if "," in num else ("." if "." in num else "")
        if sep:
            parts = num.split(sep)
            if len(parts) > 2 or len(parts[-1]) == 3:
                num = "".join(parts)
            elif len(parts[-1]) > 3:
                return None
            else:
                num = ".".join(parts)
    try:
        return float(num)
    except ValueError:
        return None

def import_rows(rows):
    """Insert normalized rows; shared by local CSV and public-source adapters.

    Сквозная дедупликация: запись ищется по каноническому ключу (номер
    извещения / guid / fallback source_id+URL). Найденная запись обновляется
    слиянием полей — заполненные значения и заметка note сохраняются, пустые
    поля дозаполняются из нового источника. Returns (added, updated, skipped).
    """
    init_db()
    added = updated = skipped = 0
    with session() as con:
        for row in rows:
            r = {k: str(row.get(k) or "").strip() for k in FIELDS + EXTRA_FIELDS}
            if not r["source_id"] or not r["title"]:
                skipped += 1
                continue
            r["object_address"] = r["object_address"] or r["address"]
            r["price"] = parse_price(r["price"])  # float или None
            key = canonical_key(r["source_id"], r["url"])
            existing = con.execute(
                "SELECT * FROM tenders WHERE canonical_key=?", (key,)
            ).fetchone()
            if existing is not None:
                merged = merge_values(dict(existing), r, MERGE_FIELDS)
                c = classify(merged)
                merged.update(c)
                fields = MERGE_FIELDS + ("category", "score", "relevance", "region_check")
                assignments = ", ".join(f"{f}=?" for f in fields)
                con.execute(
                    f"UPDATE tenders SET {assignments} WHERE id=?",
                    [merged.get(f) for f in fields] + [existing["id"]],
                )
                updated += 1
            else:
                c = classify(r)
                fields = FIELDS + ["category", "score", "relevance", "region_check",
                                   "organizer", "deadline_time", "object_address", "canonical_key"]
                values = [r.get(f) for f in FIELDS]
                values += [c["category"], c["score"], c["relevance"], c["region_check"],
                           r["organizer"], r["deadline_time"], r["object_address"], key]
                columns = ",".join(fields)
                placeholders = ",".join("?" for _ in fields)
                con.execute(
                    f"INSERT INTO tenders ({columns}) VALUES ({placeholders})", values
                )
                added += 1
    return added, updated, skipped

def import_csv(filename):
    """Импорт локального CSV. Обязательны только колонки source_id и title;
    остальные поля необязательны и подставляются пустыми (как в открытых данных)."""
    with Path(filename).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [x for x in ("source_id", "title") if x not in (reader.fieldnames or [])]
        if missing: raise ValueError("Нет обязательных колонок: " + ", ".join(missing))
        return import_rows(reader)