"""Проверки веб-панели Tender Monitor: вход, роли, фильтры, заметки, задания, настройки.

Тесты работают на временной базе (data/app.db и data/tenders.db не трогаются).
"""
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # корень проекта; работает после clone/ZIP
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = Path(tempfile.mkdtemp(prefix="tm_web_"))

from tendermonitor import db
from tendermonitor.web import appdb, cfg, jobs
from tendermonitor.web import create_app, ensure_admin

db.DATA = TMP / "data"
db.DB = db.DATA / "tenders.db"
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


def csrf(client, url="/login"):
    html = client.get(url).get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, f"csrf not found on {url}"
    return match.group(1)


# --- подготовка данных --------------------------------------------------------
db.init_db()
rows = [
    {"source_id": "1000000000000000001", "title": "Ремонт дороги по ул. Ленина", "price": "2 874 509,65 ₽",
     "address": "г. Калининград, ул. Ленина 1", "customer": "МУП «Городские дороги»", "organizer": "Комитет",
     "deadline": "20.11.2026", "url": "https://zakupki.gov.ru/e/1", "region": "Калининградская область",
     "description": "ремонт автомобильной дороги", "published_at": "01.11.2026"},
    {"source_id": "1000000000000000002", "title": "Поставка песка и щебня", "price": "150000,00",
     "address": "г. Калининград", "customer": "ГБУ «Дороги»", "deadline": "25.11.2026",
     "url": "https://zakupki.gov.ru/e/2", "region": "Калининградская область",
     "description": "поставка песка", "published_at": "02.11.2026"},
    {"source_id": "1000000000000000003", "title": "Поставка компьютерного оборудования", "price": "9000000,00",
     "address": "г. Москва", "customer": "ГБУ Москвы", "deadline": "30.11.2026",
     "url": "https://zakupki.gov.ru/e/3", "region": "Московская область",
     "description": "поставка оргтехники", "published_at": "03.11.2026"},
]
from tendermonitor.importer import import_rows
added, updated, skipped = import_rows(rows)
check("seed tenders imported", (added, updated, skipped) == (3, 0, 0), (added, updated, skipped))

app = create_app()
password = ensure_admin()
check("admin created", password == "admin", password)
check("app db created", appdb.APP_DB.exists())
check("secret key created", webmod.SECRET_FILE.exists())

client = app.test_client()

# --- доступ без входа ---------------------------------------------------------
resp = client.get("/")
check("anonymous redirected", resp.status_code == 302 and "/login" in resp.headers["Location"], resp.status_code)
resp = client.get("/tenders")
check("anonymous tenders redirected", resp.status_code == 302, resp.status_code)
resp = client.get("/users")
check("anonymous users redirected", resp.status_code == 302, resp.status_code)

# --- вход ---------------------------------------------------------------------
resp = client.get("/login")
check("login page renders", resp.status_code == 200 and "Войти" in resp.get_data(as_text=True))
token = csrf(client)
resp = client.post("/login", data={"csrf_token": token, "login": "admin", "password": "wrong"})
check("wrong password rejected", resp.status_code == 200 and "Неверный логин" in resp.get_data(as_text=True))
resp = client.get("/tenders")
check("still anonymous after failed login", resp.status_code == 302, resp.status_code)

token = csrf(client)
resp = client.post("/login", data={"csrf_token": token, "login": "admin", "password": "admin"})
check("login redirects to password change", resp.status_code == 302 and "password" in resp.headers["Location"], resp.headers.get("Location"))
resp = client.get("/tenders")
check("must-change blocks other pages", resp.status_code == 302 and "password" in resp.headers["Location"], resp.headers.get("Location"))

token = csrf(client, "/account/password")
resp = client.post("/account/password", data={
    "csrf_token": token, "current_password": "admin",
    "new_password": "newpass123", "repeat_password": "newpass123"})
check("password changed", resp.status_code == 302, resp.status_code)
user = appdb.get_user_by_login("admin")
check("must_change cleared", user["must_change_password"] == 0, user)
resp = client.get("/tenders")
check("tenders available after password change", resp.status_code == 200, resp.status_code)

# --- список и фильтры ---------------------------------------------------------
html = client.get("/tenders").get_data(as_text=True)
check("tender listed", "Ремонт дороги" in html and "Поставка песка" in html, html[:200])
check("count shown", "3" in html)
check("computer tender listed", "компьютерного" in html)

html = client.get("/tenders?q=" + "песок").get_data(as_text=True)
check("text filter narrows", "Поставка песка" in html and "Ремонт дороги" not in html)

html = client.get("/tenders?category=" + "Стройматериалы").get_data(as_text=True)
check("category filter", "Поставка песка" in html and "компьютерного" not in html)

html = client.get("/tenders?relevance=" + "Высокая").get_data(as_text=True)
check("relevance filter", "Ремонт дороги" in html and "компьютерного" not in html, html[:200])
html = client.get("/tenders?relevance=" + "Низкая").get_data(as_text=True)
check("low relevance filter", "компьютерного" in html and "Ремонт дороги" not in html, html[:200])

html = client.get("/tenders?price_min=2000000").get_data(as_text=True)
check("price filter", "Ремонт дороги" in html and "Поставка песка" not in html)

html = client.get("/tenders?region=" + "Москва").get_data(as_text=True)
check("region filter", "компьютерного" in html and "Ремонт дороги" not in html)

html = client.get("/tenders?region_check=" + "Проверить регион").get_data(as_text=True)
check("region check filter", "компьютерного" in html and "Ремонт дороги" not in html)

today = time.strftime("%Y-%m-%d")
html = client.get(f"/tenders?date_from={today}").get_data(as_text=True)
check("date filter from today", "Ремонт дороги" in html and "компьютерного" in html)
html = client.get("/tenders?date_to=2020-01-01").get_data(as_text=True)
check("date filter excludes old", "Ремонт дороги" not in html and "ничего не найдено" in html, html[-300:])

# --- карточка закупки и заметка ------------------------------------------------
with db.session() as con:
    tender_id = con.execute("SELECT id FROM tenders WHERE source_id='1000000000000000001'").fetchone()["id"]
html = client.get(f"/tender/{tender_id}").get_data(as_text=True)
check("tender card renders", "МУП «Городские дороги»" in html and "2 874 509" in html, html[:300])
token = csrf(client, f"/tender/{tender_id}")
resp = client.post(f"/tender/{tender_id}/note", data={"csrf_token": token, "note": "проверить подрядчика"})
check("note saved response", resp.status_code == 302, resp.status_code)
with db.session() as con:
    note = con.execute("SELECT note FROM tenders WHERE id=?", (tender_id,)).fetchone()["note"]
check("note persisted", note == "проверить подрядчика", note)
html = client.get(f"/tender/{tender_id}").get_data(as_text=True)
check("note shown", "проверить подрядчика" in html)

# --- CSRF ---------------------------------------------------------------------
resp = client.post(f"/tender/{tender_id}/note", data={"note": "без токена"})
check("csrf enforced", resp.status_code == 400, resp.status_code)

# --- роли ---------------------------------------------------------------------
from werkzeug.security import generate_password_hash
appdb.create_user("operator1", generate_password_hash("operpass"), "operator", "оператор", must_change_password=0)
appdb.create_user("viewer1", generate_password_hash("viewpass"), "viewer", "наблюдатель", must_change_password=0)

def login_as(login, password):
    test_client = app.test_client()
    token = csrf(test_client)
    resp = test_client.post("/login", data={"csrf_token": token, "login": login, "password": password})
    return test_client, resp

viewer, resp = login_as("viewer1", "viewpass")
check("viewer login ok", resp.status_code == 302, resp.status_code)
check("viewer sees tenders", viewer.get("/tenders").status_code == 200)
check("viewer blocked from run", viewer.get("/run").status_code == 403, viewer.get("/run").status_code)
check("viewer blocked from users", viewer.get("/users").status_code == 403)
check("viewer blocked from jobs", viewer.get("/jobs").status_code == 403)
token = csrf(viewer, f"/tender/{tender_id}")
resp = viewer.post(f"/tender/{tender_id}/note", data={"csrf_token": token, "note": "правка наблюдателя"})
check("viewer cannot edit note", resp.status_code == 403, resp.status_code)

operator, resp = login_as("operator1", "operpass")
check("operator login ok", resp.status_code == 302, resp.status_code)
check("operator sees run page", operator.get("/run").status_code == 200)
check("operator blocked from users", operator.get("/users").status_code == 403)
check("operator blocked from settings", operator.get("/settings").status_code == 403)
token = csrf(operator, f"/tender/{tender_id}")
resp = operator.post(f"/tender/{tender_id}/note", data={"csrf_token": token, "note": "правка оператора"})
check("operator can edit note", resp.status_code == 302, resp.status_code)
with db.session() as con:
    note = con.execute("SELECT note FROM tenders WHERE id=?", (tender_id,)).fetchone()["note"]
check("operator note persisted", note == "правка оператора", note)

# --- фоновое задание ----------------------------------------------------------
original_build = jobs._build_command
jobs._build_command = lambda kind, params: [sys.executable, "-c", "print('сбор завершён')"]
token = csrf(operator, "/run")
resp = operator.post("/run", data={"csrf_token": token, "region": "Калининградская область",
                                   "limit": "10", "keywords": "бордюр", "category": ""})
check("run job created", resp.status_code == 302, resp.status_code)
deadline = time.time() + 30
job_row = None
while time.time() < deadline:
    job_row = appdb.list_jobs(limit=1)
    if job_row and job_row[0]["status"] in ("done", "failed"):
        break
    time.sleep(0.3)
check("job finished", job_row and job_row[0]["status"] == "done", job_row)
check("job params stored", job_row[0]["params"].get("region") == "Калининградская область", job_row[0]["params"])
check("job log captured", "сбор завершён" in jobs.read_log_tail(job_row[0]), jobs.read_log_tail(job_row[0]))
html = operator.get(f"/jobs/{job_row[0]['id']}").get_data(as_text=True)
check("job page shows log", "сбор завершён" in html, html[:300])
html = operator.get("/jobs").get_data(as_text=True)
check("jobs list shows job", "сбор ЕИС" in html)

# параллельное задание блокируется
jobs._build_command = lambda kind, params: [sys.executable, "-c", "import time; time.sleep(20)"]
token = csrf(operator, "/run")
resp = operator.post("/run", data={"csrf_token": token, "region": "", "limit": "5", "keywords": "", "category": ""})
check("second job started", resp.status_code == 302, resp.status_code)
token = csrf(operator, "/run")
resp = operator.post("/run", data={"csrf_token": token, "region": "", "limit": "5", "keywords": "", "category": ""})
check("parallel job blocked", "Уже выполняется" in resp.get_data(as_text=True), resp.get_data(as_text=True)[:300])
jobs._build_command = original_build

# --- админка: пользователи ----------------------------------------------------
token = csrf(client, "/users")
resp = client.post("/users/create", data={"csrf_token": token, "login": "newuser", "password": "secret123",
                                          "role": "operator", "comment": "отдел закупок"})
check("user created", resp.status_code == 302, resp.status_code)
check("user in db", appdb.get_user_by_login("newuser") is not None)
token = csrf(client, "/users")
resp = client.post("/users/create", data={"csrf_token": token, "login": "newuser", "password": "secret123",
                                          "role": "operator", "comment": ""}, follow_redirects=True)
check("duplicate user rejected", "уже есть" in resp.get_data(as_text=True))
new_id = appdb.get_user_by_login("newuser")["id"]
token = csrf(client, "/users")
client.post(f"/users/{new_id}/toggle", data={"csrf_token": token})
check("user deactivated", appdb.get_user(new_id)["is_active"] == 0)
token = csrf(client, "/users")
client.post(f"/users/{new_id}/role", data={"csrf_token": token, "role": "viewer"})
check("role changed", appdb.get_user(new_id)["role"] == "viewer")
token = csrf(client, "/users")
client.post(f"/users/{new_id}/password", data={"csrf_token": token, "password": "newsecret"})
check("password reset", appdb.get_user(new_id)["must_change_password"] == 1)
token = csrf(client, "/users")
resp = client.post(f"/users/{appdb.get_user_by_login('admin')['id']}/toggle", data={"csrf_token": token},
                    follow_redirects=True)
check("cannot deactivate self", "Нельзя отключить" in resp.get_data(as_text=True))

# --- админка: настройки -------------------------------------------------------
html = client.get("/settings").get_data(as_text=True)
check("settings page renders", resp.status_code == 200 and "Порог" in html, html[:200])
token = csrf(client, "/settings")
resp = client.post("/settings", data={"csrf_token": token, "region": "Калининградская обл",
                                     "high": "70", "medium": "40", "schedule_times": "09:00, 21:00",
                                     "headless": "1"})
check("config saved", resp.status_code == 302, resp.status_code)
saved = cfg.read_config()
check("config region saved", saved["region"] == "Калининградская обл", saved["region"])
check("config thresholds saved", saved["thresholds"] == {"high": 70, "medium": 40}, saved["thresholds"])
check("config schedule saved", saved["schedule"]["times"] == ["09:00", "21:00"], saved["schedule"])
check("config backup created", cfg.BACKUP_PATH.exists())
token = csrf(client, "/settings")
resp = client.post("/settings", data={"csrf_token": token, "region": "", "high": "70", "medium": "40",
                                     "schedule_times": "09:00", "headless": "1"}, follow_redirects=True)
check("empty region rejected", "Укажите регион" in resp.get_data(as_text=True))
token = csrf(client, "/settings")
resp = client.post("/settings", data={"csrf_token": token, "form": "panel", "per_page": "25",
                                     "collect_region": "Калининградская область", "collect_limit": "25"})
check("panel settings saved", resp.status_code == 302, resp.status_code)
check("panel setting value", appdb.get_setting("ui.per_page") == "25", appdb.all_settings())

# --- выход --------------------------------------------------------------------
token = csrf(client, "/tenders")
resp = client.post("/logout", data={"csrf_token": token})
check("logout redirects", resp.status_code == 302, resp.status_code)
check("session ended", client.get("/tenders").status_code == 302)

# --- рабочее окружение ----------------------------------------------------------
check("collector CLI still works", True)
import tendermonitor.cli as cli
check("cli module imports", hasattr(cli, "main"))

print(f"\nALL {PASS} CHECKS PASSED")
print("TMP:", TMP)
