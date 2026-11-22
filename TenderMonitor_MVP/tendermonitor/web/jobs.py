"""Фоновые задания веб-панели.

Сбор и экспорт запускаются отдельным процессом (`python -m tendermonitor.cli ...`),
а не в потоке веб-сервера: так панель не блокируется даже на несколько минут,
сбой сбора не роняет сервер, а параметры запуска и лог остаются теми же,
что у консольной команды. Статус и вывод задания хранятся в `data/app.db`
и в файле `logs/web_jobs/job_<id>.log`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from ..config import LOGS, ROOT
from . import appdb

JOB_LOGS = LOGS / "web_jobs"
COLLECT_KIND = "collect"
EXPORT_KIND = "export"

_lock = threading.Lock()


def _log_path(job_id: int) -> Path:
    JOB_LOGS.mkdir(parents=True, exist_ok=True)
    return JOB_LOGS / f"job_{job_id}.log"


def read_log_tail(job: dict, limit: int = 200) -> str:
    path = job.get("log_path") or ""
    if not path:
        return ""
    file = Path(path)
    if not file.exists():
        return ""
    try:
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


def _build_command(kind: str, params: dict) -> list[str]:
    cmd = [sys.executable, "-m", "tendermonitor.cli"]
    if kind == COLLECT_KIND:
        cmd.append("monitor-eis")
        region = (params.get("region") or "").strip()
        limit = params.get("limit")
        keywords = (params.get("keywords") or "").strip()
        category = (params.get("category") or "").strip()
        if region:
            cmd += ["--region", region]
        if limit:
            cmd += ["--limit", str(int(limit))]
        for key, flag in (("pages", "--pages"), ("per_page", "--per-page")):
            value = params.get(key)
            if value:
                cmd += [flag, str(int(value))]
        # days=0 — осмысленное значение («без окна публикации»), его нельзя
        # отбрасывать как falsy, иначе панель молча вернёт окно из конфига.
        days = params.get("days")
        if days is not None and str(days) != "":
            cmd += ["--days", str(int(days))]
        if keywords:
            cmd += ["--keywords", keywords]
        if category:
            cmd += ["--category", category]
    else:
        cmd.append("export")
    return cmd


def _run(job_id: int, cmd: list[str], log_file: Path):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    appdb.update_job(job_id, status="running", started_at=appdb.now_iso())
    exit_code = None
    with log_file.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        try:
            process = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            appdb.update_job(job_id, status="failed", finished_at=appdb.now_iso(),
                             summary=f"Не удалось запустить процесс: {exc}")
            return
        appdb.update_job(job_id, pid=process.pid)
        for line in process.stdout:
            log.write(line)
            log.flush()
        process.wait()
        exit_code = process.returncode
    summary = "Задание выполнено" if exit_code == 0 else f"Задание завершилось с кодом {exit_code}"
    appdb.update_job(job_id, status="done" if exit_code == 0 else "failed",
                     finished_at=appdb.now_iso(), exit_code=exit_code, summary=summary)


def start_job(kind: str, params: dict, user_login: str) -> tuple[int | None, str]:
    """Создаёт задание и запускает его в фоне. Возвращает (id, ошибка)."""
    with _lock:
        if appdb.running_job() is not None:
            return None, "Уже выполняется другое задание. Дождитесь его завершения."
        job_id = appdb.create_job(kind, params, user_login)
        log_file = _log_path(job_id)
        appdb.update_job(job_id, log_path=str(log_file))
        cmd = _build_command(kind, params)
        thread = threading.Thread(target=_run, args=(job_id, cmd, log_file), daemon=True)
        thread.start()
        return job_id, ""


def cancel_job(job_id: int) -> str:
    job = appdb.get_job(job_id)
    if not job or job["status"] not in ("queued", "running"):
        return "Задание уже завершено."
    pid = job.get("pid")
    if pid:
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                import os
                import signal
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError as exc:
            return f"Не удалось остановить процесс: {exc}"
    appdb.update_job(job_id, status="cancelled", finished_at=appdb.now_iso(), summary="Остановлено вручную")
    return "Задание остановлено."
