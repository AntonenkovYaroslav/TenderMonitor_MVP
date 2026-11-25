# -*- coding: utf-8 -*-
"""Проверки блока «глубина/сроки поиска + строительный фильтр сбора»:
search_settings, окно публикации, просрочка, многостраничный RSS (стаб),
гейт run_once, клamps формы запуска, проброс флагов в CLI, валидация search
в cfg.apply_form. Временные базы; реальные data/*.db не трогаются.
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]  # корень проекта; работает после clone/ZIP
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = Path(tempfile.mkdtemp(prefix="tm_build_"))

from tendermonitor import db, exporter
from tendermonitor.config import search_settings, settings
from tendermonitor.sources import eis_browser as eb
from tendermonitor.web import appdb, cfg, jobs
from tendermonitor.web import create_app, ensure_admin

db.DATA = TMP / "data"
db.DB = db.DATA / "tenders.db"
exporter.REPORTS = TMP / "reports"
appdb.APP_DB = TMP / "data" / "app.db"
jobs.JOB_LOGS = TMP / "logs"
cfg.CONFIG_PATH = TMP / "keywords.json"
cfg.BACKUP_PATH = TMP / "keywords.json.bak"
shutil.copy(ROOT / "config" / "keywords.json", cfg.CONFIG_PATH)

import tendermonitor.web as webmod
webmod.SECRET_FILE = TMP / "app_secret.key"
os.environ["TM_ADMIN_PASSWORD"] = "admin"

PASS = 0


def check(name, condition, detail=""):
    global PASS
    assert condition, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"ok {PASS:02d} {name}")


# --- 1. search_settings: defaults, клamps, округление к допустимым --------------
s = search_settings({})
check("defaults", s == {"records_per_page": 100, "max_pages": 5, "max_cards": 1000,
                        "published_days": 30, "skip_expired": True, "ca_bundle": ""}, s)
check("old config without search", search_settings({"region": "Калининградская область"}) == s)
check("per_page clamp 500->200", search_settings({"search": {"records_per_page": 500}})["records_per_page"] == 200)
check("per_page snap 37->50", search_settings({"search": {"records_per_page": 37}})["records_per_page"] == 50)
check("per_page snap 12->10", search_settings({"search": {"records_per_page": 12}})["records_per_page"] == 10)
check("per_page junk->default", search_settings({"search": {"records_per_page": "abc"}})["records_per_page"] == 100)
check("max_pages clamp", search_settings({"search": {"max_pages": 999}})["max_pages"] == 50)
check("max_pages min", search_settings({"search": {"max_pages": 0}})["max_pages"] == 1)
check("max_cards negative->0", search_settings({"search": {"max_cards": -5}})["max_cards"] == 0)
check("max_cards clamp", search_settings({"search": {"max_cards": 200000}})["max_cards"] == 100000)
check("days clamp", search_settings({"search": {"published_days": 99999}})["published_days"] == 3650)
check("days zero kept", search_settings({"search": {"published_days": 0}})["published_days"] == 0)
check("skip_expired false kept", search_settings({"search": {"skip_expired": False}})["skip_expired"] is False)
check("ca_bundle trimmed", search_settings({"search": {"ca_bundle": "  C:\\ca.pem  "}})["ca_bundle"] == "C:\\ca.pem")
real = search_settings()
check("project config search", (real["records_per_page"], real["max_pages"], real["max_cards"],
                                real["published_days"], real["skip_expired"]) == (100, 5, 1000, 30, True), real)
import ssl as _ssl
check("bundled ca exists", (ROOT / real["ca_bundle"]).exists(), real["ca_bundle"])
check("bundled ca loads via relative path", isinstance(eb._rss_ssl_context(real["ca_bundle"]), _ssl.SSLContext))

# --- 2. окно публикации, URL результатов, просрочка ----------------------------
check("window zero empty", eb._date_window(0) == {} and eb._date_window(-3) == {})
w = eb._date_window(30)
today = datetime.now()
check("window keys", set(w) == {"publishDateFrom", "publishDateTo"}, w)
check("window to today", w["publishDateTo"] == today.strftime("%d.%m.%Y"), w)
check("window from -30d", w["publishDateFrom"] == (today - timedelta(days=30)).strftime("%d.%m.%Y"), w)
check("window format", all(re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", v) for v in w.values()), w)
url = eb._results_url("Калининградская область", 100, 2, w)
check("results url params", "recordsPerPage=_100" in url and "pageNumber=2" in url
      and "publishDateFrom=" in url and "searchString=" in url, url)
check("results url no window", "publishDateFrom" not in eb._results_url("x", 50, 1, {}))
fixed = datetime(2026, 11, 22)
check("expired past", eb._is_expired("01.01.2020") is True)
check("expired future", eb._is_expired("31.12.2099") is False)
check("expired empty", eb._is_expired("") is False and eb._is_expired(None) is False)
check("expired junk", eb._is_expired("скоро") is False)
check("expired iso", eb._is_expired("2020-01-01") is True)
check("expired with time", eb._is_expired("01.01.2020 10:00") is True)
check("expired today not expired", eb._is_expired("22.11.2026", today=fixed) is False)
check("expired yesterday", eb._is_expired("21.11.2026", today=fixed) is True)

# --- 3. многостраничный RSS со стабом urlopen ----------------------------------
def rss_xml(ids):
    items = []
    for n in ids:
        sid = f"0123456789012345{n:03d}"
        items.append(f"""<item>
<title>Поставка песка №{n}</title>
<link>https://zakupki.gov.ru/epz/order/notice/ea44/view/common-info.html?regNumber={sid}</link>
<guid>{sid}</guid>
<description>НМЦК: 100000 руб.&lt;br/&gt;Заказчик: МУП «Дороги».&lt;br/&gt;Дата окончания подачи заявок: 31.12.2099 10:00</description>
<pubDate>Fri, 20 Nov 2026 08:00:00 +0300</pubDate>
</item>""")
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            + "".join(items) + "</channel></rss>").encode("utf-8")


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


PAGES = {1: rss_xml([1, 2]), 2: rss_xml([2, 3]), 3: rss_xml([4])}
seen_urls = []


def fake_urlopen(request, timeout=None, context=None):
    seen_urls.append(request.full_url)
    page = int(re.search(r"pageNumber=(\d+)", request.full_url).group(1))
    return FakeResponse(PAGES.get(page, rss_xml([])))


with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
    records = eb._read_rss_records(region="Калининградская область", per_page=2, max_pages=5, published_days=30)
check("rss multipage unique", [r["source_id"] for r in records]
      == ["0123456789012345001", "0123456789012345002", "0123456789012345003", "0123456789012345004"], records)
check("rss stops on short page", len(seen_urls) == 3, seen_urls)
check("rss url per_page", all("recordsPerPage=_2" in u for u in seen_urls), seen_urls)
check("rss url page numbers", [re.search(r"pageNumber=(\d+)", u).group(1) for u in seen_urls] == ["1", "2", "3"])
check("rss url date window", all("publishDateFrom=" in u and "publishDateTo=" in u for u in seen_urls))
check("rss price kept raw", records[0]["price"] == "100000 руб.", records[0]["price"])
check("rss deadline parsed", records[0]["deadline"] == "31.12.2099" and records[0]["deadline_time"] == "10:00", records[0])
check("rss region passed", records[0]["region"] == "Калининградская область")

seen_urls.clear()
with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
    capped = eb._read_rss_records(per_page=2, max_pages=5, published_days=30, max_cards=2)
check("rss max_cards cap", len(capped) == 2 and len(seen_urls) == 1, (len(capped), seen_urls))

seen_urls.clear()
with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
    nowindow = eb._read_rss_records(per_page=2, max_pages=1, published_days=0)
check("rss days=0 no window", "publishDateFrom" not in seen_urls[0], seen_urls)

with mock.patch("urllib.request.urlopen", return_value=FakeResponse(rss_xml([]))):
    check("rss empty page breaks", eb._read_rss_records(per_page=2, max_pages=5) == [])

dtd = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel></channel></rss>'
with mock.patch("urllib.request.urlopen", return_value=FakeResponse(dtd)):
    try:
        eb._read_rss_records(per_page=2, max_pages=1)
        check("rss DTD rejected", False, "no exception")
    except ValueError as exc:
        check("rss DTD rejected", "DTD" in str(exc), exc)

# наименование объекта из description (служебный <title> ленты ЕИС)
import xml.etree.ElementTree as ET
from tendermonitor.classifier import classify
item = ET.fromstring("""<item>
<title>Электронный аукцион №0123456789012345099</title>
<link>https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0123456789012345099</link>
<guid>0123456789012345099</guid>
<description>Найденный результат:&lt;br/&gt;Электронный аукцион № 0123456789012345099&lt;br/&gt;Наименование объекта закупки: Ремонт дороги по ул. Ленина&lt;br/&gt;Начальная цена контракта: 123456.78 Валюта: Российский рубль&lt;br/&gt;Наименование Заказчика: МУП «Дороги»</description>
</item>""")
parsed_item = eb._parse_rss_item(item, "Калининградская область")
check("rss object title from description", parsed_item["title"] == "Ремонт дороги по ул. Ленина", parsed_item["title"])
check("rss price label without junk", parsed_item["price"] == "123456.78 Валюта: Российский рубль", parsed_item["price"])
check("rss customer from description", parsed_item["customer"] == "МУП «Дороги»", parsed_item["customer"])
noise_row = {"region": "Калининградская область", "description": "", "address": "", "customer": ""}
c = classify(dict(noise_row, title="Поставка реагентов для клинико-диагностической лаборатории"))
check("medical reagents zeroed", c["score"] == 0 and c["relevance"] == "Низкая", c)
c = classify(dict(noise_row, title="Реагентов для анализатора MAGLUMI"))
check("analyzer reagents zeroed", c["score"] == 0 and c["relevance"] == "Низкая", c)
c = classify(dict(noise_row, title="на поставку лекарственных препаратов для медицинского применения (МНН: КАБАЗИТАКСЕЛ)"))
check("pharma МНН zeroed", c["score"] == 0 and c["relevance"] == "Низкая", c)
c = classify(dict(noise_row, title="Поставка противогололёдных реагентов для посыпки дорог"))
check("winter reagents kept", c["score"] >= 35 and c["category"] == "Зимнее содержание дорог", c)

# --- 4. run_once: гейт профиля, просрочка, дедуп, проброс параметров -----------
class FakePage:
    def close(self):
        pass


class FakeContext:
    def new_page(self):
        return FakePage()


class FakeBrowser:
    def new_context(self, **kw):
        return FakeContext()

    def close(self):
        pass


class FakePW:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def rec(sid, title, region="Калининградская область", deadline="31.12.2099"):
    return {"source_id": sid, "title": title, "price": "100000", "address": "",
            "customer": "МУП «Дороги»", "organizer": "", "deadline": deadline,
            "deadline_time": "", "url": f"https://zakupki.gov.ru/e/{sid}", "region": region,
            "description": "", "published_at": "20.11.2026", "object_address": "",
            "contact_name": "", "contact_phone": "", "contact_email": ""}


RSS_STUB = [
    rec("0123456789012345101", "Ремонт дороги по ул. Ленина"),          # высокая -> kept
    rec("0123456789012345102", "Поставка мебели для офиса"),            # стоп-шум -> filtered
    rec("0123456789012345103", "Ремонт дороги", deadline="01.01.2020"),  # просрочка -> expired
    rec("0123456789012345104", "Ремонт дороги", region="Московская область"),  # чужой регион
    rec("0123456789012345105", "Озеленение территории сквера"),         # средняя 50 -> kept
    rec("0123456789012345101", "Ремонт дороги по ул. Ленина"),          # дубль внутри запуска
]
captured = {}


def fake_rss(**kw):
    captured.update(kw)
    return list(RSS_STUB)


def fail_extract(*a, **kw):
    raise eb.PlaywrightError("stub: browser path disabled")


db.init_db()
with mock.patch.object(eb, "sync_playwright", lambda: FakePW()), \
     mock.patch.object(eb, "_launch_browser", lambda pw, headless=True: FakeBrowser()), \
     mock.patch.object(eb, "_extract_records", side_effect=fail_extract), \
     mock.patch.object(eb, "_read_rss_records", side_effect=fake_rss):
    raw, imported, report = eb.run_once(region="Калининградская область", pages=7, per_page=20, days=14)
check("run_once raw count", raw == 6, raw)
check("run_once gate imported", imported == 2, imported)
check("run_once params passthrough", (captured["per_page"], captured["max_pages"], captured["published_days"]) == (20, 7, 14), captured)
check("run_once cap from config", captured["max_cards"] == 1000, captured)
with db.session() as con:
    kept = {r["source_id"] for r in con.execute("SELECT source_id FROM tenders")}
check("run_once kept ids", kept == {"0123456789012345101", "0123456789012345105"}, kept)
check("run_once report exists", Path(report).exists(), report)

# повторный запуск: те же записи уже видены -> imported 0
with mock.patch.object(eb, "sync_playwright", lambda: FakePW()), \
     mock.patch.object(eb, "_launch_browser", lambda pw, headless=True: FakeBrowser()), \
     mock.patch.object(eb, "_extract_records", side_effect=fail_extract), \
     mock.patch.object(eb, "_read_rss_records", side_effect=fake_rss):
    raw2, imported2, _ = eb.run_once(region="Калининградская область")
check("run_once dedup unseen", raw2 == 6 and imported2 == 0, (raw2, imported2))

# limit=1 обрезает сырьё до гейта; days=0 пробрасывается
with mock.patch.object(eb, "sync_playwright", lambda: FakePW()), \
     mock.patch.object(eb, "_launch_browser", lambda pw, headless=True: FakeBrowser()), \
     mock.patch.object(eb, "_extract_records", side_effect=fail_extract), \
     mock.patch.object(eb, "_read_rss_records", side_effect=fake_rss):
    raw3, imported3, _ = eb.run_once(region="Калининградская область", limit=1, days=0)
check("run_once limit cap", raw3 == 1 and imported3 == 0, (raw3, imported3))  # id101 уже в базе
check("run_once days=0 passthrough", captured["published_days"] == 0, captured)

# skip_expired=False: просроченная запись проходит гейт
cfg_no_skip = copy.deepcopy(settings())
cfg_no_skip["search"]["skip_expired"] = False
with mock.patch.object(eb, "sync_playwright", lambda: FakePW()), \
     mock.patch.object(eb, "_launch_browser", lambda pw, headless=True: FakeBrowser()), \
     mock.patch.object(eb, "_extract_records", side_effect=fail_extract), \
     mock.patch.object(eb, "_read_rss_records", side_effect=fake_rss), \
     mock.patch.object(eb, "settings", lambda: cfg_no_skip):
    raw4, imported4, _ = eb.run_once(region="Калининградская область")
check("run_once skip_expired off", imported4 == 1, imported4)  # id103 просрочен, но профильный
with db.session() as con:
    check("expired row imported when off", con.execute(
        "SELECT COUNT(*) c FROM tenders WHERE source_id='0123456789012345103'").fetchone()["c"] == 1)

# --- 5. jobs._build_command: проброс флагов, days=0 не теряется ----------------
cmd = jobs._build_command(jobs.COLLECT_KIND, {"region": "Калининградская область", "limit": 300,
                                              "pages": 7, "per_page": 20, "days": 0})
check("cmd flags", cmd[cmd.index("monitor-eis") + 1:] == ["--region", "Калининградская область",
      "--limit", "300", "--pages", "7", "--per-page", "20", "--days", "0"], cmd)
cmd2 = jobs._build_command(jobs.COLLECT_KIND, {"limit": 50})
check("cmd no depth flags by default", "--pages" not in cmd2 and "--per-page" not in cmd2 and "--days" not in cmd2, cmd2)
cmd3 = jobs._build_command(jobs.COLLECT_KIND, {"days": 14})
check("cmd days only", cmd3[cmd3.index("--days") + 1] == "14", cmd3)

# --- 6. веб-панель: клamps формы запуска и рендер новых полей ------------------
app = create_app()
ensure_admin()
client = app.test_client()


def csrf(c, url="/login"):
    html = c.get(url).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"csrf not found on {url}"
    return m.group(1)


token = csrf(client)
client.post("/login", data={"csrf_token": token, "login": "admin", "password": "admin"})
token = csrf(client, "/account/password")
client.post("/account/password", data={"csrf_token": token, "current_password": "admin",
                                       "new_password": "panel123", "repeat_password": "panel123"})

started = []


def fake_start(kind, params, login):
    started.append((kind, dict(params), login))
    return len(started), None


html = client.get("/run").get_data(as_text=True)
check("run page new fields", 'name="pages"' in html and 'name="per_page"' in html and 'name="days"' in html)
check("run limit max 5000", 'name="limit"' in html and 'max="5000"' in html)
check("run search hints", "100" in html and "30" in html)

with mock.patch.object(jobs, "start_job", side_effect=fake_start):
    token = csrf(client, "/run")
    resp = client.post("/run", data={"csrf_token": token, "region": "Калининградская область",
                                     "limit": "99999", "pages": "7", "per_page": "20", "days": "0"})
check("run POST redirect", resp.status_code == 302, resp.status_code)
kind, params, login = started[-1]
check("run limit clamped to max_cards", params["limit"] == 1000, params)
check("run depth params", (params.get("pages"), params.get("per_page"), params.get("days")) == (7, 20, 0), params)

with mock.patch.object(jobs, "start_job", side_effect=fake_start):
    token = csrf(client, "/run")
    client.post("/run", data={"csrf_token": token, "region": "", "limit": "abc"})
check("run limit junk default", started[-1][1]["limit"] == 300, started[-1][1])
check("run empty days omitted", "days" not in started[-1][1] and "pages" not in started[-1][1], started[-1][1])

with mock.patch.object(jobs, "start_job", side_effect=fake_start):
    token = csrf(client, "/run")
    client.post("/run", data={"csrf_token": token, "region": "", "limit": "0"})
check("run limit zero -> 1", started[-1][1]["limit"] == 1, started[-1][1])

# --- 7. cfg.apply_form: валидация блока search ---------------------------------
base = cfg.read_config()
form = {"region": "Калининградская область", "high": "65", "medium": "35",
        "schedule_times": "08:00, 20:00", "headless": "1",
        "search_per_page": "37", "search_max_pages": "7", "search_max_cards": "2000",
        "search_published_days": "60", "search_ca_bundle": "  C:\\certs\\ca.pem  "}
result, errors = cfg.apply_form(base, form)
check("apply_form no errors", errors == [], errors)
check("apply_form per_page snapped", result["search"]["records_per_page"] == 50, result["search"])
check("apply_form search values", (result["search"]["max_pages"], result["search"]["max_cards"],
                                   result["search"]["published_days"]) == (7, 2000, 60), result["search"])
check("apply_form skip_expired unchecked", result["search"]["skip_expired"] is False)
check("apply_form ca_bundle trimmed", result["search"]["ca_bundle"] == "C:\\certs\\ca.pem")

form_bad = dict(form, search_max_pages="99", search_per_page="500", search_max_cards="-1",
                search_published_days="99999")
result2, errors2 = cfg.apply_form(base, form_bad)
check("apply_form range errors", len(errors2) == 4, errors2)
check("apply_form keeps old on error", result2["search"]["max_pages"] == base["search"]["max_pages"], result2["search"])

form_junk = dict(form, search_max_pages="abc")
_, errors3 = cfg.apply_form(base, form_junk)
check("apply_form int error", any("целое" in e for e in errors3), errors3)

form_empty = {"region": "Калининградская область", "high": "65", "medium": "35",
              "schedule_times": "08:00", "search_per_page": "", "search_max_pages": "",
              "search_max_cards": "", "search_published_days": ""}
old_cfg = {k: v for k, v in base.items() if k != "search"}
result4, errors4 = cfg.apply_form(old_cfg, form_empty)
check("apply_form empty -> defaults", errors4 == [] and result4["search"]["records_per_page"] == 100
      and result4["search"]["max_pages"] == 5 and result4["search"]["max_cards"] == 1000
      and result4["search"]["published_days"] == 30, (errors4, result4.get("search")))

# сохранение через панель: POST /settings с полями search
token = csrf(client, "/settings")
resp = client.post("/settings", data={"csrf_token": token, "region": "Калининградская область",
                                      "high": "65", "medium": "35", "schedule_times": "08:00, 20:00",
                                      "headless": "1", "search_per_page": "200", "search_max_pages": "10",
                                      "search_max_cards": "3000", "search_published_days": "90",
                                      "search_skip_expired": "1", "search_ca_bundle": ""})
check("settings POST redirect", resp.status_code == 302, resp.status_code)
saved = cfg.read_config()
check("settings search saved", (saved["search"]["records_per_page"], saved["search"]["max_pages"],
                                saved["search"]["max_cards"], saved["search"]["published_days"],
                                saved["search"]["skip_expired"]) == (200, 10, 3000, 90, True), saved["search"])
check("settings backup created", cfg.BACKUP_PATH.exists())
html = client.get("/settings").get_data(as_text=True)
check("settings page search fields", 'name="search_per_page"' in html and 'name="search_max_pages"' in html
      and 'name="search_published_days"' in html and 'name="search_ca_bundle"' in html)
check("settings stop card", "Стоп-фразы и веса оценки" in html and "набор реагентов" in html)

# --- 8. CLI: новые флаги на месте ----------------------------------------------
# Дочерний процесс печатает справку в кодировке консоли (cp1251), если не задать
# PYTHONIOENCODING/PYTHONUTF8, — родитель с encoding="utf-8" получал бы кракозябры.
cli_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
proc = subprocess.run([sys.executable, "-m", "tendermonitor.cli", "monitor-eis", "--help"],
                      cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=cli_env)
check("cli help ok", proc.returncode == 0, proc.stderr)
check("cli new flags", all(f in (proc.stdout or "") for f in ("--pages", "--per-page", "--days")), proc.stdout)

print(f"\nALL {PASS} CHECKS PASSED")