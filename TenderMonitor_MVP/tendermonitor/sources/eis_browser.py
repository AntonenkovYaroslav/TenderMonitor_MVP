"""Read public EIS procurement pages with a locally installed Playwright browser."""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ..config import settings
from ..db import session, init_db
from ..exporter import export
from ..importer import import_rows

SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?recordsPerPage=_50"
NOTICE_PATH = re.compile(r"/epz/order/notice/[^\"'?#]+/view/common-info\.html", re.I)
RSS_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
# Кандидаты строки поиска по убыванию специфичности: случайный первый input
# (фильтры, подписки, скрытые поля) больше не используется, если есть целевой.
SEARCH_SELECTORS = (
    "input[name='searchString']",
    "#searchString",
    "input[type='search']",
    "input[placeholder*='Поиск' i]",
    "input[placeholder*='поиск' i]",
)
# Служебные строки карточки, которые не должны попадать в значения полей:
# точные метки и длинные служебные фразы. Короткие слова вроде «регион» не
# используются как префикс, чтобы не отбраковать названия организаций.
_SERVICE_PREFIXES = (
    "субъект российской федерации", "дата и время", "адрес электронной почты",
    "номер контактного телефона", "идентификационный код закупки",
    "начальная (максимальная) цена", "начальная цена", "окончание подачи заявок",
    "место нахождения", "адрес места нахождения", "наименование заказчика",
    "организация, осуществляющая", "ответственное должностное лицо",
    "объект закупки", "наименование объекта закупки", "способ определения поставщика",
    "обеспечение заявки", "обеспечение исполнения", "требования к участникам",
    "преимущества и требования", "условия признания", "размещено ", "этап ",
    "дата начала", "дата окончания", "дата размещения", "дата публикации",
    "срок подачи", "срок исполнения", "начало исполнения", "окончание исполнения",
)
# Служебные префиксы заголовка: «на поставку ...», «на выполнение ...»,
# «на оказание ...», «на право заключения контракта на ...» (в т.ч. цепочкой).
_TITLE_PREFIXES = (
    re.compile(r"^на\s+право\s+заключения\s+(?:контракта|договора)(?:\s+на)??\s+", re.I),
    re.compile(r"^на\s+(?:поставку|выполнение|оказание)\s+", re.I),
)
# Метки карточки ЕИС, для которых строится DOM-пара «поле -> значение».
_PAIR_LABELS = (
    "Наименование объекта закупки", "Объект закупки",
    "Начальная (максимальная) цена контракта", "Начальная цена",
    "Дата и время окончания срока подачи заявок", "Окончание подачи заявок",
    "Наименование заказчика", "Заказчик",
    "Организация, осуществляющая размещение", "Организация, осуществляющая размещение извещения",
    "Ответственное должностное лицо", "Адрес электронной почты",
    "Номер контактного телефона", "Адрес места нахождения", "Место нахождения",
    "Идентификационный код закупки", "Субъект Российской Федерации", "Регион",
    "Дополнительная информация", "Требования заказчика",
)
_SERVICE_LABELS = {label.lower().replace("ё", "е") for label in _PAIR_LABELS} | {
    "регион", "субъект рф", "заказчик", "организатор закупки", "дата", "номер",
    "нмцк", "начальная цена контракта", "срок подачи заявок", "контактное лицо",
    "контактная информация", "информация о заказчике", "информация об организации",
}

def clean_title(text: str) -> str:
    """Убирает служебные префиксы; исходный текст сохраняется в description карточки."""
    original = (text or "").strip()
    cleaned = original
    for _ in range(4):
        for pattern in _TITLE_PREFIXES:
            cleaned = pattern.sub("", cleaned).strip()
    if cleaned and cleaned != original:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned or original

def _is_service(value: str) -> bool:
    low = re.sub(r"\s+", " ", (value or "").strip().lower().replace("ё", "е"))
    return not low or low in _SERVICE_LABELS or low.startswith(_SERVICE_PREFIXES)

# Название региона само по себе («Калининградская обл») — не название организации.
_REGION_VALUE = re.compile(r"^(?:[а-яё-]+\s+)+(обл\.?|область|край|республика|ао|округ)$", re.I)

def _looks_like_region(value: str) -> bool:
    low = re.sub(r"\s+", " ", (value or "").strip().lower().replace("ё", "е"))
    return bool(_REGION_VALUE.match(low))

# Признаки названия организации: организационно-правовая форма, тип учреждения
# или органа власти. Используется только для полей customer/organizer.
_ORG_MARKERS = (
    "учреждение", "предприятие", "администрация", "комитет", "управление",
    "министерство", "департамент", "правительство", "мэрия", "дума", "совет",
    "казначейство", "инспекция", "служба", "больница", "поликлиника", "диспансер",
    "школа", "гимназия", "лицей", "детский сад", "музей", "театр", "библиотека",
    "фонд", "центр", "станция", "отдел", "войсковая", "часть", "ооо", "пао", "зао",
    "фгбу", "гбу", "мбу", "мку", "гку", "фгуп", "гуп", "муп", "казенное",
    "бюджетное", "автономное", "унитарное", "некоммерческая", "организация",
    "общество", "товарищество", "кооператив", "ассоциация", "союз", "палата",
    "университет", "институт", "академия", "колледж", "техникум", "санаторий",
    "интернат", "приют", "ип ", "ао ",
)

_ORG_BAD_STARTS = (
    "выполнение работ", "поставка ", "оказание услуг", "на поставку",
    "на выполнение", "на оказание", "с даты", "по дату", "информация",
    "дата ", "срок ", "закупка за счет", "начало ", "окончание ",
)

def _org_from_text(value: str) -> str:
    """Первое предложение-название организации из блока «Дополнительная информация»."""
    head = re.split(r"\.\s+(?=Место нахождения|Почтовый адрес|Телефон|Адрес электронной|Ответственное)",
                    (value or "").strip(), maxsplit=1)[0].strip()
    return head if _org_ok(head) else ""

def _org_ok(value: str) -> bool:
    low = re.sub(r"\s+", " ", (value or "").strip().lower().replace("ё", "е"))
    if not low or _is_service(value) or _looks_like_region(value):
        return False
    if low.startswith(_ORG_BAD_STARTS):
        return False
    if any(marker in low for marker in _ORG_MARKERS):
        return True
    # Название в кавычках без явной формы («Ромашка») тоже считается организацией.
    return "«" in (value or "") or '"' in (value or "")

def _launch_browser(playwright, headless: bool = True):
    """Prefer the Windows Edge installation; install Playwright Chromium only if Edge is unavailable."""
    try:
        return playwright.chromium.launch(
            channel="msedge", headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--ignore-certificate-errors"],
        )
    except PlaywrightError as edge_error:
        logging.info("Microsoft Edge channel unavailable; installing Playwright Chromium: %s", edge_error)
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )
        return playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--ignore-certificate-errors"],
        )

def _find_search_input(page):
    for selector in SEARCH_SELECTORS:
        locator = page.locator(selector)
        for idx in range(locator.count()):
            candidate = locator.nth(idx)
            if candidate.is_visible() and candidate.is_enabled():
                return candidate
    fallback = page.locator("input:not([type=hidden])")
    for idx in range(fallback.count()):
        candidate = fallback.nth(idx)
        if candidate.is_visible() and candidate.is_enabled():
            return candidate
    raise PlaywrightError("Search input not found on EIS results page")

def _page_pairs(page) -> dict:
    """Пары «метка -> значение» из DOM карточки: значение берётся из соседнего
    элемента (или соседа родителя), а не из случайной следующей строки текста."""
    return page.evaluate(
        r"""(labels) => {
            const norm = s => (s || '').replace(/\s+/g, ' ').replace(/[:：]\s*$/, '').trim().toLowerCase();
            const wanted = new Set(labels.map(norm));
            const out = {};
            for (const el of document.querySelectorAll('body *')) {
                if (el.children.length) continue;
                const key = norm(el.textContent);
                if (!key || !wanted.has(key) || key in out) continue;
                let value = '';
                const sibling = el.nextElementSibling;
                if (sibling) value = norm(sibling.innerText || sibling.textContent);
                if (!value && el.parentElement && el.parentElement.nextElementSibling) {
                    value = norm(el.parentElement.nextElementSibling.innerText || el.parentElement.nextElementSibling.textContent);
                }
                if (value) out[key] = value;
            }
            return out;
        }""",
        list(_PAIR_LABELS),
    )

def _read_rss_records(region: str = "") -> list[dict]:
    """Read the EIS public search feed and normalize entries for the existing importer."""
    query = urllib.parse.urlencode({
        "searchString": region,
        "morphology": "on",
        "pageNumber": "1",
        "recordsPerPage": "_50",
        "fz44": "on",
        "fz223": "on",
    })
    request = urllib.request.Request(
        f"{RSS_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        xml_data = response.read()
    root = ET.fromstring(xml_data)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    records = []
    for item in entries:
        def value(name: str) -> str:
            node = item.find(name)
            if node is None:
                node = item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
            if node is None:
                return ""
            if name == "link" and not (node.text or "").strip():
                return node.attrib.get("href", "").strip()
            return (node.text or "").strip()

        def clean(text: str) -> str:
            text = re.sub(r"<(?:br\s*/?|/p|/div|/li|/tr)\s*>", "\n", text, flags=re.I)
            text = unescape(re.sub(r"<[^>]+>", " ", text))
            return "\n".join(re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip())

        def fields(*names: str) -> str:
            wanted = {name.lower().replace("ё", "е") for name in names}
            for node in item.iter():
                tag = node.tag.rsplit("}", 1)[-1].split(":")[-1].lower().replace("ё", "е")
                if tag in wanted and (node.text or "").strip():
                    return clean(node.text or "")
            return ""

        title = clean(value("title"))
        link = value("link")
        guid = value("guid") or value("id")
        description_raw = value("description") or value("summary")
        description = clean(description_raw)
        source_match = re.search(r"\d{19}", " ".join((guid, link, title)))
        if not title or not source_match or not link:
            continue

        searchable = "\n".join((title, description))
        def labelled(*labels: str) -> str:
            explicit = fields(*labels)
            if explicit:
                return explicit
            label_pattern = "|".join(re.escape(label) for label in labels)
            match = re.search(rf"(?:{label_pattern})\s*[:：\-]?\s*([^\n;|]+)", searchable, re.I)
            return match.group(1).strip() if match else ""

        raw_price = labelled("НМЦК", "Начальная (максимальная) цена контракта", "Начальная цена")
        customer = labelled("Заказчик", "Наименование заказчика")
        deadline = labelled("Дата окончания подачи заявок", "Окончание подачи заявок", "Срок подачи заявок", "Дата и время окончания срока подачи заявок")
        deadline_match = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?", deadline)
        deadline_date = deadline_match.group(1) if deadline_match else ""
        deadline_time = deadline_match.group(2) if deadline_match and deadline_match.group(2) else ""
        records.append({
            "source_id": source_match.group(0), "title": clean_title(title),
            "price": raw_price, "address": "", "object_address": "",
            "customer": customer, "organizer": "", "deadline": deadline_date, "deadline_time": deadline_time,
            "contact_name": "", "contact_phone": "", "contact_email": "",
            "url": link, "region": region,
            "description": description[:5000], "published_at": value("pubDate") or value("published"),
        })
    return records

def _extract_records(page, region: str = "") -> list[dict]:
    # Регион поиска берётся из конфига; релевантность решает классификатор.
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    search = _find_search_input(page)
    search.fill(region)
    search.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    # Wait for EIS results/cards to populate instead of scraping the page shell.
    page.wait_for_function(
        "noticePath => { const pattern = new RegExp(noticePath, 'i'); return [...document.querySelectorAll('a[href]')].some(a => pattern.test(a.href)); }",
        arg=NOTICE_PATH.pattern,
        timeout=45000,
    )
    links = page.locator("a[href]").evaluate_all("els => els.map(a => ({href:a.href,title:(a.innerText||a.textContent||'').trim()}))")
    candidates = []
    for link in links:
        href = link.get("href", "")
        title = re.sub(r"\s+", " ", link.get("title", "")).strip()
        if title and NOTICE_PATH.search(href):
            candidates.append((title, href))
    seen, records = set(), []
    for title, href in candidates:
        if href in seen:
            continue
        seen.add(href)
        record_page = page.context.new_page()
        try:
            record_page.goto(href, wait_until="domcontentloaded", timeout=45000)
            record_page.wait_for_timeout(700)
            raw_text = record_page.locator("body").inner_text(timeout=15000)
            lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines() if line.strip()]
            text = " ".join(lines)
            pairs = _page_pairs(record_page)
            def pair_value(*labels: str, org: bool = False) -> str:
                for label in labels:
                    value = pairs.get(label.lower().replace("ё", "е"), "")
                    if not value or not (_org_ok(value) if org else not _is_service(value)):
                        continue
                    cut = len(value)
                    for other in _PAIR_LABELS:
                        key = other.lower().replace("ё", "е")
                        idx = value.find(key)
                        if idx > 0:
                            cut = min(cut, idx)
                    value = value[:cut].strip(" ,;:")
                    if value and (_org_ok(value) if org else not _is_service(value)):
                        return value
                return ""
            def next_value(*labels: str, org: bool = False) -> str:
                def acceptable(candidate: str) -> bool:
                    return _org_ok(candidate) if org else not _is_service(candidate)
                for idx, line in enumerate(lines):
                    low = line.lower()
                    for label in labels:
                        pos = low.find(label.lower())
                        if pos < 0:
                            continue
                        # Формат «метка: значение» в одной строке; метка не
                        # должна быть частью более длинного слова («заказчика»).
                        tail = line[pos + len(label):]
                        if tail[:1].isalpha():
                            continue
                        rest = tail.strip(" :：-")
                        if rest and acceptable(rest) and not any(l.lower() in rest.lower() for l in labels):
                            return rest
                        j = idx + 1
                        while j < len(lines) and not acceptable(lines[j]):
                            j += 1
                        if j < len(lines):
                            value = lines[j]
                            if not any(l.lower() in value.lower() for l in labels):
                                return value
                return ""
            def field(*labels: str, org: bool = False) -> str:
                return pair_value(*labels, org=org) or next_value(*labels, org=org)
            reg_match = re.search(r"(?:№|номер извещения)\s*([0-9]{19})", text, re.I)
            if not reg_match:
                reg_match = re.search(r"(?:regNumber|regnumber)=([0-9]{19})", href, re.I)
            if not reg_match:
                continue
            title_value = field("Наименование объекта закупки", "Объект закупки") or title
            price_value = field("Начальная (максимальная) цена контракта", "Начальная цена")
            deadline_value = field("Дата и время окончания срока подачи заявок", "Окончание подачи заявок")
            deadline_match = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*(\d{2}:\d{2})?", deadline_value)
            customer = field("Наименование заказчика", "Заказчик", "Требования заказчика", org=True) \
                or _org_from_text(pairs.get("дополнительная информация", ""))
            organizer = field("Организация, осуществляющая размещение", "Организация, осуществляющая размещение извещения", org=True)
            contact_name = field("Ответственное должностное лицо")
            contact_email = field("Адрес электронной почты")
            contact_phone = field("Номер контактного телефона")
            address = field("Адрес места нахождения", "Место нахождения")
            ikz = field("Идентификационный код закупки")
            card_region = field("Субъект Российской Федерации", "Регион")
            records.append({
                "source_id": reg_match.group(1), "title": clean_title(title_value),
                "price": price_value,
                "address": address, "object_address": address,
                "customer": customer, "organizer": organizer,
                "deadline": deadline_match.group(1) if deadline_match else "",
                "deadline_time": deadline_match.group(2) if deadline_match and deadline_match.group(2) else "",
                "contact_name": contact_name, "contact_phone": contact_phone, "contact_email": contact_email,
                "url": href, "region": card_region or region,
                "description": (f"ИКЗ: {ikz}. " if ikz else "") + text[:5000],
                "published_at": "",
            })
        finally:
            record_page.close()
    return records

def run_once() -> tuple[int, int, Path]:
    """Collect EIS once, keep only unseen matching notices, and update the fixed workbook."""
    init_db()
    cfg = settings()
    region = (cfg.get("region") or "").strip()
    headless = bool(cfg.get("headless", True))
    demo_include_all = bool(cfg.get("demo_include_all", False))
    method = "Edge"
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, headless=headless)
            context = browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
            page = context.new_page()
            try:
                try:
                    records = _extract_records(page, region=region)
                except PlaywrightError as exc:
                    method = "RSS"
                    logging.warning("Edge collection failed; using EIS RSS: %s", exc)
                    records = _read_rss_records(region=region)
                if not records and method != "RSS":
                    method = "RSS"
                    logging.warning("Edge returned no raw records; using EIS RSS")
                    records = _read_rss_records(region=region)
            finally:
                page.close()
                browser.close()

        logging.info("EIS collection method=%s; raw records before filters=%d", method, len(records))
        if demo_include_all:
            relevant = records
            filtered_out = 0
            logging.info("Demo mode enabled (config demo_include_all): business and region filters are bypassed")
        else:
            from ..classifier import classify
            relevant = []
            filtered_out = 0
            for record in records:
                result = classify(record)
                if result["category"] != "Не определено" and result["region_check"] != "Проверить регион":
                    relevant.append(record)
                else:
                    filtered_out += 1

        with session() as con:
            existing_ids = {row[0] for row in con.execute("SELECT DISTINCT source_id FROM tenders")}
        unseen = []
        duplicates = 0
        for record in relevant:
            notice_id = str(record.get("source_id") or "").strip()
            if not notice_id or notice_id in existing_ids:
                duplicates += 1
                continue
            existing_ids.add(notice_id)
            unseen.append(record)

        imported, skipped = import_rows(unseen)
        report, _ = export()
        logging.info(
            "EIS summary: method=%s raw=%d filtered_out=%d matched=%d already_seen=%d imported=%d skipped=%d report=%s",
            method, len(records), filtered_out, len(relevant), duplicates, imported, skipped, report,
        )
        return len(records), imported, report
    except Exception:
        logging.exception("EIS collection failed; method=%s", method)
        raise
