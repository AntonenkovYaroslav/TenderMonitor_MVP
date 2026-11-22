"""Local-time scheduler for daily EIS monitoring runs.

Поведение по умолчанию (переопределяется в config/keywords.json, блок "schedule",
и флагами CLI schedule-eis):
- первый сбор выполняется сразу при старте, не дожидаясь ближайшего слота;
- если компьютер спал или был выключен в плановое время, пропущенные слоты
  догоняются одним сбором при старте (catch-up), а сон во время работы
  обрабатывается короткими циклами сна: после пробуждения сбор идёт сразу.

Время последнего успешного сбора хранится в data/schedule_state.json.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
import json
import logging
import time as sleep_time
from collections.abc import Callable

from .config import DATA, settings


RUN_TIMES = (time(8, 0), time(20, 0))
STATE_FILE = DATA / "schedule_state.json"
# Шаг сна: после пробуждения компьютера оставшееся время пересчитывается,
# поэтому плановый слот не «проскакивает» из-за длинного sleep.
_SLEEP_STEP = 30


def _parse_times(values) -> tuple[time, ...]:
    """Список строк 'ЧЧ:ММ' из конфига; некорректные значения пропускаются."""
    parsed = []
    for value in values or []:
        try:
            parsed.append(datetime.strptime(str(value).strip(), "%H:%M").time())
        except ValueError:
            logging.warning("Invalid schedule time %r in config; skipped", value)
    if not parsed:
        return RUN_TIMES
    return tuple(sorted(set(parsed)))


def _next_run(now: datetime, times: tuple[time, ...]) -> datetime:
    candidates = [now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0) for t in times]
    for candidate in candidates:
        if candidate > now:
            return candidate
    tomorrow = (now + timedelta(days=1)).date()
    return datetime.combine(tomorrow, times[0], tzinfo=now.tzinfo)


def _missed_slots(last: datetime | None, now: datetime, times: tuple[time, ...]) -> int:
    """Сколько плановых слотов прошло после последнего успешного сбора."""
    if last is None or now <= last:
        return 0
    if (now.date() - last.date()).days > 400:
        # Очень старое состояние: точный подсчёт не нужен, достаточно факта пропуска.
        return 1
    missed = 0
    day = last.date()
    while day <= now.date():
        for t in times:
            slot = datetime.combine(day, t, tzinfo=now.tzinfo)
            if last < slot <= now:
                missed += 1
        day += timedelta(days=1)
    return missed


def _read_last_run() -> datetime | None:
    try:
        with STATE_FILE.open(encoding="utf-8") as f:
            value = datetime.fromisoformat(json.load(f)["last_run"])
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_last_run(moment: datetime) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump({"last_run": moment.isoformat()}, f, ensure_ascii=False)
    except OSError:
        logging.exception("Could not write schedule state file %s", STATE_FILE)


def run_schedule(
    run_once: Callable[[], tuple[int, int, object]],
    *,
    run_at_start: bool | None = None,
    catch_up: bool | None = None,
) -> None:
    """Run monitoring daily at the configured local times (default 08:00/20:00).

    None означает «взять из конфига»; явные True/False (флаги CLI) имеют приоритет.
    """
    schedule_cfg = settings().get("schedule") or {}
    times = _parse_times(schedule_cfg.get("times"))
    if run_at_start is None:
        run_at_start = bool(schedule_cfg.get("run_at_start", True))
    if catch_up is None:
        catch_up = bool(schedule_cfg.get("catch_up", True))

    logging.info(
        "EIS schedule started; times=%s run_at_start=%s catch_up=%s",
        [t.strftime("%H:%M") for t in times], run_at_start, catch_up,
    )
    print(
        f"Расписание ЕИС: {', '.join(t.strftime('%H:%M') for t in times)} (местное время). "
        f"Первый сбор при старте: {'да' if run_at_start else 'нет'}; "
        f"catch-up пропущенных слотов: {'да' if catch_up else 'нет'}.",
        flush=True,
    )

    def do_run(reason: str) -> None:
        logging.info("Scheduled EIS run started (%s)", reason)
        print(f"Выполняю сбор ЕИС ({reason}). Обычно занимает 1-2 минуты.", flush=True)
        try:
            found, imported, report = run_once()
            _write_last_run(datetime.now().astimezone())
            print(f"ЕИС: карточек {found}; добавлено/обновлено {imported}; Excel: {report}", flush=True)
        except Exception:
            logging.exception("Scheduled EIS run failed")
            print("Сбор ЕИС завершился ошибкой; подробности записаны в logs\\app.log.", flush=True)

    if run_at_start:
        do_run("первый сбор при старте")
    elif catch_up:
        missed = _missed_slots(_read_last_run(), datetime.now().astimezone(), times)
        if missed:
            print(f"Пропущено плановых слотов: {missed}. Выполняю один catch-up сбор.", flush=True)
            do_run("catch-up пропущенных слотов")

    while True:
        now = datetime.now().astimezone()
        next_run = _next_run(now, times)
        print(f"Следующий сбор ЕИС: {next_run:%d.%m.%Y %H:%M} (местное время). Для остановки нажмите Ctrl+C.", flush=True)
        while True:
            remaining = (next_run - datetime.now().astimezone()).total_seconds()
            if remaining <= 0:
                break
            sleep_time.sleep(min(remaining, _SLEEP_STEP))
        do_run("плановый сбор")