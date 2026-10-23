"""Child job tools — delegate short coding tasks to ocbot (OpenCode on free models) and supervise them.

Flow: child_delegate pushes the base branch of a local repo into an exchange
repository and creates a job; ocbot works in budgeted legs; child_watch wakes
Veles with a child_report task when the job ends; Veles checks the result
(child_report / child_diff / child_accept verify_only) and accepts, sends a
correction (child_followup) or rejects it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ouroboros import child_api
from ouroboros.tools.registry import ToolContext, ToolEntry

NAME = re.compile(r"[a-z0-9-]{1,40}\Z")
VERIFY_TIMEOUT_SEC = 600
TAIL_CHARS = 3000
CHILD_TOOL_NAMES = (
    "child_delegate", "child_jobs", "child_status", "child_report", "child_diff",
    "child_followup", "child_stop", "child_accept", "child_reject",
)


def _state_path(ctx: ToolContext) -> Path:
    return Path(ctx.drive_root) / "state" / "child_jobs.json"


def _load_links(ctx: ToolContext) -> Dict[str, Dict[str, str]]:
    try:
        return json.loads(_state_path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_link(ctx: ToolContext, job_id: str, link: Dict[str, str]) -> None:
    links = _load_links(ctx)
    links[job_id] = link
    path = _state_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])}: {(proc.stderr or proc.stdout).strip()[-600:]}")
    return proc.stdout.strip()


def _trust(path: Path) -> None:
    """Root pushes into ocbot-owned repos; git requires an explicit global trust entry."""
    proc = subprocess.run(["git", "config", "--global", "--get-all", "safe.directory"], capture_output=True, text=True)
    if str(path) not in proc.stdout.split("\n"):
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(path)], check=True, capture_output=True)


def _err(exc: Exception) -> str:
    if isinstance(exc, child_api.ChildApiError):
        return f"⚠️ ocbot: {exc}"
    return f"⚠️ {type(exc).__name__}: {exc}"


def _summary_line(job: Dict[str, Any]) -> str:
    leg = job.get("leg") or {}
    parts = [
        f"J{job['num']} [{job['job_id']}] {job['state']}",
        f"«{job['title']}»",
        f"заход {job['legs_used']}/{job['max_legs']}",
    ]
    if job["state"] in ("running", "checkpointing"):
        parts.append(f"шаги {leg.get('steps', 0)}/{leg.get('max_steps', 0)}, контекст {leg.get('context_used', 0)}/{leg.get('context_limit', 0)}")
    if job.get("verify"):
        parts.append(f"проверка exit={job['verify'].get('exit')}")
    if job.get("reason"):
        parts.append("причина: " + job["reason"][:300])
    if job.get("stopped_by"):
        parts.append("остановил: " + job["stopped_by"])
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _child_delegate(ctx: ToolContext, repo: str, task: str, title: str = "", acceptance: list = None,
                    verify_cmd: str = "", project: str = "", base: str = "", model: str = "",
                    max_legs: int = 0, parent_ref: str = "") -> str:
    try:
        repo_path = Path(repo).expanduser().resolve()
        if not (repo_path / ".git").exists():
            return f"⚠️ {repo_path} не git-репозиторий"
        base = base or _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        if base == "HEAD":
            return "⚠️ HEAD отсоединён: укажи base (ветку)"
        _git(repo_path, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
        name = project or re.sub(r"[^a-z0-9-]+", "-", repo_path.name.lower()).strip("-")[:40]
        if not NAME.fullmatch(name):
            return "⚠️ project: только [a-z0-9-], 1–40 символов"
        dirty = bool(_git(repo_path, "status", "--porcelain"))
        child_api.request("POST", "/exchanges", body={"name": name})
        exchange = child_api.exchange_dir() / f"{name}.git"
        _trust(exchange)
        _git(repo_path, "push", "--quiet", "--force", str(exchange), f"refs/heads/{base}:refs/heads/{base}", timeout=300)
        body: Dict[str, Any] = {
            "title": title, "task": task, "acceptance": [a for a in (acceptance or []) if str(a).strip()],
            "source": {"exchange": name, "base": base}, "verify_cmd": verify_cmd, "parent_ref": parent_ref,
        }
        if model:
            body["model"] = model
        if max_legs:
            body["budget"] = {"max_legs": int(max_legs)}
        job = child_api.request("POST", "/jobs", body=body,
                                headers={"Idempotency-Key": f"veles-{uuid.uuid4().hex}"})["job"]
        _save_link(ctx, job["job_id"], {"repo": str(repo_path), "base": base, "exchange": name})
        note = "\n⚠️ В репозитории есть незакоммиченные изменения — младший их не видит." if dirty else ""
        return (f"✅ Делегировано младшему: {_summary_line(job)}\n"
                f"База: {base} @ {job['base_ref'][:10]}. Жди child_report — ocbot разбудит, когда закончит.{note}")
    except Exception as exc:
        return _err(exc)


def _child_jobs(ctx: ToolContext, state: str = "", origin: str = "") -> str:
    try:
        jobs = child_api.request("GET", "/jobs", query={"state": state, "origin": origin})["jobs"]
    except Exception as exc:
        return _err(exc)
    if not jobs:
        return "Задач нет."
    return "\n".join(_summary_line(j) + (" (владелец)" if j["origin"] == "owner" else "") for j in jobs[-30:])


def _child_status(ctx: ToolContext, job_id: str) -> str:
    try:
        return _summary_line(child_api.request("GET", f"/jobs/{job_id}")["job"])
    except Exception as exc:
        return _err(exc)


def _child_report(ctx: ToolContext, job_id: str) -> str:
    try:
        data = child_api.request("GET", f"/jobs/{job_id}/report")
    except Exception as exc:
        return _err(exc)
    verify = data.get("verify") or {}
    command = verify.get("cmd") or data["job"].get("verify_cmd") or ""
    if not command:
        verify_line = "Проверка ocbot: не задана"
    elif "exit" not in verify:
        verify_line = f"Проверка ocbot: {command} → ещё не запускалась (задача не дошла до проверки)"
    else:
        verify_line = f"Проверка ocbot: {command} → exit {verify['exit']}"
    lines = [
        _summary_line(data["job"]),
        "",
        "REPORT.md:\n" + ((data.get("report") or "—")[:4000]),
        "",
        "HANDOFF.md:\n" + ((data.get("handoff") or "—")[:1500]),
        "",
        verify_line,
        (verify.get("tail") or "")[-1500:],
        "",
        "Коммиты:\n" + (data.get("commits") or "—"),
        "",
        "Diff-stat:\n" + (data.get("diff_stat") or "—")[-3000:],
    ]
    return "\n".join(lines)


def _child_diff(ctx: ToolContext, job_id: str, path: str = "", offset: int = 0, limit: int = 20000) -> str:
    try:
        data = child_api.request("GET", f"/jobs/{job_id}/diff",
                                 query={"path": path, "offset": int(offset), "limit": max(1, min(int(limit), 60000))})
    except Exception as exc:
        return _err(exc)
    more = f"\n… ещё {data['size'] - data['next']} символов: offset={data['next']}" if data["next"] < data["size"] else ""
    return (data["text"] or "(пусто)") + more


def _child_followup(ctx: ToolContext, job_id: str, feedback: str) -> str:
    try:
        job = child_api.request("POST", f"/jobs/{job_id}/followup", body={"text": feedback})["job"]
        return "✅ Поправка отправлена, новый заход в очереди: " + _summary_line(job)
    except Exception as exc:
        return _err(exc)


def _child_stop(ctx: ToolContext, job_id: str) -> str:
    try:
        return "⏹ " + _summary_line(child_api.request("POST", f"/jobs/{job_id}/stop", body={})["job"])
    except Exception as exc:
        return _err(exc)


def _child_reject(ctx: ToolContext, job_id: str, reason: str) -> str:
    try:
        job = child_api.request("POST", f"/jobs/{job_id}/close", body={"resolution": "rejected", "comment": reason})["job"]
        return "❌ Отклонено: " + _summary_line(job)
    except Exception as exc:
        return _err(exc)


def _verify_env(home: str) -> Dict[str, str]:
    # Child code never sees Veles's secrets: a minimal environment, not a filtered copy.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}


def _run_verify(worktree: Path, command: str, user: str) -> Dict[str, Any]:
    argv = ["sh", "-c", command]
    if user and os.geteuid() == 0:
        import pwd  # POSIX only; imported here so the module loads everywhere
        info = pwd.getpwnam(user)
        for root, dirs, files in os.walk(worktree):
            for name in [*dirs, *files]:
                os.lchown(os.path.join(root, name), info.pw_uid, info.pw_gid)
        os.chown(worktree, info.pw_uid, info.pw_gid)
        argv = ["runuser", "-u", user, "--", *argv]
    try:
        proc = subprocess.run(argv, cwd=str(worktree), env=_verify_env(str(worktree)), capture_output=True,
                              text=True, timeout=VERIFY_TIMEOUT_SEC, start_new_session=True)
        return {"exit": proc.returncode, "tail": ((proc.stdout or "") + (proc.stderr or ""))[-TAIL_CHARS:]}
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        return {"exit": 124, "tail": out.decode("utf-8", "replace")[-TAIL_CHARS:] if isinstance(out, bytes) else str(out)[-TAIL_CHARS:]}


def _child_accept(ctx: ToolContext, job_id: str, verify_only: bool = False, verify_cmd: str = "",
                  merge: bool = True, force: bool = False) -> str:
    try:
        job = child_api.request("GET", f"/jobs/{job_id}")["job"]
        link = _load_links(ctx).get(job["job_id"])
        if not link:
            return "⚠️ Эта задача создана не через child_delegate (нет связи с репозиторием)."
        if job["state"] != "done" and not force:
            return f"⚠️ Задача в состоянии {job['state']}, а не done. Используй force=true, если уверен."
        repo = Path(link["repo"])
        exchange = child_api.exchange_dir() / f"{link['exchange']}.git"
        _trust(exchange)
        ref = f"refs/remotes/child/J{job['num']}"
        _git(repo, "fetch", "--quiet", str(exchange), f"+refs/heads/{job['branch']}:{ref}", timeout=300)
        head = _git(repo, "rev-parse", ref)
        command = verify_cmd or job.get("verify_cmd") or ""
        result = {"exit": 0, "tail": "проверка не задана"}
        if command:
            tmp = Path(tempfile.mkdtemp(prefix=f"veles-child-J{job['num']}-"))
            work = tmp / "work"
            try:
                _git(repo, "worktree", "add", "--quiet", "--detach", str(work), head)
                os.chmod(tmp, 0o755)
                result = _run_verify(work, command, os.environ.get("VELES_CHILD_VERIFY_USER", "nobody").strip())
            finally:
                _git(repo, "worktree", "remove", "--force", str(work), check=False)
                shutil.rmtree(tmp, ignore_errors=True)
                _git(repo, "worktree", "prune", check=False)
        verdict = f"Проверка у Велеса: {command or '—'} → exit {result['exit']}\n{result['tail'][-1500:]}"
        if verify_only:
            return f"🔎 J{job['num']} @ {head[:10]}\n{verdict}"
        if result["exit"] != 0:
            return f"⚠️ Не принято: проверка упала.\n{verdict}\nДай поправку через child_followup или отклони."
        merged = ""
        if merge:
            if _git(repo, "status", "--porcelain"):
                return f"⚠️ В {repo} есть незакоммиченные изменения — слияние не выполнено.\n{verdict}"
            current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if current != link["base"]:
                return f"⚠️ Текущая ветка {current}, а задача от {link['base']} — переключись и повтори.\n{verdict}"
            proc = subprocess.run(["git", "-C", str(repo), "-c", "user.name=Veles", "-c", "user.email=veles@users.noreply.github.com",
                                   "merge", "--no-ff", "-m", f"merge child J{job['num']}: {job['title'][:80]}", ref],
                                  capture_output=True, text=True, timeout=120)
            if proc.returncode:
                _git(repo, "merge", "--abort", check=False)
                return f"⚠️ Конфликт слияния, репозиторий возвращён как был.\n{(proc.stderr or proc.stdout)[-800:]}"
            merged = _git(repo, "rev-parse", "HEAD")
        closed = child_api.request("POST", f"/jobs/{job['job_id']}/close", body={
            "resolution": "accepted", "comment": f"accepted by Veles @ {head[:10]}" + (f", merged {merged[:10]}" if merged else "")})["job"]
        return (f"✅ Принято: {_summary_line(closed)}\n{verdict}"
                + (f"\nСлито в {link['base']}: {merged[:10]} (push не делал)." if merged else ""))
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _schema(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


_JOB_ID = {"type": "string", "description": "job_id (job_…) или номер задачи"}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("child_delegate", _schema(
            "child_delegate",
            "Делегировать короткую задачу по коду младшему агенту ocbot (OpenCode, бесплатные модели). "
            "Пушит базовую ветку локального репозитория в exchange и ставит задачу; не ждёт результата — "
            "по завершении придёт задача child_report. Размер задачи: 20–30 минут работы, проверяемые критерии.",
            {
                "repo": {"type": "string", "description": "Абсолютный путь к локальному git-репозиторию"},
                "task": {"type": "string", "description": "Короткое ТЗ: что сделать, где, ограничения"},
                "title": {"type": "string", "description": "Заголовок задачи (одна строка)"},
                "acceptance": {"type": "array", "items": {"type": "string"}, "description": "Проверяемые критерии приёмки"},
                "verify_cmd": {"type": "string", "description": "Команда проверки, например 'python3 -m pytest -q'"},
                "project": {"type": "string", "description": "Имя exchange/проекта [a-z0-9-]; по умолчанию из имени каталога"},
                "base": {"type": "string", "description": "Базовая ветка; по умолчанию текущая"},
                "model": {"type": "string", "description": "Модель OpenCode, например opencode/deepseek-v4.1-flash-free"},
                "max_legs": {"type": "integer", "description": "Максимум заходов (1–8), по умолчанию 4"},
                "parent_ref": {"type": "string", "description": "Ссылка на шаг плана, например plan:<id>/step:<n>"},
            },
            ["repo", "task"]), _child_delegate, timeout_sec=360),
        ToolEntry("child_jobs", _schema(
            "child_jobs", "Список задач младшего (свои и владельца) с состоянием.",
            {"state": {"type": "string", "description": "Фильтр состояния (done, needs_attention, running, …)"},
             "origin": {"type": "string", "description": "veles или owner"}},
            []), _child_jobs, timeout_sec=30),
        ToolEntry("child_status", _schema("child_status", "Короткое состояние задачи младшего.", {"job_id": _JOB_ID}, ["job_id"]),
                  _child_status, timeout_sec=30),
        ToolEntry("child_report", _schema(
            "child_report", "Отчёт младшего по задаче: REPORT.md, HANDOFF.md, его проверка, коммиты, diff-stat.",
            {"job_id": _JOB_ID}, ["job_id"]), _child_report, timeout_sec=30),
        ToolEntry("child_diff", _schema(
            "child_diff", "Diff задачи младшего постранично (по умолчанию 20000 символов).",
            {"job_id": _JOB_ID, "path": {"type": "string", "description": "Ограничить файлом/каталогом"},
             "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["job_id"]), _child_diff, timeout_sec=30),
        ToolEntry("child_followup", _schema(
            "child_followup", "Поправка младшему: конкретные замечания → новый заход в той же задаче. Не больше 2 раз на задачу.",
            {"job_id": _JOB_ID, "feedback": {"type": "string", "description": "Что именно исправить и как проверить"}},
            ["job_id", "feedback"]), _child_followup, timeout_sec=30),
        ToolEntry("child_stop", _schema("child_stop", "Остановить свою задачу младшего.", {"job_id": _JOB_ID}, ["job_id"]),
                  _child_stop, timeout_sec=30),
        ToolEntry("child_accept", _schema(
            "child_accept",
            "Забрать результат младшего: fetch ветки из exchange, своя проверка в изолированной копии (от nobody, без секретов), "
            "затем merge в базовую ветку репозитория (без push) и закрытие задачи. verify_only=true — только проверить.",
            {"job_id": _JOB_ID, "verify_only": {"type": "boolean"}, "verify_cmd": {"type": "string", "description": "Переопределить команду проверки"},
             "merge": {"type": "boolean", "description": "Сливать в базовую ветку (по умолчанию true)"},
             "force": {"type": "boolean", "description": "Разрешить для состояния не done"}},
            ["job_id"]), _child_accept, timeout_sec=900),
        ToolEntry("child_reject", _schema(
            "child_reject", "Отклонить результат младшего с причиной (задача закрывается).",
            {"job_id": _JOB_ID, "reason": {"type": "string"}}, ["job_id", "reason"]), _child_reject, timeout_sec=30),
    ]
