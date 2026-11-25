"""Iteration 3 verification: counters, flexible CSV, ikz alias, scheduler, CLI/BAT regression."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # корень проекта; работает после clone/ZIP
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tendermonitor import db, exporter, importer, scheduler
from tendermonitor.sources import eis

TMP = Path(tempfile.mkdtemp(prefix="tm_iter3_"))
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

# --- 1. added/updated/skipped counters ---
row = {"source_id": "9990000000000000001", "title": "Ремонт дороги по ул. Ленина",
       "price": "1 000,50 " + chr(0x20BD), "url": "https://zakupki.gov.ru/x1",
       "region": "Калининградская область"}
check("counters first insert", importer.import_rows([row]) == (1, 0, 0))
check("counters reimport updates", importer.import_rows([row]) == (0, 1, 0))
row2 = dict(row, price="2 000,00")
check("counters changed price updates", importer.import_rows([row2]) == (0, 1, 0))
with db.session() as con:
    price = con.execute("SELECT price FROM tenders WHERE source_id=? AND url=?",
                        (row["source_id"], row["url"])).fetchone()[0]
check("updated price in db", price == 2000.0, price)
check("counters skipped invalid", importer.import_rows([{"source_id": "9992", "title": ""}]) == (0, 0, 1))
check("same number different url merges", importer.import_rows([dict(row, url="https://zakupki.gov.ru/x2")]) == (0, 1, 0))

# --- 2. flexible import_csv ---
minimal = TMP / "minimal.csv"
minimal.write_text("source_id,title\n9990000000000000010,Поставка песка\n", encoding="utf-8")
check("csv subset columns", importer.import_csv(str(minimal)) == (1, 0, 0))
no_title = TMP / "no_title.csv"
no_title.write_text("source_id,price\n9990000000000000011,100\n", encoding="utf-8")
try:
    importer.import_csv(str(no_title))
    check("csv missing title raises", False)
except ValueError as exc:
    check("csv missing title raises", "обязательных" in str(exc) and "title" in str(exc), str(exc))
extras = TMP / "extras.csv"
extras.write_text("source_id,title,organizer,deadline_time,object_address\n"
                  "9990000000000000012,Уборка снега,Комитет по закупкам,10:00,г. Калининград\n",
                  encoding="utf-8")
check("csv extras import", importer.import_csv(str(extras)) == (1, 0, 0))
with db.session() as con:
    r = con.execute("SELECT organizer, deadline_time, object_address FROM tenders WHERE source_id='9990000000000000012'").fetchone()
check("csv extras kept", tuple(r) == ("Комитет по закупкам", "10:00", "г. Калининград"), tuple(r))
check("full sample csv still works", sum(importer.import_csv(str(ROOT / "data" / "sample_tenders.csv"))[:2]) == 1)

# --- 3. eis.py: ikz must not replace purchase number ---
xml_ok = TMP / "notice.xml"
xml_ok.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<export>
  <purchaseNumber>0335300000226000695</purchaseNumber>
  <ikz>262390632283839060100102350014399633</ikz>
  <purchaseObjectInfo>Поставка песка для дорожных работ</purchaseObjectInfo>
  <maxPrice>100000.00</maxPrice>
  <customerFullName>ГБУЗ КО Больница</customerFullName>
  <applicationEndDate>2026-12-01</applicationEndDate>
</export>""", encoding="utf-8")
records, errors = eis.parse_package(xml_ok)
check("xml one record", len(records) == 1 and not errors, (len(records), errors))
check("source_id is purchase number not ikz", records[0]["source_id"] == "0335300000226000695", records[0]["source_id"])
saved, parsed, errors = eis.import_package(xml_ok)
check("xml import saved", (saved, parsed) == (1, 1), (saved, parsed))
xml_ikz_only = TMP / "ikz_only.xml"
xml_ikz_only.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<export><ikz>262390632283839060100102350014399633</ikz>
<purchaseObjectInfo>Поставка песка</purchaseObjectInfo></export>""", encoding="utf-8")
records, errors = eis.parse_package(xml_ikz_only)
check("ikz alone is not a number", not records and errors, (records, errors))

# --- 4. scheduler helpers (real clock) ---
check("parse times default", scheduler._parse_times(["08:00", "20:00"]) == scheduler.RUN_TIMES)
check("parse times empty fallback", scheduler._parse_times([]) == scheduler.RUN_TIMES)
check("parse times invalid fallback", scheduler._parse_times(["25:00", "abc"]) == scheduler.RUN_TIMES)
check("parse times sort dedup", scheduler._parse_times(["20:00", "08:00", "08:00"]) == (time(8, 0), time(20, 0)))
base = datetime.now().astimezone()
def at(day_offset, hour, minute=0):
    return (base + timedelta(days=day_offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
times = scheduler.RUN_TIMES
check("next run before 8", scheduler._next_run(at(0, 7), times) == at(0, 8))
check("next run between slots", scheduler._next_run(at(0, 9), times) == at(0, 20))
check("next run after 20", scheduler._next_run(at(0, 21), times) == at(1, 8))
check("missed none last night", scheduler._missed_slots(at(-1, 21), at(0, 7), times) == 0)
check("missed one this morning", scheduler._missed_slots(at(-1, 21), at(0, 9), times) == 1)
check("missed five over two days", scheduler._missed_slots(at(-2, 7), at(0, 9), times) == 5)
check("missed zero without state", scheduler._missed_slots(None, at(0, 9), times) == 0)
check("missed zero future last", scheduler._missed_slots(at(1, 9), at(0, 9), times) == 0)

# --- 5. state file roundtrip ---
scheduler.DATA = TMP / "sched"
scheduler.STATE_FILE = scheduler.DATA / "schedule_state.json"
moment = at(0, 8)
scheduler._write_last_run(moment)
check("state roundtrip", scheduler._read_last_run() == moment, scheduler._read_last_run())
scheduler.STATE_FILE.write_text("{broken", encoding="utf-8")
check("state corrupt tolerated", scheduler._read_last_run() is None)
scheduler.STATE_FILE.unlink()

# --- 6. run_schedule integration with fake clock ---
class FakeDateTime(datetime):
    _now = None
    @classmethod
    def now(cls, tz=None):
        return cls._now

def fake_sleep(seconds):
    FakeDateTime._now += timedelta(seconds=seconds)

def run_scenario(cfg, start, plan, state=None, kwargs=None, oversleep_first=False):
    scheduler.DATA = TMP / "sched"
    scheduler.STATE_FILE = scheduler.DATA / "schedule_state.json"
    scheduler.DATA.mkdir(parents=True, exist_ok=True)
    if scheduler.STATE_FILE.exists():
        scheduler.STATE_FILE.unlink()
    if state is not None:
        scheduler.STATE_FILE.write_text(json.dumps({"last_run": state.isoformat()}), encoding="utf-8")
    scheduler.settings = lambda: cfg
    scheduler.datetime = FakeDateTime
    state_sleep = {"first": True}
    def sleep(seconds):
        extra = 3600 if oversleep_first and state_sleep["first"] else 0
        state_sleep["first"] = False
        FakeDateTime._now += timedelta(seconds=seconds + extra)
    scheduler.sleep_time = types.SimpleNamespace(sleep=sleep)
    FakeDateTime._now = start
    calls = []
    def run_once():
        idx = len(calls)
        if idx >= len(plan):
            raise KeyboardInterrupt  # защита от бесконечного цикла при ошибке сценария
        action = plan[idx]
        if action == "stop":
            raise KeyboardInterrupt
        calls.append(FakeDateTime._now)
        if action == "fail":
            raise ValueError("boom")
        return (35, 4, "report.xlsx")
    try:
        scheduler.run_schedule(run_once, **(kwargs or {}))
    except KeyboardInterrupt:
        pass
    return calls

cfg_default = {"schedule": {}}
calls = run_scenario(cfg_default, at(0, 7), ["ok", "ok", "stop"])
check("start run immediate", calls[0] == at(0, 7), calls)
check("then planned slot 8", calls[1] == at(0, 8), calls)
check("state written after success", scheduler._read_last_run() == at(0, 8), scheduler._read_last_run())

cfg_no_start = {"schedule": {"run_at_start": False, "catch_up": True}}
calls = run_scenario(cfg_no_start, at(0, 9), ["ok", "ok", "stop"], state=at(-1, 21))
check("catch-up runs at start", calls[0] == at(0, 9), calls)
check("catch-up then slot 20", calls[1] == at(0, 20), calls)

cfg_off = {"schedule": {"run_at_start": False, "catch_up": False}}
calls = run_scenario(cfg_off, at(0, 7), ["ok", "stop"], state=at(-1, 21))
check("both off waits for slot", calls == [at(0, 8)], calls)

calls = run_scenario(cfg_no_start, at(0, 7), ["ok", "stop"])
check("no state no catch-up run", calls == [at(0, 8)], calls)

calls = run_scenario(cfg_off, at(0, 7, 59) + timedelta(seconds=50), ["ok", "stop"], oversleep_first=True)
check("overslept slot runs after wake", calls[0].hour == 9 and calls[0] > at(0, 8), calls)

scheduler.STATE_FILE.unlink(missing_ok=True)
calls = run_scenario(cfg_default, at(0, 7), ["fail", "ok", "stop"])
check("failed run does not stop loop", len(calls) == 2 and calls[1] == at(0, 8), calls)
check("failed run not marked success", scheduler._read_last_run() == at(0, 8), scheduler._read_last_run())

calls = run_scenario(cfg_default, at(0, 7), ["ok", "stop"], kwargs={"run_at_start": False, "catch_up": False})
check("cli flags override config", calls == [at(0, 8)], calls)

cfg_custom = {"schedule": {"times": ["09:30", "18:45"], "run_at_start": False, "catch_up": False}}
calls = run_scenario(cfg_custom, at(0, 7), ["ok", "ok", "stop"])
check("custom times from config", calls == [at(0, 9, 30), at(0, 18, 45)], calls)

real_cfg = json.loads((ROOT / "config" / "keywords.json").read_text(encoding="utf-8"))
check("config has schedule block", real_cfg.get("schedule") == {"times": ["08:00", "20:00"], "run_at_start": True, "catch_up": True}, real_cfg.get("schedule"))

# --- 7. CLI + BAT regression on the real repo (backup/restore) ---
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
backups = {}
for rel in ("data/tenders.db", "reports/tenders_report.xlsx", "last_run.log"):
    p = ROOT / rel
    if p.exists():
        backups[rel] = p.read_bytes()

def restore():
    for rel, payload in backups.items():
        (ROOT / rel).write_bytes(payload)

def cli(*args):
    return subprocess.run([PY, "-m", "tendermonitor.cli", *args], cwd=ROOT, env=ENV,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)

def bat(name, *args, cwd=ROOT):
    # ENV задаёт PYTHONIOENCODING=utf-8: без него python внутри .bat печатает
    # счётчики в cp1251, хотя сам .bat делает chcp 65001 для своих echo.
    return subprocess.run(["cmd", "/c", str(cwd / name), *args], cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=ENV, timeout=300)

try:
    r = cli("init")
    check("cli init", r.returncode == 0, r.stderr[-300:])
    r = cli("import-csv", r"data\sample_tenders.csv")
    check("cli import-csv new counters", r.returncode == 0 and "Добавлено:" in r.stdout and "обновлено:" in r.stdout, r.stdout + r.stderr[-300:])
    r = cli("import-eis-open-data", r"data\eis_open_data.csv")
    check("cli open data 342", r.returncode == 0 and "342" in r.stdout and "пропущено: 0" in r.stdout, r.stdout + r.stderr[-300:])
    r = cli("schedule-eis", "--help")
    check("cli schedule flags", "--no-run-at-start" in r.stdout and "--no-catch-up" in r.stdout, r.stdout)
    r = cli("export")
    check("cli export", r.returncode == 0 and "Excel создан" in r.stdout, r.stdout + r.stderr[-300:])

    test_csv = TMP / "bat_incoming.csv"
    test_csv.write_text("source_id,title,price\n9990000000000000099,Тестовая закупка итерации 3,12345.67\n", encoding="utf-8")
    r = bat("import_csv.bat", str(test_csv))
    check("bat import_csv real import", r.returncode == 0 and "Добавлено: 1" in r.stdout, r.stdout + r.stderr[-300:])
    r = bat("import_eis.bat", str(xml_ok))
    check("bat import_eis real import", r.returncode == 0 and "добавлено/обновлено: 1" in r.stdout, r.stdout + r.stderr[-300:])
    r = bat("demo.bat")
    check("bat demo", r.returncode == 0 and "Excel создан" in r.stdout, r.stdout + r.stderr[-300:])
    r = bat("export_excel.bat")
    check("bat export_excel", r.returncode == 0 and "Excel создан" in r.stdout, r.stdout + r.stderr[-300:])
    r = bat("import_eis.bat")
    check("bat import_eis no arg usage", r.returncode == 1 and "Перетащите" in r.stdout, r.stdout)
    r = bat("import_csv.bat")
    check("bat import_csv missing file message", r.returncode == 1 and "Файл не найден" in r.stdout, r.stdout)

    no_venv = TMP / "no_venv"
    no_venv.mkdir()
    for name in ("import_csv.bat", "import_eis.bat", "demo.bat", "export_excel.bat", "run_monitor.bat", "run_schedule.bat"):
        shutil.copyfile(ROOT / name, no_venv / name)
        raw = (ROOT / name).read_bytes()
        check(f"bat {name} crlf", b"\r\n" in raw and raw.replace(b"\r\n", b"").count(b"\n") == 0)
        check(f"bat {name} venv check", b".venv\\Scripts\\python.exe" in raw and "install.bat".encode("utf-8") in raw)
    r = bat("demo.bat", cwd=no_venv)
    check("bat without venv message", r.returncode == 1 and "install.bat" in r.stdout and ".venv" in r.stdout, r.stdout)
finally:
    restore()

print(f"\nALL {checks} CHECKS PASSED")
print("TMP:", TMP)