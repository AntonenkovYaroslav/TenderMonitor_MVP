import csv
import re
from pathlib import Path
from .db import session, init_db
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
    """Insert normalized rows; shared by local CSV and public-source adapters."""
    init_db()
    added = skipped = 0
    with session() as con:
        for row in rows:
            r = {k: str(row.get(k) or "").strip() for k in FIELDS + EXTRA_FIELDS}
            if not r["source_id"] or not r["title"]:
                skipped += 1
                continue
            price = parse_price(r["price"])
            c = classify(r)
            vals = [r[k] for k in FIELDS]
            vals[2] = price
            vals += [c["category"],c["score"],c["relevance"],c["region_check"]]
            vals += [r["organizer"], r["deadline_time"], r["object_address"] or r["address"]]
            con.execute("INSERT INTO tenders (source_id,title,price,address,customer,deadline,contact_name,contact_phone,contact_email,url,region,description,published_at,category,score,relevance,region_check,organizer,deadline_time,object_address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET title=excluded.title,price=excluded.price,address=excluded.address,customer=excluded.customer,deadline=excluded.deadline,contact_name=excluded.contact_name,contact_phone=excluded.contact_phone,contact_email=excluded.contact_email,region=excluded.region,description=excluded.description,published_at=excluded.published_at,category=excluded.category,score=excluded.score,relevance=excluded.relevance,region_check=excluded.region_check,organizer=excluded.organizer,deadline_time=excluded.deadline_time,object_address=excluded.object_address", vals)
            added += 1
    return added, skipped

def import_csv(filename):
    with Path(filename).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [x for x in FIELDS if x not in (reader.fieldnames or [])]
        if missing: raise ValueError("Нет колонок: " + ", ".join(missing))
        return import_rows(reader)