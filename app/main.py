"""Kontrollpanel för Viva Engage-dumpen.

Starta: uvicorn app.main:app --host 0.0.0.0 --port <ledig port>

Orkestrerar scraper.dump och scraper.download som subprocesser, visar progress
läst direkt från data/raw/, och låter dig klistra in en ny token när den gamla
gått ut. Token-byte gäller nästa körning (resume hoppar färdiga grupper).
"""

import base64
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections import deque
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
BUILDPID = ROOT / "data" / "build.pid"
ARCHIVE_DB = ROOT / "data" / "archive.db"
BUILD_LOG = ROOT / "data" / "build.log"
LOGS = {
    "dump": ROOT / "data" / "dump.log",
    "update": ROOT / "data" / "dump.log",  # inkrementell skriver till samma logg
    "threads": ROOT / "data" / "threads.log",
    "download": ROOT / "data" / "download.log",
    "enrich": ROOT / "data" / "enrich.log",
    "storylines": ROOT / "data" / "storylines.log",
    "community_info": ROOT / "data" / "community_info.log",
    "users": ROOT / "data" / "users.log",
    "reactors": ROOT / "data" / "reactors.log",
    "pipeline": ROOT / "data" / "pipeline.log",
    "import": ROOT / "data" / "import.log",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Viva Engage-dump")

from app.archive import router as archive_router  # noqa: E402
from app.archive import admin_router  # noqa: E402

app.include_router(archive_router)
app.include_router(admin_router)  # exempel + nekade bilagor, bara i admin-panelen


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
        pid = data["pid"]
    except (ValueError, KeyError, json.JSONDecodeError):
        PIDFILE.unlink(missing_ok=True)
        return None
    # Reapa om det är vårt eget avslutade barn (annars rapporterar os.kill det
    # som levande - en zombie - och panelen fastnar på "kör").
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            PIDFILE.unlink(missing_ok=True)
            return None
    except ChildProcessError:
        pass  # inte vårt barn (panelen omstartad) - faller tillbaka nedan
    except OSError:
        pass
    try:
        os.kill(pid, 0)  # lever?
    except ProcessLookupError:
        PIDFILE.unlink(missing_ok=True)
        return None
    except PermissionError:
        return data  # finns men ägs av annan - betrakta som körande
    if _is_zombie(pid):  # avslutat men ännu inte reapat
        PIDFILE.unlink(missing_ok=True)
        return None
    return data


_COMMANDS = {
    "dump": ["scraper.dump"],
    "update": ["scraper.dump", "--update"],
    "threads": ["scraper.threads"],
    "download": ["scraper.download"],
    "enrich": ["scraper.enrich"],
    "storylines": ["scraper.storylines"],
    "community_info": ["scraper.community_info"],
    "users": ["scraper.users"],
    "reactors": ["scraper.reactors"],
    "pipeline": ["scraper.pipeline"],
}


def _start(kind: str, groups: str = "") -> None:
    if kind not in _COMMANDS:
        raise HTTPException(400, "okänd körningstyp")
    if _running():
        raise HTTPException(409, "en körning pågår redan")
    if not _read_env().get("YAMMER_TOKEN"):
        raise HTTPException(400, "ingen token satt")
    cmd = [sys.executable, "-m", *_COMMANDS[kind]]
    gids = [g for g in re.split(r"[,\s]+", groups.strip()) if g.isdigit()]
    if gids:
        cmd += ["--groups", ",".join(gids)]
    log = LOGS[kind].open("w", encoding="utf-8")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
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


def _is_zombie(pid: int) -> bool:
    """True om processen avslutat men inte reapats (os.kill rapporterar den
    annars som levande). Läser tillståndsfältet i /proc/{pid}/stat."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
        return data.rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def _build_running() -> bool:
    """Arkivbygget har egen spårning - får köra parallellt med en dump."""
    if not BUILDPID.exists():
        return False
    try:
        pid = int(BUILDPID.read_text(encoding="utf-8"))
    except ValueError:
        BUILDPID.unlink(missing_ok=True)
        return False
    # Reapa om det är vårt eget avslutade barn (annars blir det en zombie).
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            BUILDPID.unlink(missing_ok=True)
            return False
    except ChildProcessError:
        pass  # inte vårt barn (panelen omstartad) - faller tillbaka nedan
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        BUILDPID.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True
    if _is_zombie(pid):  # avslutat men ännu inte reapat
        BUILDPID.unlink(missing_ok=True)
        return False
    return True


def _start_build() -> None:
    if _build_running():
        raise HTTPException(409, "ett bygge pågår redan")
    log = BUILD_LOG.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "scraper.build"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    BUILDPID.write_text(str(proc.pid), encoding="utf-8")


# ---- progress ----

def _progress() -> dict:
    groups_file = RAW / "groups.json"
    total = 0
    if groups_file.exists():
        total = len(json.loads(groups_file.read_text(encoding="utf-8")))
    # Meddelande-/trådantal per community ur det byggda arkivet (snabbt, en query).
    stats = _community_stats()
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
            gid = _as_int(gdir.name)
            msgs, threads = stats.get(gid, (0, 0))
            rows.append({"id": gid, "name": gname, "pages": npages, "status": status,
                         "messages": msgs, "threads": threads})
    attachments = len(list(ATT.glob("*"))) if ATT.exists() else 0
    return {
        "groups_total": total, "done": done, "skipped": skipped,
        "in_progress": in_progress, "pages": pages, "attachments": attachments,
        "bytes_raw": _dir_size(RAW), "bytes_attachments": _dir_size(ATT),
        "rows": sorted(rows, key=lambda r: (r["messages"], r["pages"]), reverse=True),
        "storyline": _storyline_summary(),
    }


def _storyline_summary() -> dict | None:
    """Storyline-täckning: upptäckta trådar/användare (ur discovered-filen) +
    vad som faktiskt ligger i arkivet (group_id NULL). None om inget finns."""
    disc = ROOT / "data" / "raw" / "storyline_threads.json"
    threads_disc = 0
    if disc.exists():
        try:
            d = json.loads(disc.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "threads" in d:  # nytt platt format
                threads_disc = len(d.get("threads") or [])
            elif isinstance(d, dict):  # gammalt {user_id: [tids]}
                tids: set = set()
                for v in d.values():
                    if v:
                        tids.update(v)
                threads_disc = len(tids)
        except (ValueError, OSError):
            pass
    msgs = threads_arch = users_with = 0
    if ARCHIVE_DB.exists():
        try:
            con = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
            msgs = con.execute(
                "SELECT COUNT(*) FROM messages WHERE group_id IS NULL").fetchone()[0]
            threads_arch = con.execute(
                "SELECT COUNT(DISTINCT thread_id) FROM messages WHERE group_id IS NULL"
            ).fetchone()[0]
            users_with = con.execute(
                "SELECT COUNT(DISTINCT sender_id) FROM messages "
                "WHERE group_id IS NULL AND sender_id IS NOT NULL").fetchone()[0]
            con.close()
        except sqlite3.Error:
            pass
    if not (threads_disc or msgs):
        return None
    return {"users_with": users_with, "threads_discovered": threads_disc,
            "messages": msgs, "threads": threads_arch}


def _as_int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return -1


def _community_stats() -> dict[int, tuple[int, int]]:
    """{group_id: (antal_meddelanden, antal_trådar)} ur archive.db, eller tomt."""
    if not ARCHIVE_DB.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT group_id, COUNT(*), COUNT(DISTINCT thread_id) "
            "FROM messages GROUP BY group_id"
        ).fetchall()
        con.close()
        return {gid: (mc, tc) for gid, mc, tc in rows}
    except sqlite3.Error:
        return {}


_size_cache: dict[str, tuple[float, int]] = {}


def _dir_size(p: Path, ttl: float = 60.0) -> int:
    """Total storlek på ett katalogträd. TTL-cachad: data/raw/ rymmer tiotusentals
    filer (33k+ profiler m.m.) och en full rglob tar ~1,5s - den får inte köras
    var 3:e sekund när panelen pollar /api/status. Storleken ändras långsamt."""
    key = str(p)
    now = time.time()
    hit = _size_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    total = 0
    if p.exists():
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    _size_cache[key] = (now, total)
    return total


_THREADS_DIR = ROOT / "data" / "raw" / "threads"
_thread_samples: deque = deque(maxlen=30)  # (tidpunkt, antal klara) för takt/ETA


def _thread_total() -> int:
    """Totalt antal kända trådar - parsas ur threads.log (skrivs vid start)."""
    log = LOGS["threads"]
    if log.exists():
        m = re.search(r"(\d+) kända trådar", log.read_text(encoding="utf-8")[:400])
        if m:
            return int(m.group(1))
    return 0


def _thread_status() -> dict | None:
    if not _THREADS_DIR.exists():
        return None
    done = skipped = 0
    for d in _THREADS_DIR.iterdir():
        if not d.is_dir():
            continue
        if (d / ".done").exists():
            done += 1
        elif (d / ".skipped").exists():
            skipped += 1
    if done == 0 and skipped == 0:
        return None
    total = _thread_total() or (done + skipped)
    now = time.time()
    _thread_samples.append((now, done))
    rate = eta = None
    if len(_thread_samples) >= 2:
        t0, d0 = _thread_samples[0]
        dt, dd = now - t0, done - d0
        if dt > 0 and dd > 0:
            rate = dd / dt  # trådar/sek över mätfönstret
            eta = int(max(total - done - skipped, 0) / rate)
    return {
        "total": total, "done": done, "skipped": skipped,
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_seconds": eta,
    }


_REACTIONS_DIR = ROOT / "data" / "raw" / "reactions"
_enrich_samples: deque = deque(maxlen=30)


def _enrich_total() -> int:
    """Antal trådar att berika = distinkta trådar i arkivet."""
    if not ARCHIVE_DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(DISTINCT thread_id) FROM messages").fetchone()[0]
        con.close()
        return n
    except sqlite3.Error:
        return 0


def _enrich_status() -> dict | None:
    if not _REACTIONS_DIR.exists():
        return None
    done = len(list(_REACTIONS_DIR.glob("*.done")))
    skipped = len(list(_REACTIONS_DIR.glob("*.skipped")))
    if done == 0 and skipped == 0:
        return None
    total = _enrich_total() or (done + skipped)
    now = time.time()
    _enrich_samples.append((now, done))
    rate = eta = None
    if len(_enrich_samples) >= 2:
        t0, d0 = _enrich_samples[0]
        dt, dd = now - t0, done - d0
        if dt > 0 and dd > 0:
            rate = dd / dt
            eta = int(max(total - done - skipped, 0) / rate)
    return {
        "total": total, "done": done, "skipped": skipped,
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_seconds": eta,
    }


_STORYLINE_PROGRESS = ROOT / "data" / "storyline_progress.json"
_storyline_samples: deque = deque(maxlen=30)


def _storyline_status() -> dict | None:
    if not _STORYLINE_PROGRESS.exists():
        return None
    try:
        p = json.loads(_STORYLINE_PROGRESS.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    done, total, phase = p.get("done", 0), p.get("total", 0), p.get("phase", "")
    now = time.time()
    _storyline_samples.append((now, done, phase))
    rate = eta = None
    same = [(t, d) for t, d, ph in _storyline_samples if ph == phase]
    if len(same) >= 2:
        t0, d0 = same[0]
        dt, dd = now - t0, done - d0
        if dt > 0 and dd > 0:
            rate = dd / dt
            eta = int(max(total - done, 0) / rate)
    return {
        "phase": phase, "total": total, "done": done,
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_seconds": eta,
    }


_UPDATE_PROGRESS = ROOT / "data" / "update_progress.json"


def _update_status() -> dict | None:
    """Nya inlägg under en pågående --update-körning. {new_posts, checked, total}."""
    if not _UPDATE_PROGRESS.exists():
        return None
    try:
        return json.loads(_UPDATE_PROGRESS.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


_DOWNLOAD_PROGRESS = ROOT / "data" / "download_progress.json"
_download_samples: deque = deque(maxlen=30)


def _download_status() -> dict | None:
    if not _DOWNLOAD_PROGRESS.exists():
        return None
    try:
        p = json.loads(_DOWNLOAD_PROGRESS.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    done, total = p.get("done", 0), p.get("total", 0)
    now = time.time()
    _download_samples.append((now, done))
    rate = eta = None
    if len(_download_samples) >= 2:
        t0, d0 = _download_samples[0]
        dt, dd = now - t0, done - d0
        if dt > 0 and dd > 0:
            rate = dd / dt
            eta = int(max(total - done, 0) / rate)
    return {
        "total": total, "done": done, "downloaded": p.get("downloaded", 0),
        "skipped": p.get("skipped", 0), "denied": p.get("denied", 0),
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_seconds": eta,
    }


_CINFO_DIR = ROOT / "data" / "raw" / "community_info"
_cinfo_samples: deque = deque(maxlen=30)


def _community_info_total() -> int:
    """Antal communities att hämta info för = rader i communities-tabellen."""
    if not ARCHIVE_DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
        con.close()
        return n
    except sqlite3.Error:
        return 0


def _community_info_status() -> dict | None:
    if not _CINFO_DIR.exists():
        return None
    done = len(list(_CINFO_DIR.glob("*.json")))
    if done == 0:
        return None
    total = _community_info_total() or done
    now = time.time()
    _cinfo_samples.append((now, done))
    rate = eta = None
    if len(_cinfo_samples) >= 2:
        t0, d0 = _cinfo_samples[0]
        dt, dd = now - t0, done - d0
        if dt > 0 and dd > 0:
            rate = dd / dt
            eta = int(max(total - done, 0) / rate)
    return {
        "total": total, "done": done,
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_seconds": eta,
    }


_USERS_PROGRESS = ROOT / "data" / "users_progress.json"
_users_samples: deque = deque(maxlen=30)


def _users_status() -> dict | None:
    if not _USERS_PROGRESS.exists():
        return None
    try:
        p = json.loads(_USERS_PROGRESS.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    phase, done, total = p.get("phase", ""), p.get("done", 0), p.get("total", 0)
    now = time.time()
    _users_samples.append((now, done, phase))
    rate = eta = None
    same = [(t, d) for t, d, ph in _users_samples if ph == phase]
    if len(same) >= 2:
        t0, d0 = same[0]
        dt, dd = now - t0, done - d0
        if dt > 0 and dd > 0:
            rate = dd / dt
            eta = int(max(total - done, 0) / rate) if total else None
    return {
        "phase": phase, "total": total, "done": done, "recent": p.get("recent", []),
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_seconds": eta,
    }


_REACTORS_PROGRESS = ROOT / "data" / "reactors_progress.json"
_reactors_samples: deque = deque(maxlen=30)


def _reactors_status() -> dict | None:
    if not _REACTORS_PROGRESS.exists():
        return None
    try:
        p = json.loads(_REACTORS_PROGRESS.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    done, total = p.get("done", 0), p.get("total", 0)
    now = time.time()
    _reactors_samples.append((now, done))
    rate = eta = None
    if len(_reactors_samples) >= 2:
        t0, d0 = _reactors_samples[0]
        dt, dd = now - t0, done - d0
        if dt > 0 and dd > 0:
            rate = dd / dt
            eta = int(max(total - done, 0) / rate)
    return {
        "total": total, "done": done, "upgraded": p.get("upgraded", 0),
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_seconds": eta,
    }


_PIPELINE_PROGRESS = ROOT / "data" / "pipeline_progress.json"


def _pipeline_status() -> dict | None:
    if not _PIPELINE_PROGRESS.exists():
        return None
    try:
        return json.loads(_PIPELINE_PROGRESS.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


_IMPORT_PROGRESS = ROOT / "data" / "import_progress.json"


def _import_status() -> dict | None:
    if not _IMPORT_PROGRESS.exists():
        return None
    try:
        return json.loads(_IMPORT_PROGRESS.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _archived_communities():
    """Hämtar communities och deras storlek ur arkivet för import-väljaren."""
    if not ARCHIVE_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        # Vi joinar mot messages för att få meddelandeantal
        rows = con.execute("""
            SELECT c.id, c.full_name, COUNT(m.id) as count
            FROM communities c
            LEFT JOIN messages m ON m.group_id = c.id
            GROUP BY c.id
            ORDER BY count DESC
        """).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _log_tail(kind: str, n: int = 25) -> str:
    log = LOGS.get(kind)
    if not log or not log.exists():
        return ""
    return "\n".join(log.read_text(encoding="utf-8").splitlines()[-n:])


# ---- routes ----

def _excluded_ids() -> set[int]:
    """Grupp-id i EXCLUDE_GROUPS (.env) - grupper som hålls utanför dump/build."""
    raw = _read_env().get("EXCLUDE_GROUPS", "")
    return {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    comms = _archived_communities()
    exc = _excluded_ids()
    arch_ids = {c["id"] for c in comms}
    return templates.TemplateResponse(request, "index.html", {
        "archived_communities": comms,
        "excluded_groups": exc,
        # Exkluderade id som inte finns i arkivet (t.ex. redan exkluderade test-grupper)
        # - visas ändå så de går att bocka ur.
        "extra_excluded": sorted(exc - arch_ids),
    })


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
        "archive": {
            "built_at": ARCHIVE_DB.stat().st_mtime if ARCHIVE_DB.exists() else None,
            "building": _build_running(),
        },
        "threads": _thread_status(),
        "enrich": _enrich_status(),
        "storylines": _storyline_status(),
        "download": _download_status(),
        "update": _update_status(),
        "community_info": _community_info_status(),
        "users": _users_status(),
        "reactors": _reactors_status(),
        "pipeline": _pipeline_status(),
        "import": _import_status(),
    }


@app.post("/build")
def build():
    _start_build()
    return RedirectResponse("/", status_code=302)


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
def start(kind: str, groups: str = ""):
    _start(kind, groups)
    return RedirectResponse("/", status_code=302)


@app.post("/start_import/{cmd}")
def start_import(cmd: str, source: int = Form(...), target: str = Form(""), flat: bool = Form(False)):
    if _running():
        raise HTTPException(409, "en körning pågår redan")
    if not _read_env().get("YAMMER_TOKEN"):
        raise HTTPException(400, "ingen token satt")

    # cmd: dry-run, smoke, run, clear
    full_cmd = [sys.executable, "-m", "scraper.importer", cmd, str(source)]
    if target and target.strip():
        full_cmd.append(target.strip())
    if flat:
        full_cmd.append("--flat")

    log = LOGS["import"].open("w", encoding="utf-8")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        full_cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
    )
    PIDFILE.write_text(
        json.dumps({"pid": proc.pid, "kind": "import", "started": time.time()}),
        encoding="utf-8",
    )
    return RedirectResponse("/", status_code=302)


@app.post("/api/exclude")
def set_exclude(ids: str = Form("")):
    """Sparar EXCLUDE_GROUPS i .env (grupper som hålls utanför dump/build)."""
    clean = sorted({int(x) for x in ids.split(",") if x.strip().lstrip("-").isdigit()})
    env = _read_env()
    if clean:
        env["EXCLUDE_GROUPS"] = ",".join(str(i) for i in clean)
    else:
        env.pop("EXCLUDE_GROUPS", None)
    env.setdefault("YAMMER_API_BASE", "https://www.yammer.com/api/v1")
    ENV.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    return {"ok": True, "excluded": clean}


@app.post("/stop")
def stop():
    _stop()
    return RedirectResponse("/", status_code=302)
