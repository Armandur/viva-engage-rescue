"""Kontrollpanel för Viva Engage-dumpen.

Starta: uvicorn app.main:app --host 0.0.0.0 --port <ledig port>

Orkestrerar scraper.dump och scraper.download som subprocesser, visar progress
läst direkt från data/raw/, och låter dig klistra in en ny token när den gamla
gått ut. Token-byte gäller nästa körning (resume hoppar färdiga grupper).
"""

import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
ATT = ROOT / "data" / "attachments"
ENV = ROOT / ".env"
PIDFILE = ROOT / "data" / "run.pid"
LOGS = {
    "dump": ROOT / "data" / "dump.log",
    "update": ROOT / "data" / "dump.log",  # inkrementell skriver till samma logg
    "threads": ROOT / "data" / "threads.log",
    "download": ROOT / "data" / "download.log",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Viva Engage-dump")

from app.archive import router as archive_router  # noqa: E402

app.include_router(archive_router)


# ---- token ----

def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def _write_token(token: str) -> None:
    env = _read_env()
    env["YAMMER_TOKEN"] = token.strip()
    env.setdefault("YAMMER_API_BASE", "https://www.yammer.com/api/v1")
    ENV.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")


def _aud(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("aud")
    except Exception:
        return None


def _is_yammer_token(token: str) -> bool:
    """Avvisa bara token vars audience säkert pekar på fel resurs."""
    aud = _aud(token)
    return aud is None or "yammer.com" in str(aud)


def _token_info() -> dict:
    token = _read_env().get("YAMMER_TOKEN", "")
    if not token:
        return {"set": False}
    info = {"set": True}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if exp:
            info["exp"] = exp
            info["expires_in"] = int(exp - time.time())
        info["upn"] = claims.get("upn") or claims.get("unique_name")
    except Exception:
        pass
    return info


# ---- körning (subprocess via pidfil) ----

def _running() -> dict | None:
    if not PIDFILE.exists():
        return None
    try:
        data = json.loads(PIDFILE.read_text(encoding="utf-8"))
        os.kill(data["pid"], 0)  # lever?
        return data
    except (ProcessLookupError, ValueError, KeyError, json.JSONDecodeError):
        PIDFILE.unlink(missing_ok=True)
        return None
    except PermissionError:
        return data  # finns men ägs av annan - betrakta som körande


_COMMANDS = {
    "dump": ["scraper.dump"],
    "update": ["scraper.dump", "--update"],
    "threads": ["scraper.threads"],
    "download": ["scraper.download"],
}


def _start(kind: str) -> None:
    if kind not in _COMMANDS:
        raise HTTPException(400, "okänd körningstyp")
    if _running():
        raise HTTPException(409, "en körning pågår redan")
    if not _read_env().get("YAMMER_TOKEN"):
        raise HTTPException(400, "ingen token satt")
    log = LOGS[kind].open("w", encoding="utf-8")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", *_COMMANDS[kind]],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
    )
    PIDFILE.write_text(
        json.dumps({"pid": proc.pid, "kind": kind, "started": time.time()}),
        encoding="utf-8",
    )


def _stop() -> None:
    run = _running()
    if not run:
        return
    try:
        os.kill(run["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    PIDFILE.unlink(missing_ok=True)


# ---- progress ----

def _progress() -> dict:
    groups_file = RAW / "groups.json"
    total = 0
    if groups_file.exists():
        total = len(json.loads(groups_file.read_text(encoding="utf-8")))
    done = skipped = in_progress = pages = 0
    rows = []
    if (RAW / "groups").exists():
        for gdir in sorted((RAW / "groups").iterdir()):
            if not gdir.is_dir():
                continue
            npages = len(list(gdir.glob("page_*.json")))
            pages += npages
            gname = gdir.name
            gjson = gdir / "group.json"
            if gjson.exists():
                gname = json.loads(gjson.read_text(encoding="utf-8")).get(
                    "full_name", gname)
            if (gdir / ".done").exists():
                status = "klar"
                done += 1
            elif (gdir / ".skipped").exists():
                status = "hoppad"
                skipped += 1
            else:
                status = "pågår"
                in_progress += 1
            rows.append({"name": gname, "pages": npages, "status": status})
    attachments = len(list(ATT.glob("*"))) if ATT.exists() else 0
    return {
        "groups_total": total, "done": done, "skipped": skipped,
        "in_progress": in_progress, "pages": pages, "attachments": attachments,
        "bytes_raw": _dir_size(RAW), "bytes_attachments": _dir_size(ATT),
        "rows": sorted(rows, key=lambda r: r["pages"], reverse=True),
    }


def _dir_size(p: Path) -> int:
    total = 0
    if p.exists():
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def _log_tail(kind: str, n: int = 25) -> str:
    log = LOGS.get(kind)
    if not log or not log.exists():
        return ""
    return "\n".join(log.read_text(encoding="utf-8").splitlines()[-n:])


# ---- routes ----

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/viva-token-sync.user.js")
def userscript(request: Request):
    """Serverar userscriptet (Tampermonkey installerar direkt från .user.js).

    PANEL och @connect sätts till den adress panelen nåddes på, så scriptet
    pekar rätt oavsett host/port."""
    src = (ROOT / "browser" / "viva-token-sync.user.js").read_text(encoding="utf-8")
    base = str(request.base_url).rstrip("/")
    src = src.replace('"http://ubuntu-ai:8050"', f'"{base}"')
    src = src.replace("@connect      ubuntu-ai", f"@connect      {request.url.hostname}")
    return Response(src, media_type="text/javascript; charset=utf-8")


@app.get("/api/status")
def status():
    run = _running()
    return {
        "token": _token_info(),
        "running": run,
        "progress": _progress(),
        "log": _log_tail(run["kind"]) if run else _log_tail("dump"),
    }


@app.post("/token")
def set_token(token: str = Form(...)):
    _write_token(token)
    return RedirectResponse("/", status_code=302)


@app.post("/api/token")
def api_token(payload: dict = Body(...)):
    """Tar emot token programmatiskt (från userscriptet i webbläsaren)."""
    token = (payload.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token saknas")
    if not _is_yammer_token(token):
        raise HTTPException(400, f"fel resurs (aud={_aud(token)}) - inte en Yammer-API-token")
    _write_token(token)
    return {"ok": True}


@app.post("/start/{kind}")
def start(kind: str):
    _start(kind)
    return RedirectResponse("/", status_code=302)


@app.post("/stop")
def stop():
    _stop()
    return RedirectResponse("/", status_code=302)
