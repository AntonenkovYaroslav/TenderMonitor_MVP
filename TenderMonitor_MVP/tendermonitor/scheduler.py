"""Simple local-time scheduler for two daily EIS monitoring runs."""
from __future__ import annotations

from datetime import datetime, time, timedelta
import logging
import time as sleep_time
from collections.abc import Callable


RUN_TIMES = (time(8, 0), time(20, 0))


def run_schedule(run_once: Callable[[], tuple[int, int, object]]) -> None:
    """Run monitoring every day at 08:00 and 20:00 in the machine's local timezone."""
    logging.info("EIS schedule started; daily runs at %s and %s local time", *RUN_TIMES)
    while True:
        now = datetime.now().astimezone()
        candidates = [now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0) for t in RUN_TIMES]
        next_run = next((candidate for candidate in candidates if candidate > now), None)
        if next_run is None:
            tomorrow = (now + timedelta(days=1)).date()
            next_run = datetime.combine(tomorrow, RUN_TIMES[0], tzinfo=now.tzinfo)

        seconds = max(1, int((next_run - now).total_seconds()))
        print(f"Следующий сбор ЕИС: {next_run:%d.%m.%Y %H:%M} (местное время). Для остановки нажмите Ctrl+C.", flush=True)
        sleep_time.sleep(seconds)
        try:
            found, imported, report = run_once()
            print(f"ЕИС: карточек {found}; добавлено/обновлено {imported}; Excel: {report}", flush=True)
        except Exception:
            logging.exception("Scheduled EIS run failed")
            print("Сбор ЕИС завершился ошибкой; подробности записаны в logs\\app.log.", flush=True)
