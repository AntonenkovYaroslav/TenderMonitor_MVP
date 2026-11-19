import csv
from pathlib import Path
from .db import connect, init_db
from .classifier import classify
FIELDS = ["source_id","title","price","address","customer","deadline","contact_name","contact_phone","contact_email","url","region","description","published_at"]

def import_rows(rows):
    """Insert normalized rows; shared by local CSV and public-source adapters."""
    init_db()
    added = skipped = 0
    with connect() as con:
        for r in rows:
            r = {k: str(r.get(k) or "").strip() for k in FIELDS}
            if not r["source_id"] or not r["title"]:
                skipped += 1
                continue
            try: price = float(r["price"].replace("\u00a0", "").replace(" ", "").replace(",", ".")) if r["price"] else None
            except ValueError: price = None
            c = classify(r)
            vals = [r[k] for k in FIELDS]
            vals[2] = price
            vals += [c["category"],c["score"],c["relevance"],c["region_check"]]
            vals += [str(r.get("organizer") or "").strip(), str(r.get("deadline_time") or "").strip(), str(r.get("object_address") or r.get("address") or "").strip()]
            con.execute("INSERT INTO tenders (source_id,title,price,address,customer,deadline,contact_name,contact_phone,contact_email,url,region,description,published_at,category,score,relevance,region_check,organizer,deadline_time,object_address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET title=excluded.title,price=excluded.price,address=excluded.address,customer=excluded.customer,deadline=excluded.deadline,contact_name=excluded.contact_name,contact_phone=excluded.contact_phone,contact_email=excluded.contact_email,region=excluded.region,description=excluded.description,published_at=excluded.published_at,category=excluded.category,score=excluded.score,relevance=excluded.relevance,region_check=excluded.region_check,organizer=excluded.organizer,deadline_time=excluded.deadline_time,object_address=excluded.object_address", vals)
            added += 1
    return added, skipped

def import_csv(filename):
    with Path(filename).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [x for x in FIELDS if x not in (reader.fieldnames or [])]
        if missing: raise ValueError("Нет колонок: " + ", ".join(missing))
        return import_rows(reader)
