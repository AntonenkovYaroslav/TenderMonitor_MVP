"""
EIS XML/ZIP adapter for Tender Monitor.

Imports XML files or ZIP packages downloaded through an authorized EIS channel.
This module does NOT make live requests to undocumented EIS endpoints.

EIS XML schemas differ by procurement law and document type. Field extraction
is best-effort and must be validated against a real official export before
relying on it for operational decisions.
"""
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

from ..classifier import classify
from ..db import connect, init_db

ALIASES = {
    "source_id": {"purchasenumber", "regnumber", "registrationnumber", "purchaseid", "noticeid", "ikz"},
    "title": {"purchaseobjectinfo", "purchaseobjectname", "objectinfo", "purchasename", "noticename", "docname", "lotname"},
    "price": {"maxprice", "initialmaxprice", "initialmaximumprice", "nmck", "nmckprice", "price", "maxcontractprice"},
    "address": {"deliveryplace", "placeofdelivery", "placeofwork", "workplace", "executionplace", "address", "workaddress"},
    "customer": {"customerfullname", "customername", "fullcustomername", "customerorganizationname", "customer"},
    "organizer": {"organizerfullname", "organizername", "placingorganization", "placingorganizationname", "organizationplacing", "authorizedorganization"},
    "deadline": {"applicationenddate", "enddate", "submissionenddate", "applicationsenddate", "deadline", "admissionenddate"},
    "deadline_time": {"applicationendtime", "submissionendtime", "applicationsendtime", "deadline time", "endtime"},
    "contact_name": {"contactperson", "contactpersonname", "responsibleperson", "contactfio", "fio"},
    "contact_phone": {"contactphone", "phone", "phonenumber", "telephone"},
    "contact_email": {"contactemail", "email", "emailaddress"},
    "url": {"url", "href", "noticeurl", "purchaseurl", "documenturl"},
    "region": {"regionname", "region", "subjectname", "deliveryregion"},
    "description": {"purchaseobjectinfo", "objectinfo", "description", "lotname", "purchasename"},
    "published_at": {"docpublishdate", "publishdate", "publicationdate", "placedate", "publishedat"},
}

def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()

def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def _values(root: ET.Element) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for node in root.iter():
        name = _local_name(node.tag)
        value = _clean(" ".join(node.itertext()))
        if name and value:
            values.setdefault(name, [])
            if value not in values[name]:
                values[name].append(value)
    return values

def _first(values: dict[str, list[str]], aliases: set[str]) -> str:
    candidates = []
    for key, vals in values.items():
        if key in aliases:
            candidates.extend(vals)
    return max(candidates, key=len) if candidates else ""

def _normalize_record(root: ET.Element, source_file: str) -> dict:
    vals = _values(root)
    row = {field: _first(vals, aliases) for field, aliases in ALIASES.items()}
    row["source_id"] = row.get("source_id", "")
    row["title"] = row.get("title") or row.get("description") or ""
    row["description"] = row.get("description") or row["title"]
    row["url"] = row.get("url", "")
    if not row["url"].startswith(("http://", "https://")):
        row["url"] = ""

    # If the XML only has one organization field, do not silently label it
    # as both customer and organizer.
    row["object_address"] = row.get("address", "")
    row["_source_file"] = source_file

    raw_price = row.get("price", "")
    if raw_price:
        cleaned = raw_price.replace("\xa0", "").replace(" ", "").replace(",", ".")
        match = re.search(r"\d+(?:\.\d+)?", cleaned)
        row["price"] = match.group(0) if match else ""
    return row

def iter_xml_files(path: str | Path) -> Iterable[tuple[str, bytes]]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".xml":
        yield path.name, path.read_bytes()
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".xml") and not name.startswith("__MACOSX/"):
                    yield name, archive.read(name)
    else:
        raise ValueError("Поддерживаются файлы .xml и .zip")

def parse_package(path: str | Path) -> tuple[list[dict], list[str]]:
    records, errors = [], []
    for name, payload in iter_xml_files(path):
        try:
            root = ET.fromstring(payload)
            row = _normalize_record(root, name)
            if not row.get("source_id") or not row.get("title"):
                errors.append(f"{name}: не удалось определить номер закупки и/или название")
                continue
            records.append(row)
        except ET.ParseError as exc:
            errors.append(f"{name}: ошибка XML: {exc}")
    return records, errors

def import_package(path: str | Path) -> tuple[int, int, list[str]]:
    init_db()
    records, errors = parse_package(path)
    saved = 0
    with connect() as con:
        for row in records:
            row.pop("_source_file", None)
            classified = classify(row)
            try:
                price = float(row["price"]) if row.get("price") else None
            except ValueError:
                price = None

            values = (
                row.get("source_id", ""), row.get("title", ""), price,
                row.get("address", ""), row.get("customer", ""),
                row.get("deadline", ""), row.get("contact_name", ""),
                row.get("contact_phone", ""), row.get("contact_email", ""),
                row.get("url", ""), row.get("region", ""),
                row.get("description", ""), row.get("published_at", ""),
                classified["category"], classified["score"],
                classified["relevance"], classified["region_check"],
                row.get("organizer", ""), row.get("deadline_time", ""),
                row.get("object_address", row.get("address", ""))
            )
            con.execute("""
                INSERT INTO tenders (
                    source_id,title,price,address,customer,deadline,contact_name,
                    contact_phone,contact_email,url,region,description,published_at,
                    category,score,relevance,region_check,organizer,deadline_time,object_address
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id,url) DO UPDATE SET
                    title=excluded.title, price=excluded.price,
                    address=excluded.address, customer=excluded.customer,
                    deadline=excluded.deadline, contact_name=excluded.contact_name,
                    contact_phone=excluded.contact_phone, contact_email=excluded.contact_email,
                    region=excluded.region, description=excluded.description,
                    published_at=excluded.published_at, category=excluded.category,
                    score=excluded.score, relevance=excluded.relevance,
                    region_check=excluded.region_check, organizer=excluded.organizer,
                    deadline_time=excluded.deadline_time, object_address=excluded.object_address
            """, values)
            saved += 1
    return saved, len(records), errors

def inspect_package(path: str | Path) -> tuple[int, list[str], list[dict]]:
    records, errors = parse_package(path)
    preview = []
    for row in records[:10]:
        preview.append({
            "номер": row.get("source_id"),
            "название": row.get("title"),
            "НМЦК": row.get("price"),
            "заказчик": row.get("customer"),
            "организатор": row.get("organizer"),
            "адрес": row.get("object_address"),
            "регион": row.get("region"),
            "дата окончания": row.get("deadline"),
            "время окончания": row.get("deadline_time"),
            "ссылка": row.get("url"),
        })
    return len(records), errors, preview

def main():
    parser = argparse.ArgumentParser(description="Tender Monitor: EIS XML/ZIP adapter")
    sub = parser.add_subparsers(dest="action", required=True)
    inspect_cmd = sub.add_parser("inspect", help="Проверить XML/ZIP без импорта")
    inspect_cmd.add_argument("file")
    import_cmd = sub.add_parser("import", help="Импортировать XML/ZIP в базу")
    import_cmd.add_argument("file")
    args = parser.parse_args()
    if args.action == "inspect":
        count, errors, preview = inspect_package(args.file)
        print(f"Распознано документов: {count}")
        for item in preview:
            print("-" * 50)
            for key, value in item.items():
                print(f"{key}: {value or '—'}")
        for error in errors:
            print("ПРЕДУПРЕЖДЕНИЕ:", error)
    else:
        saved, parsed, errors = import_package(args.file)
        print(f"Обработано XML: {parsed}; добавлено/обновлено: {saved}; ошибок: {len(errors)}")
        for error in errors[:30]:
            print("ПРЕДУПРЕЖДЕНИЕ:", error)

if __name__ == "__main__":
    main()
