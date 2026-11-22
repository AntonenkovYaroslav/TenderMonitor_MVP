"""Read public EIS procurement pages with a locally installed Playwright browser."""
from __future__ import annotations

import copy
import logging
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ..config import ROOT, settings, search_settings
from ..db import session, init_db
from ..dedup import canonical_key
from ..exporter import export
from ..importer import import_rows

RESULTS_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
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

# --- параметры глубины и сроков поиска ----------------------------------------

def _date_window(published_days: int) -> dict:
    """Окно публикации извещения: publishDateFrom/To в формате ДД.ММ.ГГГГ.
    published_days=0 — без ограничения по дате."""
    if not published_days or published_days <= 0:
        return {}
    today = datetime.now()
    start = today - timedelta(days=int(published_days))
    return {"publishDateFrom": start.strftime("%d.%m.%Y"), "publishDateTo": today.strftime("%d.%m.%Y")}

def _results_url(region: str, per_page: int, page_number: int, window: dict) -> str:
    params = {
        "searchString": region, "morphology": "on", "pageNumber": str(page_number),
        "recordsPerPage": f"_{per_page}", "fz44": "on", "fz223": "on", **window,
    }
    return f"{RESULTS_URL}?{urllib.parse.urlencode(params)}"

def _rss_ssl_context(ca_bundle: str):
    """TLS-контекст для RSS. По умолчанию — системное доверие; при корпоративном
    TLS-прокси можно указать путь к CA-сертификату (search.ca_bundle). Проверка
    сертификата не отключается. Относительный путь считается от корня проекта,
    чтобы настройка переживала clone/ZIP и запуск из любого каталога."""
    if ca_bundle:
        path = Path(ca_bundle)
        if not path.is_absolute():
            path = ROOT / path
        return ssl.create_default_context(cafile=str(path))
    return ssl.create_default_context()

def _is_expired(deadline: str, today: datetime | None = None) -> bool:
    """Срок подачи уже прошёл (дата раньше сегодня). Пустой/нераспознанный срок
    не считается просроченным — такие записи оставляем на проверку."""
    text = (deadline or "").strip()
    if not text:
        return False
    moment = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(text[:10], fmt)
            break
        except ValueError:
            continue
    if moment is None:
        return False
    today = today or datetime.now()
    return moment.date() < today.date()

def _parse_rss_item(item, region: str) -> dict | None:
    """Нормализация одной записи RSS/Atom под существующий импортёр."""
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
        return None

    searchable = "\n".join((title, description))
    def labelled(*labels: str) -> str:
        explicit = fields(*labels)
        if explicit:
            return explicit
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(rf"(?:{label_pattern})\s*[:：\-]?\s*([^\n;|]+)", searchable, re.I)
        return match.group(1).strip() if match else ""

    raw_price = labelled("НМЦК", "Начальная (максимальная) цена контракта", "Начальная цена контракта", "Начальная цена")
    customer = labelled("Заказчик", "Наименование заказчика")
    deadline = labelled("Дата окончания подачи заявок", "Окончание подачи заявок", "Срок подачи заявок", "Дата и время окончания срока подачи заявок")
    deadline_match = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?", deadline)
    deadline_date = deadline_match.group(1) if deadline_match else ""
    deadline_time = deadline_match.group(2) if deadline_match and deadline_match.group(2) else ""
    # В RSS-ленте ЕИС <title> — служебный («Электронный аукцион №…»), а наименование
    # объекта закупки лежит в description. Без него классификатор не видит заголовок
    # и гейт сбора отсекает профильные закупки, пришедшие через RSS-фолбэк.
    object_name = labelled("Наименование объекта закупки", "Наименование закупки")
    if object_name:
        title = object_name
    return {
        "source_id": source_match.group(0), "title": clean_title(title),
        "price": raw_price, "address": "", "object_address": "",
        "customer": customer, "organizer": "", "deadline": deadline_date, "deadline_time": deadline_time,
        "contact_name": "", "contact_phone": "", "contact_email": "",
        "url": link, "region": region,
        "description": description[:5000], "published_at": value("pubDate") or value("published"),
    }

def _read_rss_records(region: str = "", per_page: int = 100, max_pages: int = 5,
                      published_days: int = 30, max_cards: int = 0, ca_bundle: str = "") -> list[dict]:
    """Многостраничная RSS-лента ЕИС с окном публикации и общим лимитом записей.
    Дедупликация по номеру извещения внутри одного запуска."""
    window = _date_window(published_days)
    context = _rss_ssl_context(ca_bundle)
    seen, records = set(), []
    for page_number in range(1, max_pages + 1):
        query = urllib.parse.urlencode({
            "searchString": region, "morphology": "on", "pageNumber": str(page_number),
            "recordsPerPage": f"_{per_page}", "fz44": "on", "fz223": "on", **window,
        })
        request = urllib.request.Request(
            f"{RSS_URL}?{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
        )
        with urllib.request.urlopen(request, timeout=45, context=context) as response:
            xml_data = response.read()
        # Защита от entity-expansion/DTD в недоверенной ленте: ЕИС RSS не содержит DTD.
        if b"<!DOCTYPE" in xml_data[:4096] or b"<!ENTITY" in xml_data:
            raise ValueError("RSS-лента содержит DTD/ENTITY — разбор отклонён")
        root = ET.fromstring(xml_data)
        entries = root.findall(".//item")
        if not entries:
            entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        if not entries:
            break
        for item in entries:
            record = _parse_rss_item(item, region)
            if not record or record["source_id"] in seen:
                continue
            seen.add(record["source_id"])
            records.append(record)
            if max_cards and len(records) >= max_cards:
                break
        logging.info("EIS RSS: page=%d entries=%d total=%d", page_number, len(entries), len(records))
        if max_cards and len(records) >= max_cards:
            break
        if len(entries) < per_page:
            break
    return records

def _collect_candidates(page, region: str, per_page: int, max_pages: int,
                        published_days: int, cap: int) -> list[tuple[str, str]]:
    """Ссылки на карточки из результатов поиска: первая страница прямым GET
    (с окном дат и размером страницы), затем переходы по пагинации ЕИС."""
    window = _date_window(published_days)
    seen, candidates = set(), []

    def wait_cards():
        page.wait_for_function(
            "noticePath => { const pattern = new RegExp(noticePath, 'i'); return [...document.querySelectorAll('a[href]')].some(a => pattern.test(a.href)); }",
            arg=NOTICE_PATH.pattern, timeout=45000,
        )

    def harvest() -> int:
        links = page.locator("a[href]").evaluate_all("els => els.map(a => ({href:a.href,title:(a.innerText||a.textContent||'').trim()}))")
        before = len(candidates)
        for link in links:
            href = link.get("href", "")
            title = re.sub(r"\s+", " ", link.get("title", "")).strip()
            if title and NOTICE_PATH.search(href) and href not in seen:
                seen.add(href)
                candidates.append((title, href))
                if cap and len(candidates) >= cap:
                    break
        return len(candidates) - before

    try:
        page.goto(_results_url(region, per_page, 1, window), wait_until="domcontentloaded", timeout=60000)
        wait_cards()
    except PlaywrightError:
        logging.info("EIS direct results URL failed; falling back to search form")
        page.goto(f"{RESULTS_URL}?recordsPerPage=_{per_page}", wait_until="domcontentloaded", timeout=60000)
        search = _find_search_input(page)
        search.fill(region)
        search.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        wait_cards()
    harvest()

    page_number = 1
    while page_number < max_pages and (not cap or len(candidates) < cap):
        next_number = page_number + 1
        next_href = ""
        try:
            anchors = page.locator("a[href*='pageNumber=']").evaluate_all("els => els.map(a => a.href)")
            pattern = re.compile(rf"[?&]pageNumber={next_number}(?:&|$)")
            next_href = next((href for href in anchors if pattern.search(href)), "")
        except PlaywrightError:
            next_href = ""
        target = next_href or _results_url(region, per_page, next_number, window)
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
            wait_cards()
        except PlaywrightError:
            logging.info("EIS page %d unavailable; stopping pagination", next_number)
            break
        if harvest() == 0:
            break
        page_number = next_number
    logging.info("EIS search: pages=%d cards=%d", page_number, len(candidates))
    return candidates

def _extract_records(page, region: str = "", limit: int = 0, per_page: int = 100,
                     max_pages: int = 5, published_days: int = 30, max_cards: int = 0) -> list[dict]:
    # Регион поиска берётся из конфига; релевантность решает классификатор.
    cap = limit or max_cards
    candidates = _collect_candidates(page, region, per_page, max_pages, published_days, cap)
    if limit and len(candidates) > limit:
        candidates = candidates[:limit]
        logging.info("EIS search: cards limited to %d", limit)
    total = len(candidates)
    logging.info("EIS search: cards found=%d; reading cards", total)
    records = []
    for idx, (title, href) in enumerate(candidates, 1):
        if idx == 1 or idx % 5 == 0 or idx == total:
            logging.info("EIS cards: %d/%d", idx, total)
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

def run_once(region: str | None = None, limit: int | None = None, keywords: str | None = None,
             category: str | None = None, pages: int | None = None, per_page: int | None = None,
             days: int | None = None, stats: dict | None = None) -> tuple[int, int, Path]:
    """Collect EIS once, keep only unseen matching notices, and update the fixed workbook.

    Глубина и сроки поиска берутся из блока "search" конфига и переопределяются
    флагами CLI/формой панели: pages (число страниц), per_page (карточек на
    странице), days (окно публикации в днях), limit (общий лимит карточек).
    Сбор оставляет только закупки с подтверждённым регионом, определённым
    направлением и релевантностью не ниже порога «Средняя» (стоп-шум отсекается);
    просроченные по сроку подачи записи пропускаются (search.skip_expired).

    Необязательный ``stats`` — словарь, который заполняется счётчиками и
    лимитами запуска (для отчёта по источникам в collect-sources); контракт
    возвращаемого 3-кортежа не меняется.
    """
    init_db()
    cfg = settings()
    search = search_settings(cfg)
    search_region = (region or "").strip() or (cfg.get("region") or "").strip()
    headless = bool(cfg.get("headless", True))
    demo_include_all = bool(cfg.get("demo_include_all", False))
    per_page_eff = int(per_page) if per_page else search["records_per_page"]
    pages_eff = int(pages) if pages else search["max_pages"]
    days_eff = int(days) if days is not None else search["published_days"]
    max_cards = search["max_cards"]
    ca_bundle = search["ca_bundle"]
    skip_expired = search["skip_expired"]
    card_limit = int(limit) if limit else 0
    effective_cap = card_limit or max_cards
    extra_keywords = [k.strip() for k in (keywords or "").split(",") if k.strip()]
    wanted_category = (category or "").strip()
    effective_cfg = cfg
    if extra_keywords:
        effective_cfg = copy.deepcopy(cfg)
        effective_cfg.setdefault("groups", {})["Ключевые слова"] = extra_keywords
    medium_threshold = int((cfg.get("thresholds") or {}).get("medium", 35))
    method = "Edge"
    logging.info(
        "EIS collection started; method=Edge headless=%s region=%r limit=%s per_page=%s pages=%s days=%s "
        "keywords=%s category=%r demo_include_all=%s skip_expired=%s",
        headless, search_region, effective_cap or "нет", per_page_eff, pages_eff, days_eff,
        ",".join(extra_keywords) or "нет", wanted_category or "любое", demo_include_all, skip_expired,
    )
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, headless=headless)
            context = browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
            page = context.new_page()
            try:
                try:
                    records = _extract_records(
                        page, region=search_region, limit=card_limit, per_page=per_page_eff,
                        max_pages=pages_eff, published_days=days_eff, max_cards=effective_cap,
                    )
                except PlaywrightError as exc:
                    method = "RSS"
                    logging.warning("Edge collection failed; using EIS RSS: %s", exc)
                    records = _read_rss_records(
                        region=search_region, per_page=per_page_eff, max_pages=pages_eff,
                        published_days=days_eff, max_cards=effective_cap, ca_bundle=ca_bundle,
                    )
                if not records and method != "RSS":
                    method = "RSS"
                    logging.warning("Edge returned no raw records; using EIS RSS")
                    records = _read_rss_records(
                        region=search_region, per_page=per_page_eff, max_pages=pages_eff,
                        published_days=days_eff, max_cards=effective_cap, ca_bundle=ca_bundle,
                    )
                if effective_cap and len(records) > effective_cap:
                    records = records[:effective_cap]
            finally:
                page.close()
                browser.close()

        logging.info("EIS collection method=%s; raw records before filters=%d", method, len(records))
        expired_out = 0
        if demo_include_all:
            relevant = records
            filtered_out = 0
            logging.info("Demo mode enabled (config demo_include_all): business and region filters are bypassed")
        else:
            from ..classifier import classify
            relevant = []
            filtered_out = 0
            for record in records:
                if skip_expired and _is_expired(record.get("deadline", "")):
                    expired_out += 1
                    continue
                result = classify(record, cfg=effective_cfg)
                if wanted_category and result["category"] != wanted_category:
                    filtered_out += 1
                    continue
                if (result["category"] != "Не определено"
                        and result["region_check"] != "Проверить регион"
                        and result["score"] >= medium_threshold):
                    relevant.append(record)
                else:
                    filtered_out += 1

        with session() as con:
            existing_keys = {
                row[0] for row in con.execute(
                    "SELECT DISTINCT canonical_key FROM tenders WHERE canonical_key IS NOT NULL"
                )
            }
        unseen = []
        duplicates = 0
        seen_this_run = set()
        for record in relevant:
            notice_id = str(record.get("source_id") or "").strip()
            key = canonical_key(notice_id, str(record.get("url") or ""))
            if not notice_id or key in existing_keys or key in seen_this_run:
                duplicates += 1
                continue
            seen_this_run.add(key)
            unseen.append(record)

        added, updated, skipped = import_rows(unseen)
        imported = added + updated
        report, _ = export()
        logging.info(
            "EIS summary: method=%s raw=%d filtered_out=%d expired_out=%d matched=%d already_seen=%d added=%d updated=%d skipped=%d report=%s",
            method, len(records), filtered_out, expired_out, len(relevant), duplicates, added, updated, skipped, report,
        )
        if stats is not None:
            stats.update({
                "method": method, "found": len(records),
                "filtered_out": filtered_out, "expired_out": expired_out,
                "matched": len(relevant), "already_seen": duplicates,
                "added": added, "updated": updated, "skipped": skipped,
                "limits": {
                    "region": search_region, "per_page": per_page_eff,
                    "pages": pages_eff, "days": days_eff,
                    "max_cards": effective_cap or "нет", "skip_expired": skip_expired,
                },
            })
        return len(records), imported, report
    except Exception:
        logging.exception("EIS collection failed; method=%s", method)
        raise