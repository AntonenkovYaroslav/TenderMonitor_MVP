"""Iteration 2 verification: price, classifier, title/service guards, DOM pairs, import/export."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # корень проекта; работает после clone/ZIP
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = Path(tempfile.mkdtemp(prefix="tm_iter2_"))

from tendermonitor import db, exporter
db.DATA = TMP / "data"
db.DB = db.DATA / "tenders.db"
exporter.REPORTS = TMP / "reports"

from tendermonitor.importer import parse_price, import_rows, import_csv
from tendermonitor.classifier import classify
from tendermonitor.sources.eis_browser import clean_title, _is_service, _page_pairs, _find_search_input, _looks_like_region, _org_ok, _org_from_text
from tendermonitor.sources.open_data import import_open_data

PASS = 0
def check(name, condition, detail=""):
    global PASS
    assert condition, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"ok {PASS:02d} {name}")

# --- 1. price parsing ---
NBSP, NNBSP, THIN = chr(0xA0), chr(0x202F), chr(0x2009)
cases = [
    ("2 874 509,65 ₽", 2874509.65),
    (f"2{NBSP}874{NBSP}509,65{NBSP}₽", 2874509.65),
    (f"2{NNBSP}874{THIN}509,65 руб.", 2874509.65),
    ("1 234 567.89", 1234567.89),
    ("1.234.567,89", 1234567.89),
    ("1234567,89", 1234567.89),
    ("1234567", 1234567.0),
    ("1.234", 1234.0),
    ("0,00", 0.0),
    ("2 874 509,65 Российский рубль", 2874509.65),
    ("", None),
    ("Российский рубль", None),
    (None, None),
]
for raw, expected in cases:
    got = parse_price(raw)
    check(f"price {raw!r}", got == expected, f"got {got}")

# --- 2. classifier: word forms, negations, gradation, region ---
def row(title, region="Калининградская область", description="", address=""):
    return {"title": title, "region": region, "description": description, "address": address, "customer": ""}

c = classify(row("Ремонта дороги по ул. Ленина"))
check("wordform ремонт дороги", c["category"] == "Дороги и благоустройство" and "ремонт дороги" in c["keywords"], c)
check("wordform score high", c["relevance"] == "Высокая" and c["score"] == 85, c)
c = classify(row("Поставка песка для нужд города"))
check("wordform песка", c["category"] == "Стройматериалы" and "песок" in c["keywords"], c)
c = classify(row("Поставка щебня фракции 20-40"))
check("wordform щебня", "щебень" in c["keywords"], c)
c = classify(row("Уборку снега и вывоз снега", region=""))
check("wordform уборку снега", "уборка снега" in c["keywords"], c)
c = classify(row("Работы завершены, поставка песка не требуется"))
check("negation after", c["category"] == "Не определено" and c["keywords"] == "", c)
c = classify(row("Не требуется поставка песка"))
check("negation before", c["keywords"] == "", c)
c = classify(row("Поставка грунта без песка"))
check("negation без", "песок" not in c["keywords"], c)
c = classify(row("Поставка песочницы детской"))
check("no false positive песочница", c["keywords"] == "", c)
c = classify(row("Капитальный ремонт дороги, асфальтирование"))
check("gradation high", c["relevance"] == "Высокая" and c["score"] == 100, c)
c = classify(row("Ремонт дороги", region=""))
check("gradation without region", c["relevance"] == "Средняя" and c["score"] == 60, c)
c = classify(row("Ремонт дороги", description="поставка песка и щебня"))
check("gradation combination high", c["relevance"] == "Высокая" and c["score"] == 100, c)
c = classify(row("Ремонт дороги", region="Московская область", address="г. Москва"))
check("other region check", c["region_check"] == "Проверить регион", c)
c = classify(row("Ремонт дороги", region="Калининградской области"))
check("region wordform + label", c["region_check"] == "Калининградская область", c)
c = classify(row("Песок, щебень, ЩПС"))
check("category by max hits", c["category"] == "Стройматериалы", c)
c = classify(row("Асфальтобетонная смесь"))
check("adjective to noun", "асфальтобетон" in c["keywords"], c)

# --- 3. title cleaning and service guard ---
check("title на поставку", clean_title("на поставку песка") == "Песка")
check("title на выполнение", clean_title("На выполнение работ по ремонту дороги") == "Работ по ремонту дороги")
check("title на оказание", clean_title("на оказание услуг спецтехники") == "Услуг спецтехники")
check("title contract prefix", clean_title("на право заключения контракта на поставку песка") == "Песка")
check("title unchanged", clean_title("Ремонт дороги") == "Ремонт дороги")
check("service Регион", _is_service("Регион") is True)
check("service label", _is_service("Наименование заказчика") is True)
check("service empty", _is_service("") is True)
check("org Регион passes", _is_service("ООО «Регион»") is False)
check("org Информационный passes", _is_service("Информационно-вычислительный центр") is False)
check("org normal passes", _is_service("МУП «Городские дороги»") is False)
check("region-like value detected", _looks_like_region("Калининградская обл") is True)
check("region-like full name", _looks_like_region("Московская область") is True)
check("org with oblast passes", _looks_like_region("Правительство Калининградской области") is False)
check("service date line", _is_service("Дата начала исполнения контракта") is True)
check("org rejects section header", _org_ok("Информация о процедуре закупки") is False)
check("org rejects contract fragment", _org_ok("с даты заключения контракта") is False)
check("org rejects region value", _org_ok("Калининградская обл") is False)
check("org accepts institution", _org_ok("государственное бюджетное учреждение здравоохранения «Областная больница»") is True)
check("org accepts IP", _org_ok("ИП Иванов Иван Иванович") is True)
check("org accepts OOO Region", _org_ok("ООО «Регион»") is True)
check("org accepts info center", _org_ok("Информационно-вычислительный центр") is True)
check("org rejects datetime", _org_ok("19.11.2026 17:42 (МСК-1)") is False)
check("org rejects funding header", _org_ok("Закупка за счет собственных и иных средств") is False)
check("org accepts quoted name", _org_ok("«КалининградМелиорация»") is True)
check("org rejects quoted title", _org_ok("Выполнение работ по объекту: «Благоустройство территории»") is False)
check("org from extra info", _org_from_text(
    'ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ ЗДРАВООХРАНЕНИЯ "БОЛЬНИЦА". Место нахождения: Российская Федерация, г Калининград'
) == 'ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ ЗДРАВООХРАНЕНИЯ "БОЛЬНИЦА"')
check("org from extra info empty", _org_from_text("Дата размещения: 01.11.2026") == "")

# --- 4. DOM pairs and search selector on a local fixture (headless chromium) ---
FIXTURE = TMP / "card.html"
FIXTURE.write_text("""<html><body>
<input type="hidden" name="searchString" value="decoy-hidden">
<input type="text" name="otherFilter" placeholder="Фильтр по дате">
<div class="search"><input type="text" name="searchString" placeholder="Поиск закупок"></div>
<div class="row"><div class="col"><span>Наименование заказчика</span></div><div class="col"><span>МУП «Городские дороги»</span></div></div>
<div class="row"><span>Организация, осуществляющая размещение</span><span>Комитет по закупкам администрации</span></div>
<div class="row"><span>Начальная (максимальная) цена контракта</span><span>2 874 509,65 ₽</span></div>
<div class="row"><span>Наименование объекта закупки</span><span>на поставку песка и щебня</span></div>
<div class="row"><span>Субъект Российской Федерации</span><span>Калининградская область</span></div>
<div class="row"><span>Заказчик</span><span>Регион</span><span>МУП «Городские дороги»</span></div>
<p>Наименование заказчика</p><p>Регион</p><p>Дата размещения</p><p>МУП «Городские дороги»</p>
</body></html>""", encoding="utf-8")

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(FIXTURE.as_uri())
    search = _find_search_input(page)
    check("selector picks visible searchString", search.evaluate("el => el.name") == "searchString"
          and search.evaluate("el => el.type") == "text")
    pairs = _page_pairs(page)
    check("pair parent-sibling customer", pairs.get("наименование заказчика") == "муп «городские дороги»", pairs)
    check("pair direct sibling organizer", pairs.get("организация, осуществляющая размещение") == "комитет по закупкам администрации", pairs)
    check("pair price raw", pairs.get("начальная (максимальная) цена контракта") == "2 874 509,65 ₽", pairs)
    check("pair region", pairs.get("субъект российской федерации") == "калининградская область", pairs)
    check("pair price parses", parse_price(pairs.get("начальная (максимальная) цена контракта")) == 2874509.65)
    check("pair title cleans", clean_title(pairs.get("наименование объекта закупки")) == "Песка и щебня")
    browser.close()

# --- 5. import/export isolated: fields kept, price parsed, sheets correct ---
rows = [{
    "source_id": "0123456789012345678", "title": "на поставку песка и щебня",
    "price": "2 874 509,65 ₽", "address": "г. Калининград, ул. Ленина 1",
    "customer": "МУП «Городские дороги»", "organizer": "Комитет по закупкам",
    "deadline": "20.11.2026", "deadline_time": "10:00", "object_address": "г. Калининград, ул. Ленина 1",
    "url": "https://zakupki.gov.ru/example/1", "region": "Калининградская область",
    "description": "поставка песка", "published_at": "01.11.2026",
}, {
    "source_id": "0123456789012345679", "title": "Ремонт дороги",
    "price": "1.234.567,89", "address": "г. Москва", "customer": "ГБУ Москвы",
    "url": "https://zakupki.gov.ru/example/2", "region": "Московская область",
}, {
    "source_id": "0123456789012345680", "title": "Ремонт дороги по ул. Советской",
    "price": "500000,00", "address": "г. Калининград", "customer": "МУП Калининграда",
    "url": "https://zakupki.gov.ru/example/3", "region": "Калининградская область",
}]
added, updated, skipped = import_rows(rows)
check("import_rows counts", (added, updated, skipped) == (3, 0, 0), (added, updated, skipped))
with db.session() as con:
    first = con.execute("SELECT price, organizer, deadline_time, title, relevance, region_check FROM tenders WHERE source_id='0123456789012345678'").fetchone()
    second = con.execute("SELECT price, region_check FROM tenders WHERE source_id='0123456789012345679'").fetchone()
check("db price parsed", first["price"] == 2874509.65, first["price"])
check("db organizer kept", first["organizer"] == "Комитет по закупкам")
check("db deadline_time kept", first["deadline_time"] == "10:00")
check("db title kept as given", first["title"] == "на поставку песка и щебня", first["title"])
check("db second price", second["price"] == 1234567.89, second["price"])
check("db other region flagged", second["region_check"] == "Проверить регион")
with db.session() as con:
    third = con.execute("SELECT relevance, score FROM tenders WHERE source_id='0123456789012345680'").fetchone()
check("db high gradation", third["relevance"] == "Высокая" and third["score"] == 85, dict(third))
path, count = exporter.export()
check("export count", count == 3, count)
from openpyxl import load_workbook
wb = load_workbook(path)
check("export sheets", wb.sheetnames == ["Все закупки", "Высокая", "Средняя", "Проверить регион"], wb.sheetnames)
check("sheet region rows", wb["Проверить регион"].max_row >= 2, wb["Проверить регион"].max_row)

# --- 6. regression: open-data fixture and sample CSV in isolated DB ---
imported, skipped, parsed = import_open_data(str(ROOT / "data" / "eis_open_data.csv"))
check("open data 342/0/342", (imported, skipped, parsed) == (342, 0, 342), (imported, skipped, parsed))
n, u, sk = import_csv(str(ROOT / "data" / "sample_tenders.csv"))
check("sample csv imports", n + u > 0 and sk == 0, (n, u, sk))
path, count = exporter.export()
# Сквозная дедупликация: экспорт содержит ровно одну строку на канонический ключ.
# Номера трёх тестовых записей присутствуют в eis_open_data.csv, поэтому при
# импорте открытых данных они сливаются (а не добавляются отдельно).
with db.session() as con:
    distinct = con.execute("SELECT COUNT(DISTINCT canonical_key) c FROM tenders").fetchone()["c"]
check("export after regressions", count == distinct, (count, distinct))
wb = load_workbook(path)
check("sheet medium rows", wb["Средняя"].max_row >= 2, wb["Средняя"].max_row)
check("sheet high rows", wb["Высокая"].max_row >= 2, wb["Высокая"].max_row)

print(f"\nALL {PASS} CHECKS PASSED")