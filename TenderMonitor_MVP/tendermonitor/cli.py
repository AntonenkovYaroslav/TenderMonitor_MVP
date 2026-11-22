import argparse, logging, sys
from .config import LOGS, ROOT
from .db import init_db
from .importer import import_csv
from .exporter import export
from .sources.eis import import_package as import_eis_package, inspect_package as inspect_eis_package
from .sources.open_data import import_open_data
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
    me=s.add_parser("monitor-eis", help="Один сбор открытых закупок ЕИС и экспорт Excel")
    me.add_argument("--region", default=None, help="Переопределить регион поиска")
    me.add_argument("--limit", type=int, default=None, help="Общий лимит карточек за запуск (по умолчанию search.max_cards из конфига)")
    me.add_argument("--pages", type=int, default=None, help="Сколько страниц поиска читать (по умолчанию search.max_pages)")
    me.add_argument("--per-page", type=int, default=None, help="Карточек на странице ЕИС: 10/20/50/100/200 (по умолчанию search.records_per_page)")
    me.add_argument("--days", type=int, default=None, help="Окно публикации извещения в днях; 0 — без ограничения (по умолчанию search.published_days)")
    me.add_argument("--keywords", default=None, help="Дополнительные ключевые слова через запятую")
    me.add_argument("--category", default=None, help="Оставить только закупки этого направления")
    sc=s.add_parser("schedule-eis", help="Постоянный запуск мониторинга ЕИС по расписанию (по умолчанию 08:00 и 20:00)")
    sc.add_argument("--no-run-at-start", action="store_true", help="Не выполнять первый сбор сразу при старте")
    sc.add_argument("--no-catch-up", action="store_true", help="Не догонять пропущенные плановые слоты при старте")
    cs=s.add_parser("collect-sources", help="Сбор по всем источникам из config/source_urls.txt (ЕИС, порталы, открытые данные)")
    cs.add_argument("--region", default=None, help="Переопределить регион поиска ЕИС")
    cs.add_argument("--limit", type=int, default=None, help="Общий лимит карточек ЕИС за запуск")
    cs.add_argument("--pages", type=int, default=None, help="Сколько страниц поиска ЕИС читать")
    cs.add_argument("--per-page", type=int, default=None, help="Карточек на странице ЕИС: 10/20/50/100/200")
    cs.add_argument("--days", type=int, default=None, help="Окно публикации извещения в днях; 0 — без ограничения")
    wb=s.add_parser("web", help="Веб-панель администрирования (локальный админ-интерфейс)")
    wb.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания (по умолчанию 127.0.0.1)")
    wb.add_argument("--port", type=int, default=8000, help="Порт (по умолчанию 8000)")
    wb.add_argument("--debug", action="store_true", help="Режим отладки Flask")
    ua=s.add_parser("user-add", help="Создать пользователя веб-панели из консоли")
    ua.add_argument("login")
    ua.add_argument("role", choices=["admin","operator","viewer"])
    ua.add_argument("--password", default=None, help="Пароль (иначе создаётся случайный)")
    a=p.parse_args()
    if a.cmd=="init":
        init_db(); print("База данных готова.")
    elif a.cmd=="import-csv":
        added,updated,sk=import_csv(a.file); print(f"Добавлено: {added}; обновлено: {updated}; пропущено: {sk}.")
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
        from .sources.eis_browser import run_once as run_eis_browser
        print("Сбор открытых закупок ЕИС запущен. Обычно занимает 1-2 минуты; не закрывайте окно.", flush=True)
        try:
            found, imported, report = run_eis_browser(
                region=a.region, limit=a.limit, keywords=a.keywords, category=a.category,
                pages=a.pages, per_page=a.per_page, days=a.days,
            )
        except Exception:
            logging.exception("monitor-eis command failed")
            raise
        print(f"Карточек ЕИС проверено: {found}; новых подходящих добавлено/обновлено: {imported}; Excel: {report}")
    elif a.cmd=="schedule-eis":
        from .sources.eis_browser import run_once as run_eis_browser
        run_schedule(
            run_eis_browser,
            run_at_start=False if a.no_run_at_start else None,
            catch_up=False if a.no_catch_up else None,
        )
    elif a.cmd=="collect-sources":
        from .sources.registry import collect_all
        print("Сбор по источникам из config/source_urls.txt запущен.", flush=True)
        results, report = collect_all(
            region=a.region, limit=a.limit, pages=a.pages, per_page=a.per_page, days=a.days,
        )
        for r in results:
            print(f"[{r['status']}] {r['label']} ({r['kind']}): найдено={r['found']} "
                  f"импортировано={r['imported']} отфильтровано={r['filtered']} "
                  f"пропущено={r['skipped']} — {r['detail']}")
        ok = sum(1 for r in results if r["status"] == "ok")
        print(f"Источников обработано: {len(results)}; успешно: {ok}; Excel: {report}")
    elif a.cmd=="web":
        from .web import main as web_main
        web_main(["--host", a.host, "--port", str(a.port)] + (["--debug"] if a.debug else []))
    elif a.cmd=="user-add":
        from .web import add_user
        password, error = add_user(a.login, a.role, a.password)
        if error:
            print(f"Ошибка: {error}")
        else:
            print(f"Пользователь {a.login} создан ({a.role}). Пароль: {password}")
if __name__=="__main__": main()
