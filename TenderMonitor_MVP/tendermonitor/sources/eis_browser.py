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

from ..db import session, init_db
from ..exporter import export
from ..importer import import_rows

SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?recordsPerPage=_50"
NOTICE_PATH = re.compile(r"/epz/order/notice/[^\"'?#]+/view/common-info\.html", re.I)
RSS_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
# Temporary demonstration mode: import all collected notices, including notices
# that do not match the configured business keywords or current stage.
DEMO_INCLUDE_ALL = True

def _launch_browser(playwright):
    """Prefer the Windows Edge installation; install Playwright Chromium only if Edge is unavailable."""
    try:
        return playwright.chromium.launch(
            channel="msedge", headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--ignore-certificate-errors"],
        )
    except PlaywrightError as edge_error:
        logging.info("Microsoft Edge channel unavailable; installing Playwright Chromium: %s", edge_error)
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )
        return playwright.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--ignore-certificate-errors"],
        )

def _read_rss_records() -> list[dict]:
    """Read the EIS public search feed and normalize entries for the existing importer."""
    query = urllib.parse.urlencode({
        "searchString": "Калининградская область",
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
        price_match = re.search(r"\d[\d\s\u00a0]*(?:[,.]\d{1,2})?", raw_price)
        price = re.sub(r"[\s\u00a0]", "", price_match.group(0)).replace(",", ".") if price_match else ""
        customer = labelled("Заказчик", "Наименование заказчика")
        deadline = labelled("Дата окончания подачи заявок", "Окончание подачи заявок", "Срок подачи заявок", "Дата и время окончания срока подачи заявок")
        deadline_match = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?", deadline)
        deadline_date = deadline_match.group(1) if deadline_match else ""
        deadline_time = deadline_match.group(2) if deadline_match and deadline_match.group(2) else ""
        records.append({
            "source_id": source_match.group(0), "title": title,
            "price": price, "address": "", "object_address": "",
            "customer": customer, "organizer": "", "deadline": deadline_date, "deadline_time": deadline_time,
            "contact_name": "", "contact_phone": "", "contact_email": "",
            "url": link, "region": "Калининградская область",
            "description": description[:5000], "published_at": value("pubDate") or value("published"),
        })
    return records

def _extract_records(page) -> list[dict]:
    # Search broadly for the region phrase. Relevance is determined by the existing company classifier.
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    search = page.locator("input:not([type=hidden])").first
    search.fill("Калининградская область")
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
            def next_value(*labels: str) -> str:
                for idx, line in enumerate(lines[:-1]):
                    if any(label.lower() in line.lower() for label in labels):
                        value = lines[idx + 1]
                        if not any(label.lower() in value.lower() for label in labels):
                            return value
                return ""
            reg_match = re.search(r"(?:№|номер извещения)\s*([0-9]{19})", text, re.I)
            if not reg_match:
                reg_match = re.search(r"(?:regNumber|regnumber)=([0-9]{19})", href, re.I)
            if not reg_match:
                continue
            title_value = next_value("Наименование объекта закупки", "Объект закупки") or title
            price_value = next_value("Начальная (максимальная) цена контракта", "Начальная цена")
            price_match = re.search(r"([\d\s\u00a0]+[,\.]\d{2})", price_value)
            deadline_value = next_value("Дата и время окончания срока подачи заявок", "Окончание подачи заявок")
            deadline_match = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*(\d{2}:\d{2})?", deadline_value)
            customer = next_value("Наименование заказчика", "Заказчик")
            organizer = next_value("Организация, осуществляющая размещение")
            contact_name = next_value("Ответственное должностное лицо")
            contact_email = next_value("Адрес электронной почты")
            contact_phone = next_value("Номер контактного телефона")
            address = next_value("Адрес места нахождения", "Место нахождения")
            ikz = next_value("Идентификационный код закупки")
            records.append({
                "source_id": reg_match.group(1), "title": title_value,
                "price": re.sub(r"[\s\u00a0]", "", price_match.group(1)).replace(",", ".") if price_match else "",
                "address": address, "object_address": address,
                "customer": customer, "organizer": organizer,
                "deadline": deadline_match.group(1) if deadline_match else "",
                "deadline_time": deadline_match.group(2) if deadline_match and deadline_match.group(2) else "",
                "contact_name": contact_name, "contact_phone": contact_phone, "contact_email": contact_email,
                "url": href, "region": "Калининградская область", "description": (f"ИКЗ: {ikz}. " if ikz else "") + text[:5000],
                "published_at": "",
            })
        finally:
            record_page.close()
    return records

def run_once() -> tuple[int, int, Path]:
    """Collect EIS once, keep only unseen matching notices, and update the fixed workbook."""
    init_db()
    method = "Edge"
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
            page = context.new_page()
            try:
                try:
                    records = _extract_records(page)
                except PlaywrightError as exc:
                    method = "RSS"
                    logging.warning("Edge collection failed; using EIS RSS: %s", exc)
                    records = _read_rss_records()
                if not records and method != "RSS":
                    method = "RSS"
                    logging.warning("Edge returned no raw records; using EIS RSS")
                    records = _read_rss_records()
            finally:
                page.close()
                browser.close()

        logging.info("EIS collection method=%s; raw records before filters=%d", method, len(records))
        if DEMO_INCLUDE_ALL:
            relevant = records
            filtered_out = 0
            logging.info("Demo mode enabled: business and region filters are bypassed")
        else:
            from ..classifier import classify
            relevant = []
            filtered_out = 0
            for record in records:
                result = classify(record)
                if result["category"] != "Не определено" and result["region_check"] == "Калининградская область":
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
