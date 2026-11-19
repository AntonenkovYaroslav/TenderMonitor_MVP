from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from .db import session, init_db
from .config import REPORTS

HEAD = [
    "Дата обнаружения", "Направление", "Название", "НМЦК, ₽",
    "Адрес объекта", "Заказчик", "Организатор закупки",
    "Дата окончания подачи", "Время окончания подачи",
    "Контактное лицо", "Телефон", "E-mail", "Релевантность",
    "Баллы", "Регион", "Проверка региона", "Ссылка на ЕИС", "Описание"
]

def export():
    init_db()
    with session() as con:
        rows = con.execute("""
            SELECT first_seen, category, title, price,
                   COALESCE(NULLIF(object_address,''), address) AS export_address,
                   customer, organizer, deadline, deadline_time,
                   contact_name, contact_phone, contact_email,
                   relevance, score, region, region_check, url, description
            FROM tenders AS current
            WHERE current.id = (
                SELECT MAX(previous.id) FROM tenders AS previous
                WHERE previous.source_id = current.source_id
            )
            ORDER BY score DESC, deadline
        """).fetchall()

    REPORTS.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    sheets = [
        ("Все закупки", lambda r: True),
        ("Высокая", lambda r: r["relevance"] == "Высокая"),
        ("Средняя", lambda r: r["relevance"] == "Средняя"),
        ("Проверить регион", lambda r: r["region_check"] == "Проверить регион"),
    ]

    for idx, (name, predicate) in enumerate(sheets):
        ws = wb.active if idx == 0 else wb.create_sheet()
        ws.title = name
        ws.append(HEAD)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for row in rows:
            if predicate(row):
                ws.append(list(row))
                link_cell = ws.cell(ws.max_row, 17)
                if link_cell.value:
                    link_cell.hyperlink = link_cell.value
                    link_cell.style = "Hyperlink"
        widths = [20, 23, 52, 17, 34, 32, 32, 22, 18, 22, 19, 27, 16, 10, 23, 23, 48, 65]
        for i, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 34

    path = REPORTS / "tenders_report.xlsx"
    wb.save(path)
    return path, len(rows)
