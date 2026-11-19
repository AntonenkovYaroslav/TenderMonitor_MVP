import argparse, logging, sys
from .config import LOGS, ROOT
from .db import init_db
from .importer import import_csv
from .exporter import export
from .sources.eis import import_package as import_eis_package, inspect_package as inspect_eis_package
from .sources.open_data import import_open_data
from .sources.eis_browser import run_once as run_eis_browser
from .scheduler import run_schedule
def main():
    LOGS.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOGS/"app.log", encoding="utf-8"),
            logging.FileHandler(ROOT/"last_run.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    p=argparse.ArgumentParser(prog="Tender Monitor")
    s=p.add_subparsers(dest="cmd",required=True)
    s.add_parser("init")
    i=s.add_parser("import-csv"); i.add_argument("file")
    s.add_parser("export")
    ei=s.add_parser("inspect-eis", help="Проверить XML/ZIP ЕИС")
    ei.add_argument("file")
    ee=s.add_parser("import-eis", help="Импортировать XML/ZIP ЕИС")
    ee.add_argument("file")
    od=s.add_parser("import-eis-open-data", help="Загрузить CSV/ZIP из открытых данных ЕИС")
    od.add_argument("source", help="Прямая ссылка на публичный CSV/ZIP или локальный файл")
    s.add_parser("monitor-eis", help="Один сбор открытых закупок ЕИС и экспорт Excel")
    s.add_parser("schedule-eis", help="Постоянный запуск мониторинга ЕИС ежедневно в 08:00 и 20:00")
    a=p.parse_args()
    if a.cmd=="init":
        init_db(); print("База данных готова.")
    elif a.cmd=="import-csv":
        n,sk=import_csv(a.file); print(f"Импортировано/обработано строк: {n}; пропущено: {sk}.")
    elif a.cmd=="export":
        path,n=export(); print(f"Excel создан: {path}\nВсего записей: {n}")
    elif a.cmd=="inspect-eis":
        count, errors, preview = inspect_eis_package(a.file)
        print(f"Распознано документов: {count}")
        for item in preview:
            print(item)
        for error in errors[:30]:
            print("ПРЕДУПРЕЖДЕНИЕ:", error)
    elif a.cmd=="import-eis":
        imported, parsed, errors = import_eis_package(a.file)
        print(f"Обработано XML: {parsed}; добавлено/обновлено: {imported}; ошибок: {len(errors)}")
        for error in errors[:30]:
            print("ПРЕДУПРЕЖДЕНИЕ:", error)
    elif a.cmd=="import-eis-open-data":
        imported, skipped, parsed = import_open_data(a.source)
        print(f"Строк открытых данных: {parsed}; добавлено/обновлено: {imported}; пропущено: {skipped}")
    elif a.cmd=="monitor-eis":
        try:
            found, imported, report = run_eis_browser()
        except Exception:
            logging.exception("monitor-eis command failed")
            raise
        print(f"Карточек ЕИС проверено: {found}; новых подходящих добавлено/обновлено: {imported}; Excel: {report}")
    elif a.cmd=="schedule-eis":
        run_schedule(run_eis_browser)
if __name__=="__main__": main()
