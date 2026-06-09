"""Läs-/sökgränssnitt för det byggda arkivet (data/archive.db)."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "archive.db"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

router = APIRouter(prefix="/arkiv")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _fts_query(q: str) -> str:
    """Gör fritext säker för FTS5: varje ord som citerad term (AND mellan)."""
    terms = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms)


@router.get("", response_class=HTMLResponse)
def index(request: Request):
    if not DB.exists():
        return HTMLResponse("Arkivet är inte byggt än. Kör <code>uv run python -m scraper.build</code>.")
    con = _db()
    communities = con.execute(
        "SELECT id, full_name, message_count FROM communities "
        "WHERE message_count > 0 ORDER BY message_count DESC"
    ).fetchall()
    stats = con.execute(
        "SELECT (SELECT COUNT(*) FROM messages) m, (SELECT COUNT(*) FROM users) u"
    ).fetchone()
    con.close()
    return templates.TemplateResponse(request, "archive/index.html",
                                      {"communities": communities, "stats": stats})


@router.get("/sok", response_class=HTMLResponse)
def search(request: Request, q: str = Query("")):
    hits = []
    if q.strip() and DB.exists():
        con = _db()
        hits = con.execute(
            "SELECT m.id, m.thread_id, m.group_id, m.created_at, "
            "u.full_name AS sender, c.full_name AS community, "
            "snippet(messages_fts, 0, '<mark>', '</mark>', '…', 14) AS snip "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "LEFT JOIN users u ON u.id = m.sender_id "
            "LEFT JOIN communities c ON c.id = m.group_id "
            "WHERE messages_fts MATCH ? ORDER BY rank LIMIT 200",
            (_fts_query(q),),
        ).fetchall()
        con.close()
    return templates.TemplateResponse(request, "archive/search.html", {"q": q, "hits": hits})


@router.get("/c/{group_id}", response_class=HTMLResponse)
def community(request: Request, group_id: int):
    con = _db()
    info = con.execute("SELECT * FROM communities WHERE id = ?", (group_id,)).fetchone()
    threads = con.execute(
        "SELECT m.thread_id, MAX(m.created_at) AS last_at, COUNT(*) AS n, "
        "(SELECT body_plain FROM messages s WHERE s.id = m.thread_id) AS starter, "
        "(SELECT u.full_name FROM messages s LEFT JOIN users u ON u.id = s.sender_id "
        " WHERE s.id = m.thread_id) AS starter_by "
        "FROM messages m WHERE m.group_id = ? GROUP BY m.thread_id "
        "ORDER BY last_at DESC LIMIT 300",
        (group_id,),
    ).fetchall()
    con.close()
    return templates.TemplateResponse(request, "archive/community.html",
                                      {"info": info, "threads": threads})


@router.get("/fil/{path:path}")
def attachment_file(path: str):
    """Serverar en nedladdad bilaga. Begränsat till data/attachments/."""
    base = (ROOT / "data" / "attachments").resolve()
    target = (ROOT / "data" / path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, "filen finns inte")
    return FileResponse(target)


@router.get("/t/{thread_id}", response_class=HTMLResponse)
def thread(request: Request, thread_id: int):
    con = _db()
    msgs = con.execute(
        "SELECT m.*, u.full_name AS sender FROM messages m "
        "LEFT JOIN users u ON u.id = m.sender_id "
        "WHERE m.thread_id = ? ORDER BY m.created_at ASC",
        (thread_id,),
    ).fetchall()
    atts = {}
    for a in con.execute(
        "SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id "
        "WHERE m.thread_id = ?", (thread_id,)
    ).fetchall():
        atts.setdefault(a["message_id"], []).append(a)
    group = con.execute(
        "SELECT c.id, c.full_name FROM messages m JOIN communities c ON c.id = m.group_id "
        "WHERE m.thread_id = ? LIMIT 1", (thread_id,)
    ).fetchone()
    con.close()
    return templates.TemplateResponse(request, "archive/thread.html",
                                      {"msgs": msgs, "atts": atts, "group": group})
