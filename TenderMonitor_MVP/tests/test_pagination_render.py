# -*- coding: utf-8 -*-
"""Проверки списка закупок: карточки, скрытие пустых полей, пояснение
релевантности, пагинация и форматирование. Временная база, реальные
data/app.db и data/tenders.db не трогаются.
"""
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # корень проекта; работает после clone/ZIP
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = Path(tempfile.mkdtemp(prefix="tm_cards_"))

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


def card_of(html, tender_id):
    """Блок карточки закупки по ссылке на детальную страницу."""
    blocks = html.split('<article class="tcard">')
    for block in blocks[1:]:
        if f'/tender/{tender_id}"' in block.split("</article>")[0]:
            return block.split("</article>")[0]
    return ""


# --- данные: 120 закупок, включая полностью пустые и проблемные ---------------
db.init_db()
LONG_TITLE = "Доработка программных средств ЕГАИС в целях цифровой трансформации " + "очень длинное слово " * 60
rows = []
for i in range(1, 121):
    rows.append({
        "source_id": f"9{i:018d}",
        "title": f"Поставка песка и щебня №{i}" if i % 3 else f"Ремонт дороги №{i}",
        "price": "2 874 509,65 ₽" if i % 4 else "",
        "address": "г. Калининград, ул. Ленина 1",
        "customer": "МУП «Городские дороги»",
        "organizer": "Комитет по финансам",
        "deadline": "10.12.2026",
        "url": f"https://zakupki.gov.ru/epz/order/notice/ea44/view/common-info.html?regNumber=9{i:018d}",
        "region": "Калининградская область",
        "description": "поставка песка",
        "published_at": "01.11.2026",
    })
from tendermonitor.importer import import_rows
added, updated, skipped = import_rows(rows)
check("seed 120 rows", (added, updated, skipped) == (120, 0, 0), (added, updated, skipped))
with db.session() as con:
    # id1 — полностью пустая карточка, как legacy-строки из реальной БД
    con.execute("""UPDATE tenders SET title='Закупка мебели', price=NULL, region='', address='', object_address='',
        customer='', organizer='', deadline='', deadline_time='', description='',
        score=0, relevance='Низкая', category='Не определено', region_check='Проверить регион' WHERE id=1""")
    # id3 — длинный заголовок/заказчик, регион пуст, сохранённый балл устарел
    con.execute("UPDATE tenders SET title=?, customer=?, region='', address='', object_address='', description='', score=20 WHERE id=3",
                (LONG_TITLE, "ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ " * 6))
    # id4 — ISO-дата и пустой заказчик
    con.execute("UPDATE tenders SET deadline='2026-12-02 10:00', customer='', organizer='' WHERE id=4")
    con.execute("UPDATE tenders SET relevance='Средняя', score=50 WHERE id BETWEEN 7 AND 80")
    con.commit()

app = create_app()
ensure_admin()
client = app.test_client()
token = csrf(client)
client.post("/login", data={"csrf_token": token, "login": "admin", "password": "admin"})
token = csrf(client, "/account/password")
resp = client.post("/account/password", data={
    "csrf_token": token, "current_password": "admin",
    "new_password": "panel123", "repeat_password": "panel123"})
check("password changed", resp.status_code == 302)

# --- список: карточки вместо таблицы -------------------------------------------
resp = client.get("/tenders")
html = resp.get_data(as_text=True)
check("page1 200", resp.status_code == 200, resp.status_code)
check("cards grid present", '<div class="cards">' in html)
check("50 cards", html.count('<article class="tcard">') == 50, html.count('<article class="tcard">'))
check("no table in list", "<table" not in html)
check("no broken uppercase headers", "РЕЛЕВАНТНОСТЬ" not in html)
check("no dash placeholders", ">—<" not in html)
check("badges relevance+category", 'class="badge rel-' in html and 'class="badge cat"' in html)
check("score shown", 'class="muted score">балл ' in html)
check("title link clamp", 'class="clamp clamp-3" href="/tender/' in html)
check("eis link text", "Открыть на ЕИС" in html)

html_last = client.get("/tenders?page=3").get_data(as_text=True)
empty = card_of(html_last, 1)
check("empty card found", bool(empty))
check("empty card hides price", '<span class="k">НМЦК</span>' not in empty)
check("empty card hides region", '<span class="k">Регион</span>' not in empty)
check("empty card hides customer", '<span class="k">Заказчик</span>' not in empty)
check("empty card hides deadline", '<span class="k">Срок подачи</span>' not in empty)
check("empty card no meta list", '<ul class="meta">' not in empty)
check("empty card explains zero", "оценка 0" in empty and "Стоп-фразы профиля" in empty and "мебель" in empty)
check("empty card keeps eis link", "Открыть на ЕИС" in empty)

full = card_of(html, 2)
check("full card shows price", "НМЦК" in full and "2 874 509,65 ₽" in full)
check("full card shows region", "Калининградская область" in full)
check("full card shows customer", "МУП «Городские дороги»" in full)
check("full card shows deadline", "10.12.2026" in full)
check("full card no zero hint", "оценка 0" not in full)

long_card = card_of(html_last, 3)
check("long title clamped with tooltip", 'title="' in long_card and "очень длинное слово" in long_card)
check("long customer clamp-2", 'class="v clamp clamp-2"' in long_card)
check("region missing hint", "Регион не указан в источнике" in long_card)

iso = card_of(html, 4)
check("iso deadline normalized", "02.12.2026 10:00" in iso)
check("empty customer hidden", '<span class="k">Заказчик</span>' not in iso)

# --- пояснение на странице списка ----------------------------------------------
check("scoring block present", "Как формируется релевантность" in html)
check("scoring thresholds", "от 65" in html and "от 35" in html)
check("scoring weights", "<b>35</b>" in html and "<b>12</b>" in html and "+15" in html)
check("scoring regions count", "из 18 названий" in html)
check("scoring explains empty source", "RSS-ленты" in html and "открытых данных" in html)
check("scoring stop block", "Стоп-фразы профиля" in html and "<b>60</b>" in html)

# --- пагинация: регрессия на Ошибку 500 -----------------------------------------
check("page1 next link", 'page=2' in html)
check("next link no csrf", all("csrf_token" not in u for u in re.findall(r'href="([^"]*page=2[^"]*)"', html)))
resp = client.get("/tenders?page=2")
check("page2 200 (регрессия 500)", resp.status_code == 200, resp.status_code)
html2 = resp.get_data(as_text=True)
check("page2 cards", html2.count('<article class="tcard">') == 50)
check("page2 back+next", "page=1" in html2 and "page=3" in html2)
resp = client.get("/tenders?page=2&q=&category=&relevance=&region_check=&region=&price_min=&price_max=&date_from=&date_to=&csrf_token=abc&junk=1")
check("page2 junk params 200", resp.status_code == 200, resp.status_code)
pager = [u for u in re.findall(r'class="button ghost" href="([^"]+)"', resp.get_data(as_text=True)) if "page=" in u]
check("pager links clean", pager and all("csrf_token" not in u and "junk" not in u for u in pager), pager)
resp = client.get("/tenders?page=3")
check("last page 20 shows 20", resp.status_code == 200 and "показано 20 из 120" in resp.get_data(as_text=True))
resp = client.get("/tenders?page=999")
check("overflow clamped", resp.status_code == 200 and "Страница 3 из 3" in resp.get_data(as_text=True))
resp = client.get("/tenders?page=abc")
check("non-numeric page -> 1", resp.status_code == 200 and "Страница 1 из 3" in resp.get_data(as_text=True))
resp = client.get("/tenders?relevance=%D0%A1%D1%80%D0%B5%D0%B4%D0%BD%D1%8F%D1%8F&page=2")
hf = resp.get_data(as_text=True)
check("filter+page2 200", resp.status_code == 200)
check("filter kept in pager", all("relevance=" in u for u in re.findall(r'href="([^"]*page=[13][^"]*)"', hf)))
resp = client.get("/tenders?q=%D0%BF%D0%B5%D1%81%D0%BE%D0%BA&price_min=100000&price_max=3000000&page=2")
check("search+price+page2 200", resp.status_code == 200)
check("search kept in pager", all("q=" in u for u in re.findall(r'href="([^"]*page=[13][^"]*)"', resp.get_data(as_text=True))))
resp = client.get("/tenders?relevance=%D0%92%D1%8B%D1%81%D0%BE%D0%BA%D0%B0%D1%8F&price_min=999999999&page=2")
check("empty result clamp", resp.status_code == 200 and "ничего не найдено" in resp.get_data(as_text=True))

# --- формы: экспорт отдельно, вложенных форм нет --------------------------------
depth = 0
nested = False
for m in re.finditer(r"<form\b|</form>", html):
    depth += -1 if m.group(0) == "</form>" else 1
    if depth > 1:
        nested = True
check("no nested forms", not nested)
check("export form present", re.search(r'<form method="post" action="/export"', html) is not None)

# --- карточка закупки: разбор оценки --------------------------------------------
resp = client.get("/tender/1")
c1 = resp.get_data(as_text=True)
check("card1 200", resp.status_code == 200)
check("explain block", "Оценка релевантности" in c1)
check("explain no title hits", "совпадений нет" in c1)
check("explain region penalty", "не подтверждён: −10" in c1)
check("explain clamp to zero", "= -70 → 0" in c1)
check("explain stop row", "Стоп-фразы в заголовке" in c1 and "мебель" in c1 and "−60" in c1)
check("explain category reason", "ни одна группа ключевых слов не совпала" in c1)
check("explain missing region source", "источник не передал ни регион, ни адрес" in c1)
check("explain thresholds", "«Высокая» ≥ 65" in c1 and "«Средняя» ≥ 35" in c1)
check("card1 no dash-price", "— ₽" not in c1)

resp = client.get("/tender/2")
c2 = resp.get_data(as_text=True)
check("card2 region bonus", "подтверждён: +15" in c2)
check("card2 title phrases listed", "песок, щебень" in c2)
check("card2 formula", "2×35 + 0×12 + 15 = 85" in c2)
resp = client.get("/tender/6")
c6 = resp.get_data(as_text=True)
check("card6 mixed zones formula", "2×35 + 1×12 + 15 = 97" in c6)
check("card6 title phrase", "ремонт дороги" in c6)

resp = client.get("/tender/3")
c3 = resp.get_data(as_text=True)
check("card3 legacy score note", "В базе сохранена оценка 20" in c3 and "Пересчёт по текущему конфигу даёт 0" in c3)

# --- explain/classify: контракт не сломан ---------------------------------------
from tendermonitor.classifier import classify, explain
row = {"title": "Ремонт дороги по ул. Ленина", "region": "Калининградская область",
       "address": "г. Калининград", "description": "", "customer": ""}
res = classify(row)
check("classify keys unchanged", set(res) == {"category", "score", "relevance", "region_check", "keywords"}, set(res))
check("classify score", res["score"] == 85 and res["relevance"] == "Высокая", res)
exp = explain(row)
check("explain components", exp["title_phrases"] == ["ремонт", "ремонт дороги"] and exp["region_ok"] is True, exp["title_phrases"])
check("explain raw score", exp["raw_score"] == 85 and exp["thresholds"]["high"] == 65)
check("explain regions list", len(exp["regions"]) == 18 and exp["groups"])
zero = explain({"title": "Закупка мебели", "region": "", "address": ""})
check("explain zero raw", zero["raw_score"] == -70 and zero["score"] == 0 and zero["category"] == "Не определено")
check("explain zero stop", zero["stop_phrases"] == ["мебель"], zero["stop_phrases"])

print(f"\nALL {PASS} CHECKS PASSED")