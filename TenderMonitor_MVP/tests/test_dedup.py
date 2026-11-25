# -*- coding: utf-8 -*-
"""Сквозная дедупликация и реестр источников: unit + интеграция на временной БД."""
import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]  # корень проекта; работает после clone/ZIP
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tendermonitor import db, exporter, importer, dedup
from tendermonitor.sources import eis, registry
from tendermonitor.sources import eis_browser as eb
from tendermonitor.config import settings

TMP = Path(tempfile.mkdtemp(prefix="tm_dedup_"))
db.DATA = TMP / "data"
db.DB = db.DATA / "tenders.db"
exporter.REPORTS = TMP / "reports"

checks = 0
def check(name, cond, extra=""):
    global checks
    checks += 1
    if not cond:
        print(f"FAIL {checks} {name}: {extra}")
        raise SystemExit(1)
    print(f"ok {checks} {name}")

N19 = "0335300000226000695"
N20 = "03353000002260006951"          # 20 знаков, новый формат
IKZ = "262390632283839060100102350014399633"  # 36 знаков
GUID = "550e8400-e29b-41d4-a716-446655440000"

# --- 1. canonical key: извлечение номера --------------------------------------
check("num url regNumber", dedup.number_from_url(
    f"https://zakupki.gov.ru/epz/order/notice/ea44/view/common-info.html?regNumber={N19}") == N19)
for param in ("purchaseNumber", "noticeNumber", "purchaseId", "noticeId"):
    check(f"num url {param}", dedup.number_from_url(f"https://x.ru/n?{param}={N19}") == N19)
check("num url searchString digits", dedup.number_from_url(
    f"https://zakupki.gov.ru/results.html?searchString={N19}") == N19)
check("num url searchString text rejected", dedup.number_from_url(
    "https://zakupki.gov.ru/results.html?searchString=ремонт+дороги") == "")
check("num url 20-digit", dedup.number_from_url(f"https://x.ru/n?regNumber={N20}") == N20)
check("num url with utm", dedup.number_from_url(
    f"https://zakupki.gov.ru/n?regNumber={N19}&utm_source=x") == N19)
check("num url none", dedup.number_from_url("https://zakupki.gov.ru/about") == "")
check("num sid plain", dedup.number_from_source_id(N19) == N19)
check("num sid sign+space", dedup.number_from_source_id(f"№ {N19}") == N19)
check("num sid trailing space", dedup.number_from_source_id(f"  {N19}  ") == N19)
check("num sid 20-digit", dedup.number_from_source_id(N20) == N20)
check("num sid IKZ rejected", dedup.number_from_source_id(IKZ) == "")
check("num sid short rejected", dedup.number_from_source_id("12345") == "")
check("num sid embedded", dedup.number_from_source_id(
    f"Извещение № {N19} от 01.01.2026") == N19)
check("num sid alpha rejected", dedup.number_from_source_id("ABC-123") == "")
check("guid url", dedup.guid_from_url(f"https://x.ru/n?guid={GUID}") == GUID)
check("guid sid", dedup.guid_from_source_id(GUID) == GUID)
check("guid url none", dedup.guid_from_url("https://x.ru/n") == "")

# --- 1b. normalize_url и canonical_key ----------------------------------------
check("norm url host/www/fragment", dedup.normalize_url(
    "https://WWW.Zakupki.gov.ru/path/?utm_source=a#frag") == "https://zakupki.gov.ru/path")
check("norm url sort params", dedup.normalize_url(
    "https://x.ru/p?b=2&a=1") == "https://x.ru/p?a=1&b=2")
check("norm url trailing slash", dedup.normalize_url("https://x.ru/p/") == "https://x.ru/p")
check("norm url empty", dedup.normalize_url("") == "")
check("key num priority", dedup.canonical_key(N19, f"https://x.ru/n?regNumber={N19}") == f"num:{N19}")
check("key url wins over IKZ sid", dedup.canonical_key(IKZ, f"https://x.ru/n?regNumber={N19}") == f"num:{N19}")
check("key same num diff url", dedup.canonical_key(N19, "https://a.ru/1") == dedup.canonical_key(N19, "https://b.ru/2"))
check("key diff num", dedup.canonical_key(N19, "") != dedup.canonical_key(N20, ""))
check("key guid fallback", dedup.canonical_key("REGION-1", f"https://x.ru/n?guid={GUID}") == f"guid:{GUID}")
check("key pair fallback", dedup.canonical_key("REGION-1", "https://x.ru/1").startswith("pair:REGION-1|"))
check("key IKZ pair not num", dedup.canonical_key(IKZ, "").startswith("pair:"))
mv = dedup.merge_values({"title": "старое", "customer": "Заказчик", "price": 5.0},
                        {"title": "новое", "customer": "", "price": None}, ("title", "customer", "price"))
check("merge nonempty wins", mv["title"] == "новое" and mv["customer"] == "Заказчик" and mv["price"] == 5.0, mv)

# --- 2. сквозное слияние разных источников ------------------------------------
db.init_db()
od_row = {"source_id": f"№ {N19}", "title": "Ремонт дороги", "price": "",
          "url": f"https://zakupki.gov.ru/results.html?searchString={N19}",
          "region": "Калининградская область", "customer": ""}
check("merge open-data first add", importer.import_rows([od_row]) == (1, 0, 0))
xml_row = {"source_id": N19, "title": "Ремонт дороги по ул. Ленина", "price": "100000",
           "url": f"https://zakupki.gov.ru/epz/order/notice/ea44/view/common-info.html?regNumber={N19}",
           "region": "Калининградская область", "customer": "ГБУЗ КО Больница", "deadline": "2026-12-01"}
check("merge xml updates", importer.import_rows([xml_row]) == (0, 1, 0))
csv_row = {"source_id": N19, "title": "Ремонт дороги по ул. Ленина", "price": "200000",
           "url": "https://zakupki.gov.ru/another-page", "region": "Калининградская область"}
check("merge csv updates", importer.import_rows([csv_row]) == (0, 1, 0))
with db.session() as con:
    rows = con.execute("SELECT * FROM tenders WHERE canonical_key=?", (f"num:{N19}",)).fetchall()
check("merge single row", len(rows) == 1, len(rows))
r = dict(rows[0])
check("merge customer kept", r["customer"] == "ГБУЗ КО Больница", r["customer"])
check("merge deadline kept", r["deadline"] == "2026-12-01", r["deadline"])
check("merge price updated", r["price"] == 200000.0, r["price"])
check("merge title latest", r["title"] == "Ремонт дороги по ул. Ленина", r["title"])
with db.session() as con:
    con.execute("UPDATE tenders SET note=? WHERE canonical_key=?", ("Заметка оператора", f"num:{N19}"))
importer.import_rows([dict(xml_row, price="300000")])
with db.session() as con:
    r2 = dict(con.execute("SELECT * FROM tenders WHERE canonical_key=?", (f"num:{N19}",)).fetchone())
check("merge note preserved", r2["note"] == "Заметка оператора", r2["note"])
check("merge price after note", r2["price"] == 300000.0, r2["price"])
check("merge first_seen kept", r2["first_seen"] == r["first_seen"], (r2["first_seen"], r["first_seen"]))
check("different number new row", importer.import_rows([dict(csv_row, source_id=N20)]) == (1, 0, 0))

# --- 3. fallback: не терять и не склеивать ------------------------------------
check("fallback add 1", importer.import_rows([{"source_id": "REGION-001", "title": "Уборка снега", "url": "http://x.ru/1"}]) == (1, 0, 0))
check("fallback diff url separate", importer.import_rows([{"source_id": "REGION-001", "title": "Уборка снега", "url": "http://x.ru/2"}]) == (1, 0, 0))
check("fallback diff sid separate", importer.import_rows([{"source_id": "REGION-002", "title": "Уборка снега", "url": "http://x.ru/1"}]) == (1, 0, 0))
check("fallback utm merges", importer.import_rows([{"source_id": "REGION-001", "title": "Уборка снега", "url": "http://x.ru/1?utm_source=a"}]) == (0, 1, 0))
check("fallback IKZ separate from num", importer.import_rows([{"source_id": IKZ, "title": "Поставка песка", "url": ""}]) == (1, 0, 0))

# --- 4. миграция: backfill + слияние легаси-дублей ----------------------------
with db.session() as con:
    con.execute("INSERT INTO tenders (source_id,title,url,region,customer,first_seen,canonical_key) VALUES (?,?,?,?,?,?,NULL)",
                (N19, "Ремонт дороги", "https://legacy.ru/1", "Калининградская область", "", "2026-01-01 00:00:00"))
    con.execute("INSERT INTO tenders (source_id,title,url,region,customer,note,first_seen,canonical_key) VALUES (?,?,?,?,?,?,?,NULL)",
                (N19, "Ремонт дороги", "https://legacy.ru/2", "Калининградская область", "Legacy Заказчик", "Легаси заметка", "2026-02-01 00:00:00"))
db.init_db()
with db.session() as con:
    total_n1 = con.execute("SELECT COUNT(*) c FROM tenders WHERE canonical_key=?", (f"num:{N19}",)).fetchone()["c"]
check("migration merges legacy dup", total_n1 == 1, total_n1)
with db.session() as con:
    merged = dict(con.execute("SELECT * FROM tenders WHERE canonical_key=?", (f"num:{N19}",)).fetchone())
check("migration note kept", merged["note"] in ("Заметка оператора", "Легаси заметка"), merged["note"])
check("migration customer filled", merged["customer"] != "", merged["customer"])
with db.session() as con:
    null_keys = con.execute("SELECT COUNT(*) c FROM tenders WHERE canonical_key IS NULL").fetchone()["c"]
check("migration backfill no null", null_keys == 0, null_keys)

# --- 5. экспорт и список без дублей -------------------------------------------
report, n = exporter.export()
with db.session() as con:
    distinct = con.execute("SELECT COUNT(DISTINCT canonical_key) c FROM tenders").fetchone()["c"]
check("export collapses by canonical", n == distinct, (n, distinct))
from tendermonitor.web import views
with db.session() as con:
    listed = con.execute("SELECT COUNT(*) c " + views.BASE_FROM).fetchone()["c"]
check("web list collapses by canonical", listed == distinct, (listed, distinct))

# --- 6. run_once: пре-дедуп по каноническому ключу ----------------------------
class FakePage:
    def close(self): pass
class FakeContext:
    def new_page(self): return FakePage()
class FakeBrowser:
    def new_context(self, **kw): return FakeContext()
    def close(self): pass
class FakePW:
    def __enter__(self): return object()
    def __exit__(self, *a): return False

def rec(sid, url, title="Ремонт дороги по ул. Ленина"):
    return {"source_id": sid, "title": title, "price": "100000", "address": "",
            "customer": "МУП «Дороги»", "organizer": "", "deadline": "31.12.2099",
            "deadline_time": "", "url": url, "region": "Калининградская область",
            "description": "", "published_at": "20.11.2026", "object_address": "",
            "contact_name": "", "contact_phone": "", "contact_email": ""}

N_RUN = "0123456789012345901"
N_NEW = "0123456789012345902"
importer.import_rows([rec(N_RUN, "https://zakupki.gov.ru/seed-url")])
RSS_STUB = [rec(N_RUN, "https://zakupki.gov.ru/different-url"), rec(N_NEW, "https://zakupki.gov.ru/new-url")]
def fake_rss(**kw): return list(RSS_STUB)
def fail_extract(*a, **kw): raise eb.PlaywrightError("stub")
stats = {}
with mock.patch.object(eb, "sync_playwright", lambda: FakePW()), \
     mock.patch.object(eb, "_launch_browser", lambda pw, headless=True: FakeBrowser()), \
     mock.patch.object(eb, "_extract_records", side_effect=fail_extract), \
     mock.patch.object(eb, "_read_rss_records", side_effect=fake_rss):
    raw, imported, _ = eb.run_once(region="Калининградская область", stats=stats)
check("run_once cross-source dedup imported", imported == 1, imported)
check("run_once already_seen counts canonical", stats.get("already_seen") == 1, stats)
check("run_once stats found", stats.get("found") == 2, stats)
with db.session() as con:
    seed_url = con.execute("SELECT url FROM tenders WHERE canonical_key=?", (f"num:{N_RUN}",)).fetchone()["url"]
check("run_once seen url unchanged", seed_url == "https://zakupki.gov.ru/seed-url", seed_url)

# --- 7. реестр источников -----------------------------------------------------
real_sources = registry.read_sources()
check("registry reads 4 sources", len(real_sources) == 4, len(real_sources))
kinds = {s["label"]: s["kind"] for s in real_sources}
check("registry ЕИС kind", kinds.get("ЕИС") == "eis", kinds)
check("registry gov39 kind", kinds.get("ГИС закупок Калининградской области") == "portal_unsupported", kinds)
check("registry data.gov kind", kinds.get("Портал открытых данных РФ") == "open_data_catalog", kinds)
check("registry rlw kind", kinds.get("Росжелдор (открытые данные)") == "open_data_catalog", kinds)
check("classify direct csv", registry.classify_source("https://x.ru/data.csv") == "open_data_file")
check("classify direct zip", registry.classify_source("https://x.ru/data.zip") == "open_data_file")
check("classify unknown", registry.classify_source("https://example.com/page") == "unknown")

tmp_src = TMP / "sources.txt"
tmp_src.write_text("# комментарий\n\nЕИС: https://zakupki.gov.ru/\nhttps://bare.ru/data.csv\n", encoding="utf-8")
parsed = registry.read_sources(tmp_src)
check("registry comments/blank skipped", len(parsed) == 2, parsed)
check("registry bare url label", parsed[1]["label"] == "bare.ru", parsed[1])
check("registry missing file empty", registry.read_sources(TMP / "nope.txt") == [])

# collect_all: изоляция ошибок и статусы
def fake_collect(src, **kw):
    if "boom" in src["url"]:
        raise RuntimeError("источник упал")
    return registry._result(src, "ok", found=3, imported=2, filtered=1)
test_sources = [
    {"label": "ЕИС", "url": "https://zakupki.gov.ru/", "kind": "eis"},
    {"label": "Упавший", "url": "https://boom.ru/", "kind": "unknown"},
    {"label": "Каталог", "url": "https://data.gov.ru/", "kind": "open_data_catalog"},
]
with mock.patch.object(registry, "collect_source", side_effect=fake_collect), \
     mock.patch.object(registry, "export", return_value=(TMP / "r.xlsx", 2)):
    results, rep = registry.collect_all(sources=test_sources)
check("collect_all all processed", len(results) == 3, len(results))
check("collect_all error isolated", results[1]["status"] == "error", results[1])
check("collect_all others ok", results[0]["status"] == "ok" and results[2]["status"] == "ok", results)

# _collect_catalog: прямой файл / SPA / набор-указатель / закупочный набор
from tendermonitor.sources import open_data
with mock.patch.object(registry, "_fetch", return_value=(200, "text/csv", b"source_id,title\n1,Test\n", "u")), \
     mock.patch.object(open_data, "import_open_data", return_value=(5, 0, 5)):
    res = registry._collect_catalog({"label": "c", "url": "https://x.ru/data.csv", "kind": "open_data_catalog"})
check("catalog direct file ok", res["status"] == "ok" and res["imported"] == 5, res)
with mock.patch.object(registry, "_fetch", return_value=(200, "text/html", b"<html><div id=app></div></html>", "u")):
    res = registry._collect_catalog({"label": "c", "url": "https://data.gov.ru/", "kind": "open_data_catalog"})
check("catalog SPA skipped", res["status"] == "skipped" and "SPA" in res["detail"], res)
html_index = b'<html><a href="https://rlw.gov.ru/opendata/list.csv">list</a></html>'
with mock.patch.object(registry, "_fetch", return_value=(200, "text/html", html_index, "u")), \
     mock.patch.object(open_data, "import_open_data", side_effect=ValueError("Не распознаны обязательные поля: source_id")):
    res = registry._collect_catalog({"label": "c", "url": "https://rlw.gov.ru/opendata", "kind": "open_data_catalog"})
check("catalog index skipped", res["status"] == "skipped" and "не закупки" in res["detail"], res)
with mock.patch.object(registry, "_fetch", return_value=(200, "text/html", html_index, "u")), \
     mock.patch.object(open_data, "import_open_data", return_value=(7, 1, 8)):
    res = registry._collect_catalog({"label": "c", "url": "https://rlw.gov.ru/opendata", "kind": "open_data_catalog"})
check("catalog tender dataset ok", res["status"] == "ok" and res["imported"] == 7, res)

# --- 7b. TLS-fallback и аккуратный пропуск не-закупочных файлов -----------------
import ssl
from tendermonitor import config

contexts = list(config.ssl_contexts())
check("ssl contexts exist", len(contexts) >= 1, len(contexts))
check("ssl verification never disabled", all(
    c.check_hostname and c.verify_mode == ssl.CERT_REQUIRED for c in contexts))

class _FakeResp:
    def __init__(self, payload): self._payload = payload
    def read(self, n=-1): return self._payload
    def __enter__(self): return self
    def __exit__(self, *a): return False

calls = []
def _urlopen_fail_then_ok(request, timeout=None, context=None):
    calls.append(context)
    if len(calls) == 1:
        raise ssl.SSLError("self-signed certificate in certificate chain")
    return _FakeResp(b"source_id,title\n1,Test\n")

with mock.patch.object(open_data.urllib.request, "urlopen", side_effect=_urlopen_fail_then_ok):
    payload = open_data._download("https://rlw.gov.ru/opendata/list.csv")
check("download TLS fallback payload", payload == b"source_id,title\n1,Test\n", payload)
check("download tried 2 contexts", len(calls) == 2, len(calls))
check("download contexts verify", all(
    c.check_hostname and c.verify_mode == ssl.CERT_REQUIRED for c in calls))

with mock.patch.object(open_data.urllib.request, "urlopen", side_effect=ssl.SSLError("self-signed")):
    try:
        open_data._download("https://rlw.gov.ru/opendata/list.csv")
        raised = None
    except Exception as exc:
        raised = exc
check("download all contexts fail raises", isinstance(raised, ssl.SSLError), raised)

with mock.patch.object(open_data, "import_open_data", side_effect=ValueError("Не распознаны обязательные поля: source_id")):
    res = registry._collect_open_data_file({"label": "f", "url": "https://rlw.gov.ru/opendata/list.csv", "kind": "open_data_file"})
check("direct file not tenders skipped", res["status"] == "skipped" and "не распознан" in res["detail"], res)

with mock.patch.object(registry, "_fetch", return_value=(200, "text/csv", b"property,title,value,format\n", "https://rlw.gov.ru/opendata/list.csv")), \
     mock.patch.object(open_data, "import_open_data", side_effect=ValueError("Не распознаны обязательные поля: source_id")):
    res = registry._collect_catalog({"label": "c", "url": "https://rlw.gov.ru/opendata", "kind": "open_data_catalog"})
check("catalog direct non-tender skipped", res["status"] == "skipped" and "не закупки" in res["detail"], res)

seen_args = []
def _capture_import(arg):
    seen_args.append(arg)
    return (1, 0, 1)
rel_html = b'<html><a href="/opendata/list.csv">list</a></html>'
with mock.patch.object(registry, "_fetch", return_value=(200, "text/html", rel_html, "https://rlw.gov.ru/opendata/index.html")), \
     mock.patch.object(open_data, "import_open_data", side_effect=_capture_import):
    res = registry._collect_catalog({"label": "c", "url": "https://rlw.gov.ru/opendata", "kind": "open_data_catalog"})
check("catalog relative href resolved", seen_args == ["https://rlw.gov.ru/opendata/list.csv"], seen_args)
check("catalog relative import ok", res["status"] == "ok" and res["imported"] == 1, res)

# --- 8. CLI collect-sources ---------------------------------------------------
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
cli_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
proc = subprocess.run([PY, "-m", "tendermonitor.cli", "collect-sources", "--help"],
                      cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=cli_env)
check("cli collect-sources help", proc.returncode == 0, proc.stderr)
check("cli collect-sources flags", all(f in (proc.stdout or "") for f in ("--region", "--pages", "--per-page", "--days")), proc.stdout)

print(f"\nALL {checks} CHECKS PASSED")
print("TMP:", TMP)