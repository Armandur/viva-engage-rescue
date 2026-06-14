"""Läs-/sökgränssnitt för det byggda arkivet (data/archive.db)."""

import html
import json
import re
import sqlite3
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "archive.db"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

router = APIRouter(prefix="/arkiv")
# Admin-bara vyer (exempel, nekade bilagor) - inkluderas bara av admin-panelen
# (app.main), inte av den publika appen (app.public).
admin_router = APIRouter(prefix="/arkiv")

_STHLM = ZoneInfo("Europe/Stockholm")
_MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug",
           "sep", "okt", "nov", "dec"]


def _svtid(value: str | None) -> str:
    """Yammers '2008/09/10 18:26:08 +0000' -> '10 sep 2008 20:26' (svensk tid)."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y/%m/%d %H:%M:%S %z").astimezone(_STHLM)
    except (ValueError, TypeError):
        return value
    return f"{dt.day} {_MONTHS[dt.month - 1]} {dt.year} {dt.hour:02d}:{dt.minute:02d}"


def _domain(url: str | None) -> str:
    """Värddomän ur en URL för länkkort, utan 'www.'."""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc
    except (ValueError, TypeError):
        return ""
    return host[4:] if host.startswith("www.") else host


def _built_at() -> str:
    """När arkivet senast byggdes (archive.db:s mtime), i svensk tid. '' om obyggt."""
    if not DB.exists():
        return ""
    dt = datetime.fromtimestamp(DB.stat().st_mtime, _STHLM)
    return f"{dt.day} {_MONTHS[dt.month - 1]} {dt.year} {dt.hour:02d}:{dt.minute:02d}"


templates.env.filters["svtid"] = _svtid
templates.env.filters["domain"] = _domain
templates.env.globals["archive_built"] = _built_at

# Formatering-taggar vi behåller orörda (Yammers rich använder bara dessa).
_INLINE_OK = {"br", "i", "em", "strong", "b", "p", "hr"}

# Reaktionstyper (GraphQL) -> emoji + svensk etikett för trådvyn.
REACTION_EMOJI = {
    "like": "👍", "love": "❤️", "laugh": "😆", "intenseLaugh": "🤣", "thank": "🙏",
    "celebrate": "🎉", "praise": "👏", "support": "🤝", "happy": "😊", "smile": "🙂",
    "excited": "🤩", "starStruck": "🤩", "mindBlown": "🤯", "surprised": "😮",
    "shocked": "😲", "sad": "😢", "crying": "😭", "heartBroken": "💔", "angry": "😠",
    "scared": "😱", "thinking": "🤔", "brain": "🧠", "takingNotes": "📝",
    "watching": "👀", "bullseye": "🎯", "medal": "🏅", "agree": "✅",
    "confirmed": "☑️", "goofy": "😜", "silly": "😋",
}
REACTION_LABEL = {
    "like": "Gilla", "love": "Älskar", "laugh": "Skratt", "intenseLaugh": "Gapskratt",
    "thank": "Tack", "celebrate": "Firar", "praise": "Beröm", "support": "Stöd",
    "happy": "Glad", "smile": "Leende", "excited": "Taggad", "starStruck": "Stjärnögd",
    "mindBlown": "Wow", "surprised": "Förvånad", "shocked": "Chockad", "sad": "Ledsen",
    "crying": "Gråter", "heartBroken": "Hjärtekross", "angry": "Arg", "scared": "Rädd",
    "thinking": "Funderar", "brain": "Smart", "takingNotes": "Antecknar",
    "watching": "Tittar", "bullseye": "Mitt i prick", "medal": "Medalj",
    "agree": "Håller med", "confirmed": "Bekräftat", "goofy": "Fånig", "silly": "Tokig",
}
# Inline user-mentions i body.rich (för att inte upprepa dem i aviserade-raden).
_SPAN_USER = re.compile(r"data-yammer-object='user:(\d+)'")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


class _RichSanitizer(HTMLParser):
    """Saniterar Yammers body.rich till säker HTML.

    Behåller radbrytningar/formatering (br, i, strong, p, hr), skriver om
    yammer-object-spans (user/group/tag) till våra interna arkiv-länkar och
    behåller externa länkar med säker href. All textdata escapas. Allt annat
    (okända taggar/attribut) släpps, men deras textinnehåll bevaras.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._a_open = False          # vi har öppnat en extern <a>
        self._obj: dict | None = None  # aktiv yammer-object-span
        self._obj_depth = 0           # span-djup inuti objektet

    def _emit(self, s: str) -> None:
        if self._obj is not None:
            self._obj["buf"].append(s)
        else:
            self.out.append(s)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "span":
            obj = d.get("data-yammer-object")
            if self._obj is None and obj and ":" in obj:
                typ, _, rid = obj.partition(":")
                self._obj = {"type": typ, "id": rid, "buf": []}
                self._obj_depth = 1
            elif self._obj is not None:
                self._obj_depth += 1
            return  # vanlig span: släpp taggen, behåll text
        if self._obj is not None:
            return  # inuti objekt: ignorera inre taggar, behåll bara texten
        if tag == "a":
            href = d.get("href", "")
            if href.startswith(("http://", "https://")):
                self.out.append(
                    f'<a href="{html.escape(href, quote=True)}" '
                    f'rel="noopener noreferrer" target="_blank">'
                )
                self._a_open = True
            return
        if tag in _INLINE_OK:
            self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if self._obj is None and tag in _INLINE_OK:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if self._obj is not None:
            if tag == "span":
                self._obj_depth -= 1
                if self._obj_depth == 0:
                    self.out.append(self._render_obj(self._obj))
                    self._obj = None
            return
        if tag == "a":
            if self._a_open:
                self.out.append("</a>")
                self._a_open = False
            return
        if tag in _INLINE_OK and tag not in ("br", "hr"):
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self._emit(html.escape(data))

    @staticmethod
    def _render_obj(obj: dict) -> str:
        typ, rid = obj["type"], obj["id"]
        text = "".join(obj["buf"]).strip()  # redan escapad
        if typ == "user" and rid.isdigit():
            return f'<a href="/arkiv/u/{rid}" class="text-primary">@{text or "okänd"}</a>'
        if typ == "group" and rid.isdigit():
            return f'<a href="/arkiv/c/{rid}" class="text-primary">{text or "grupp"}</a>'
        if typ == "tag":
            t = text if text.startswith("#") else "#" + text
            return f'<span class="text-secondary">{t}</span>'
        return text


def render_body(rich: str | None) -> Markup:
    """Saniterar body.rich till säker HTML för visning."""
    if not rich:
        return Markup("")
    p = _RichSanitizer()
    p.feed(rich)
    p.close()
    if p._obj is not None:  # oavslutad object-span: flusha så inget innehåll tappas
        p.out.append(p._render_obj(p._obj))
    if p._a_open:
        p.out.append("</a>")
    return Markup("".join(p.out))


templates.env.globals["render_body"] = render_body


def _fts_query(q: str) -> str:
    """Gör fritext säker för FTS5: varje ord som citerad term (AND mellan)."""
    terms = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms)


def _isodate(s: str) -> str | None:
    """'YYYY-MM-DD' (HTML date-input) -> 'YYYY/MM/DD' för jämförelse mot created_at."""
    s = (s or "").strip()
    return s.replace("-", "/") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def _safe_snip(raw: str | None) -> Markup:
    """Escapar snippet-text och gör markörerna \\x01/\\x02 till <mark>."""
    if not raw:
        return Markup("")
    return Markup(html.escape(raw).replace("\x01", "<mark>").replace("\x02", "</mark>"))


@router.get("", response_class=HTMLResponse)
def index(request: Request):
    if not DB.exists():
        return HTMLResponse("Arkivet är inte byggt än. Kör <code>uv run python -m scraper.build</code>.")
    con = _db()
    communities = con.execute(
        "SELECT c.id, c.full_name, c.message_count, c.member_count, c.privacy, "
        "c.company_group, c.moderated, c.restricted_posting, c.accessible, "
        "(SELECT COUNT(DISTINCT thread_id) FROM messages WHERE group_id = c.id) AS thread_count "
        "FROM communities c WHERE c.message_count > 0 OR c.privacy = 'private' "
        "ORDER BY c.message_count DESC, c.full_name"
    ).fetchall()
    stats = con.execute(
        "SELECT (SELECT COUNT(*) FROM messages) m, (SELECT COUNT(*) FROM users) u"
    ).fetchone()
    con.close()
    return templates.TemplateResponse(request, "archive/index.html",
                                      {"communities": communities, "stats": stats})


@router.get("/oversikt", response_class=HTMLResponse)
def overview(request: Request):
    if not DB.exists():
        return HTMLResponse("Arkivet är inte byggt än.")
    con = _db()
    totals = con.execute(
        "SELECT (SELECT COUNT(*) FROM messages) AS msgs, "
        "(SELECT COUNT(DISTINCT thread_id) FROM messages) AS threads, "
        "(SELECT COUNT(*) FROM users) AS users, "
        "(SELECT COUNT(*) FROM communities WHERE message_count > 0) AS communities, "
        "(SELECT COUNT(*) FROM attachments) AS attachments, "
        "(SELECT MIN(created_at) FROM messages) AS first, "
        "(SELECT MAX(created_at) FROM messages) AS last"
    ).fetchone()
    top_communities = con.execute(
        "SELECT id, full_name, message_count FROM communities "
        "WHERE message_count > 0 ORDER BY message_count DESC LIMIT 15"
    ).fetchall()
    top_posters = con.execute(
        "SELECT m.sender_id AS id, u.full_name AS name, COUNT(*) AS n FROM messages m "
        "LEFT JOIN users u ON u.id = m.sender_id WHERE m.sender_id IS NOT NULL "
        "GROUP BY m.sender_id ORDER BY n DESC LIMIT 15"
    ).fetchall()
    per_year = con.execute(
        "SELECT substr(created_at,1,4) AS yr, COUNT(*) AS n FROM messages "
        "WHERE created_at IS NOT NULL GROUP BY yr ORDER BY yr"
    ).fetchall()
    con.close()
    ymax = max((r["n"] for r in per_year), default=1)
    return templates.TemplateResponse(request, "archive/overview.html", {
        "totals": totals, "top_communities": top_communities,
        "top_posters": top_posters, "per_year": per_year, "ymax": ymax,
    })


@admin_router.get("/nekade", response_class=HTMLResponse)
def denied(request: Request):
    """Bilagor som download.py fick åtkomst nekad på: fil-länk + länk till inlägget."""
    path = ROOT / "data" / "attachments_denied.json"
    entries = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            entries = []
    # Slå upp inläggets Viva-permalänk ur DB:t (täcker poster utan post_web_url).
    ids = [e["message_id"] for e in entries if e.get("message_id") and not e.get("post_web_url")]
    if ids and DB.exists():
        con = _db()
        ph = ",".join("?" * len(ids))
        urls = {r["id"]: r["web_url"]
                for r in con.execute(f"SELECT id, web_url FROM messages WHERE id IN ({ph})", ids)}
        con.close()
        for e in entries:
            if not e.get("post_web_url"):
                e["post_web_url"] = urls.get(e.get("message_id"))
    return templates.TemplateResponse(request, "archive/denied.html", {"entries": entries})


@router.get("/storylines", response_class=HTMLResponse)
def storylines(request: Request):
    if not DB.exists():
        return HTMLResponse("Arkivet är inte byggt än.")
    con = _db()
    # Inlägg på en användares egen storyline = trådar de startat (id=thread_id)
    # bland väggposterna (group_id NULL). wall_total = de + alla svar i trådarna.
    users = con.execute(
        "SELECT s.sender_id AS id, u.full_name AS name, COUNT(*) AS posts, "
        "(SELECT COUNT(*) FROM messages a WHERE a.thread_id IN "
        "  (SELECT s2.thread_id FROM messages s2 WHERE s2.group_id IS NULL "
        "   AND s2.id = s2.thread_id AND s2.sender_id = s.sender_id)) AS wall_total "
        "FROM messages s LEFT JOIN users u ON u.id = s.sender_id "
        "WHERE s.group_id IS NULL AND s.id = s.thread_id AND s.sender_id IS NOT NULL "
        "GROUP BY s.sender_id ORDER BY posts DESC, wall_total DESC"
    ).fetchall()
    con.close()
    return templates.TemplateResponse(request, "archive/storylines.html", {"users": users})


@admin_router.get("/exempel", response_class=HTMLResponse)
def examples(request: Request):
    if not DB.exists():
        return HTMLResponse("Arkivet är inte byggt än.")
    con = _db()
    cases = []

    def one(label, desc, sql):
        r = con.execute(sql).fetchone()
        if r and r["thread_id"]:
            cases.append({"label": label, "desc": desc,
                          "tid": r["thread_id"], "mid": r["id"]})

    one("Omröstning", "meddelande med svarsalternativ",
        "SELECT m.thread_id, m.id FROM messages m JOIN polls p ON p.message_id = m.id LIMIT 1")
    one("Delat inlägg (reshare)", "shared_message_id satt",
        "SELECT thread_id, id FROM messages WHERE shared_message_id IS NOT NULL LIMIT 1")
    one("Announcement", "message_type = announcement",
        "SELECT thread_id, id FROM messages WHERE message_type = 'announcement' LIMIT 1")
    one("Fråga", "message_type = question",
        "SELECT thread_id, id FROM messages WHERE message_type = 'question' LIMIT 1")
    one("Systemmeddelande", "message_type = system",
        "SELECT thread_id, id FROM messages WHERE message_type = 'system' LIMIT 1")
    one("Med rubrik", "title satt",
        "SELECT thread_id, id FROM messages WHERE title IS NOT NULL LIMIT 1")
    one("Inline-bild", "bildbilaga som visas i inlägget",
        "SELECT m.thread_id, m.id FROM messages m JOIN attachments a ON a.message_id = m.id "
        "WHERE a.type = 'image' AND a.local_path IS NOT NULL LIMIT 1")
    one("Länkkort", "delad länk (ymodule) med titel/beskrivning",
        "SELECT m.thread_id, m.id FROM messages m JOIN attachments a ON a.message_id = m.id "
        "WHERE a.type = 'ymodule' AND a.description IS NOT NULL LIMIT 1")
    one("Arkiverad fil", "nedladdad filbilaga (ej bild)",
        "SELECT m.thread_id, m.id FROM messages m JOIN attachments a ON a.message_id = m.id "
        "WHERE a.type = 'file' AND a.local_path IS NOT NULL LIMIT 1")
    one("@-mention", "notified_user_ids",
        "SELECT m.thread_id, m.id FROM messages m JOIN mentions mn ON mn.message_id = m.id LIMIT 1")
    one("Mest gillade", "högst like_count",
        "SELECT thread_id, id FROM messages ORDER BY like_count DESC LIMIT 1")
    one("Längsta tråden", "flest meddelanden - djup nästling",
        "SELECT thread_id, MIN(id) AS id FROM messages GROUP BY thread_id ORDER BY COUNT(*) DESC LIMIT 1")
    con.close()
    return templates.TemplateResponse(request, "archive/examples.html", {"cases": cases})


_SEARCH_PAGE = 100  # träffar per sida i sök/browse-lazy-loading
_SEARCH_COLS = ("m.id, m.thread_id, m.group_id, m.created_at, m.sender_id, "
                "u.full_name AS sender, c.full_name AS community")
_SEARCH_JOINS = ("LEFT JOIN users u ON u.id = m.sender_id "
                 "LEFT JOIN communities c ON c.id = m.group_id")


def _search_where(grupp, avsandare, fran, till):
    where, params = [], []
    if grupp:
        where.append("m.group_id = ?"); params.append(grupp)
    if avsandare.strip():
        where.append("u.full_name LIKE ?"); params.append(f"%{avsandare.strip()}%")
    if (d := _isodate(fran)):
        where.append("substr(m.created_at,1,10) >= ?"); params.append(d)
    if (d := _isodate(till)):
        where.append("substr(m.created_at,1,10) <= ?"); params.append(d)
    return where, params


def _search_rows(con, q, grupp, avsandare, fran, till, offset, limit):
    where, params = _search_where(grupp, avsandare, fran, till)
    if q.strip():
        cond = " AND ".join(["messages_fts MATCH ?"] + where)
        rows = con.execute(
            f"SELECT {_SEARCH_COLS}, snippet(messages_fts, 0, char(1), char(2), '…', 14) AS snip "
            f"FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid {_SEARCH_JOINS} "
            f"WHERE {cond} ORDER BY rank LIMIT ? OFFSET ?",
            [_fts_query(q), *params, limit, offset]).fetchall()
    elif where:  # ingen fritext men filter satta -> bläddra
        rows = con.execute(
            f"SELECT {_SEARCH_COLS}, substr(m.body_plain,1,200) AS snip "
            f"FROM messages m {_SEARCH_JOINS} WHERE {' AND '.join(where)} "
            f"ORDER BY m.created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    else:
        rows = []
    return [{**dict(r), "snip": _safe_snip(r["snip"])} for r in rows]


def _search_total(con, q, grupp, avsandare, fran, till):
    where, params = _search_where(grupp, avsandare, fran, till)
    if q.strip():
        cond = " AND ".join(["messages_fts MATCH ?"] + where)
        return con.execute(
            f"SELECT COUNT(*) FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            f"{_SEARCH_JOINS} WHERE {cond}", [_fts_query(q), *params]).fetchone()[0]
    if where:
        return con.execute(
            f"SELECT COUNT(*) FROM messages m {_SEARCH_JOINS} WHERE {' AND '.join(where)}",
            params).fetchone()[0]
    return 0


def _search_qs(q, grupp, avsandare, fran, till) -> str:
    """Filterparametrarna som query-sträng för lazy-load-fragmentet (utan offset)."""
    return urlencode({k: v for k, v in {
        "q": q, "grupp": grupp, "avsandare": avsandare, "fran": fran, "till": till,
    }.items() if v})


@router.get("/sok/mer", response_class=HTMLResponse)
def search_more(request: Request, q: str = Query(""), grupp: int | None = Query(None),
                avsandare: str = Query(""), fran: str = Query(""), till: str = Query(""),
                offset: int = Query(0)):
    """Nästa sida sökträffar (HTML-fragment) för lazy-loading."""
    hits = []
    if DB.exists():
        con = _db()
        hits = _search_rows(con, q, grupp, avsandare, fran, till, offset, _SEARCH_PAGE)
        con.close()
    return templates.TemplateResponse(request, "archive/_search_items.html", {"hits": hits})


@router.get("/sok", response_class=HTMLResponse)
def search(request: Request, q: str = Query(""), grupp: int | None = Query(None),
           avsandare: str = Query(""), fran: str = Query(""), till: str = Query("")):
    hits, communities, total = [], [], 0
    if DB.exists():
        con = _db()
        communities = con.execute(
            "SELECT id, full_name FROM communities WHERE message_count > 0 "
            "ORDER BY full_name"
        ).fetchall()
        hits = _search_rows(con, q, grupp, avsandare, fran, till, 0, _SEARCH_PAGE)
        total = _search_total(con, q, grupp, avsandare, fran, till)
        con.close()
    has_filter = bool(grupp or avsandare.strip() or _isodate(fran) or _isodate(till))
    return templates.TemplateResponse(request, "archive/search.html", {
        "q": q, "hits": hits, "communities": communities, "has_filter": has_filter,
        "grupp": grupp, "avsandare": avsandare, "fran": fran, "till": till,
        "total": total, "search_qs": _search_qs(q, grupp, avsandare, fran, till),
    })


_THREAD_PAGE = 100  # trådar per sida i community-vyns lazy-loading


def _community_threads(con, group_id: int, offset: int, limit: int):
    return con.execute(
        "SELECT m.thread_id, MAX(m.created_at) AS last_at, COUNT(*) AS n, "
        "(SELECT body_plain FROM messages s WHERE s.id = m.thread_id) AS starter, "
        "(SELECT title FROM messages s WHERE s.id = m.thread_id) AS title, "
        "(SELECT message_type FROM messages s WHERE s.id = m.thread_id) AS message_type, "
        "(SELECT sh.body_plain FROM messages sh WHERE sh.id = "
        "  (SELECT s3.shared_message_id FROM messages s3 WHERE s3.id = m.thread_id)) AS shared_body, "
        "(SELECT u.full_name FROM messages s LEFT JOIN users u ON u.id = s.sender_id "
        " WHERE s.id = m.thread_id) AS starter_by, "
        "(SELECT s.sender_id FROM messages s WHERE s.id = m.thread_id) AS starter_id "
        "FROM messages m WHERE m.group_id = ? GROUP BY m.thread_id "
        "ORDER BY last_at DESC LIMIT ? OFFSET ?",
        (group_id, limit, offset),
    ).fetchall()


@router.get("/c/{group_id}/trader", response_class=HTMLResponse)
def community_threads(request: Request, group_id: int, offset: int = Query(0)):
    """En sida trådar (HTML-fragment) för community-vyns lazy-loading."""
    con = _db()
    threads = _community_threads(con, group_id, offset, _THREAD_PAGE)
    con.close()
    return templates.TemplateResponse(request, "archive/_thread_items.html",
                                      {"threads": threads})


@router.get("/c/{group_id}", response_class=HTMLResponse)
def community(request: Request, group_id: int):
    con = _db()
    info = con.execute("SELECT * FROM communities WHERE id = ?", (group_id,)).fetchone()
    threads = _community_threads(con, group_id, 0, _THREAD_PAGE)
    n_threads = con.execute(
        "SELECT COUNT(DISTINCT thread_id) FROM messages WHERE group_id = ?",
        (group_id,),
    ).fetchone()[0]
    pinned = con.execute(
        "SELECT title, url, type FROM pinned WHERE group_id = ?", (group_id,)
    ).fetchall()
    members = con.execute(
        "SELECT gm.user_id, gm.is_admin, COALESCE(u.full_name, 'okänd') AS name, "
        "u.job_title, u.department, u.avatar_local "
        "FROM group_members gm LEFT JOIN users u ON u.id = gm.user_id "
        "WHERE gm.group_id = ? ORDER BY gm.is_admin DESC, name", (group_id,)
    ).fetchall()
    con.close()
    about = render_body(info["extended_description"]) if info and info["extended_description"] else None
    admins = [mb for mb in members if mb["is_admin"]]
    return templates.TemplateResponse(request, "archive/community.html",
                                      {"info": info, "threads": threads, "pinned": pinned,
                                       "members": members, "admins": admins, "about": about,
                                       "gid": group_id, "n_threads": n_threads})


@router.get("/fil/{path:path}")
def attachment_file(path: str):
    """Serverar en nedladdad bilaga eller länkkortsminiatyr. Begränsat till
    data/attachments/ och data/thumbnails/."""
    allowed = [(ROOT / "data" / "attachments").resolve(),
               (ROOT / "data" / "thumbnails").resolve()]
    target = (ROOT / "data" / path).resolve()
    if not any(b in target.parents for b in allowed) or not target.is_file():
        raise HTTPException(404, "filen finns inte")
    return FileResponse(target)


_AV_COLORS = ["#0d6efd", "#6610f2", "#6f42c1", "#d63384", "#dc3545", "#fd7e14",
              "#198754", "#20c997", "#0dcaf0", "#495057"]


def _initials(name: str) -> str:
    parts = (name or "").split()
    if not parts:
        return "?"
    return (parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")).upper()


@router.get("/avatar/{user_id}")
def avatar(user_id: int, n: str = Query("")):
    """Profilbild. Returnerar nedladdad mugshot om den finns, annars en genererad
    SVG-initialcirkel (`?n=Namn`). Så kan alla vyer alltid peka hit utan att
    själva känna till om en bild finns."""
    hit = next((ROOT / "data" / "avatars").glob(f"{user_id}.*"), None)
    if hit and hit.is_file():
        return FileResponse(hit)
    color = _AV_COLORS[user_id % len(_AV_COLORS)]
    initials = html.escape(_initials(n))
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<circle cx="50" cy="50" r="50" fill="{color}"/>'
        f'<text x="50" y="50" dy=".35em" text-anchor="middle" fill="#fff" '
        f'font-family="sans-serif" font-size="40" font-weight="600">{initials}</text>'
        f'</svg>'
    )
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})


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

    # Inline-mentions per meddelande, så aviserade-raden bara visar dem som
    # inte redan är länkade i brödtexten.
    inline = {m["id"]: set(int(x) for x in _SPAN_USER.findall(m["body_rich"] or ""))
              for m in msgs}
    mentions: dict[int, list[sqlite3.Row]] = {}
    for r in con.execute(
        "SELECT mn.message_id AS mid, mn.user_id AS uid, u.full_name AS name "
        "FROM mentions mn JOIN messages m ON m.id = mn.message_id "
        "LEFT JOIN users u ON u.id = mn.user_id WHERE m.thread_id = ?", (thread_id,)
    ):
        if r["uid"] in inline.get(r["mid"], ()):
            continue  # redan länkad inline
        mentions.setdefault(r["mid"], []).append(r)
    likes: dict[int, list[sqlite3.Row]] = {}
    for r in con.execute(
        "SELECT lk.message_id AS mid, lk.user_id AS uid, u.full_name AS name "
        "FROM likes lk JOIN messages m ON m.id = lk.message_id "
        "LEFT JOIN users u ON u.id = lk.user_id WHERE m.thread_id = ?", (thread_id,)
    ):
        likes.setdefault(r["mid"], []).append(r)

    polls: dict[int, list[str]] = {}
    for r in con.execute(
        "SELECT p.message_id AS mid, p.answer FROM polls p JOIN messages m ON m.id = p.message_id "
        "WHERE m.thread_id = ? ORDER BY p.option_index", (thread_id,)
    ):
        polls.setdefault(r["mid"], []).append(r["answer"])

    # Reaktioner + reagerande (GraphQL-berikning) och seen-count för tråden.
    reactions: dict[int, list[sqlite3.Row]] = {}
    for r in con.execute(
        "SELECT rx.message_id AS mid, rx.type, rx.count FROM reactions rx "
        "JOIN messages m ON m.id = rx.message_id WHERE m.thread_id = ? "
        "ORDER BY rx.count DESC", (thread_id,)
    ):
        reactions.setdefault(r["mid"], []).append(r)
    reactors: dict[int, dict[str, list[str]]] = {}
    for r in con.execute(
        "SELECT rc.message_id AS mid, rc.type, COALESCE(u.full_name, 'okänd') AS name "
        "FROM reactors rc JOIN messages m ON m.id = rc.message_id "
        "LEFT JOIN users u ON u.id = rc.user_id WHERE m.thread_id = ?", (thread_id,)
    ):
        reactors.setdefault(r["mid"], {}).setdefault(r["type"], []).append(r["name"])
    seen_row = con.execute(
        "SELECT seen_by_count FROM thread_meta WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    seen_count = seen_row["seen_by_count"] if seen_row else None

    # Delade inlägg (reshares): hämta originalets tråd + text + avsändare så vi
    # kan visa innehållet i stället för en tom "trådstart".
    shared_links: dict[int, int] = {}
    shared_posts: dict[int, sqlite3.Row] = {}
    shared_ids = [m["shared_message_id"] for m in msgs if m["shared_message_id"]]
    if shared_ids:
        ph = ",".join("?" * len(shared_ids))
        for r in con.execute(
            f"SELECT m.id, m.thread_id, m.body_plain, u.full_name AS sender "
            f"FROM messages m LEFT JOIN users u ON u.id = m.sender_id "
            f"WHERE m.id IN ({ph})", shared_ids
        ):
            shared_links[r["id"]] = r["thread_id"]
            shared_posts[r["id"]] = r

    group = con.execute(
        "SELECT c.id, c.full_name FROM messages m JOIN communities c ON c.id = m.group_id "
        "WHERE m.thread_id = ? LIMIT 1", (thread_id,)
    ).fetchone()
    con.close()

    # Bygg ett nästlingsträd ur replied_to_id för Reddit-lika trådskenor.
    # msgs är sorterad på created_at, så barnen hamnar kronologiskt.
    by_id = {m["id"]: m for m in msgs}
    kids: dict[int, list[int]] = {}
    roots: list[int] = []
    for m in msgs:
        p = m["replied_to_id"]
        if p and p in by_id and p != m["id"]:
            kids.setdefault(p, []).append(m["id"])
        else:
            roots.append(m["id"])

    def _node(mid: int, seen: frozenset) -> dict:
        seen = seen | {mid}
        return {"id": mid, "children": [_node(c, seen) for c in kids.get(mid, [])
                                        if c not in seen]}

    tree = [_node(r, frozenset()) for r in roots]
    bodies = {m["id"]: render_body(m["body_rich"]) for m in msgs}

    return templates.TemplateResponse(request, "archive/thread.html", {
        "msgs": msgs, "atts": atts, "group": group, "bodies": bodies,
        "tree": tree, "by_id": by_id,
        "mentions": mentions, "likes": likes,
        "polls": polls, "shared_links": shared_links, "shared_posts": shared_posts,
        "reactions": reactions, "reactors": reactors, "seen_count": seen_count,
        "emoji": REACTION_EMOJI, "rlabel": REACTION_LABEL,
    })


_USER_COMM_PAGE = 100  # community-inlägg per sida på användarsidan


def _user_community(con, user_id: int, offset: int, limit: int):
    return con.execute(
        "SELECT m.id, m.thread_id, m.created_at, m.body_plain, m.body_rich, "
        "c.full_name AS community FROM messages m "
        "LEFT JOIN communities c ON c.id = m.group_id "
        "WHERE m.sender_id = ? AND m.group_id IS NOT NULL "
        "ORDER BY m.created_at DESC LIMIT ? OFFSET ?", (user_id, limit, offset),
    ).fetchall()


@router.get("/u/{user_id}/community", response_class=HTMLResponse)
def user_community(request: Request, user_id: int, offset: int = Query(0)):
    """Nästa sida av en användares community-inlägg (HTML-fragment)."""
    con = _db()
    community = _user_community(con, user_id, offset, _USER_COMM_PAGE)
    con.close()
    return templates.TemplateResponse(request, "archive/_user_comm_items.html",
                                      {"community": community})


@router.get("/u/{user_id}", response_class=HTMLResponse)
def user(request: Request, user_id: int):
    con = _db()
    info = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    # Storyline = väggposter (group_id NULL). En storyline-tråd tillhör den som
    # startade den (väggägaren = trådstartarens avsändare). Dela upp i egna
    # (på egen vägg) och kommentarer på andras vägg.
    storyline = con.execute(
        "SELECT m.id, m.thread_id, m.created_at, m.body_plain, m.body_rich, "
        "(SELECT s.sender_id FROM messages s WHERE s.id = m.thread_id) AS wall_owner, "
        "(SELECT u2.full_name FROM messages s LEFT JOIN users u2 ON u2.id = s.sender_id "
        " WHERE s.id = m.thread_id) AS wall_owner_name "
        "FROM messages m WHERE m.sender_id = ? AND m.group_id IS NULL "
        "ORDER BY m.created_at DESC LIMIT 400", (user_id,),
    ).fetchall()
    story_others = [r for r in storyline if r["wall_owner"] != user_id]
    # Egen storyline grupperad per tråd: trådstart (id=thread_id) + egna svar under.
    own_map: dict[int, dict] = {}
    for r in (x for x in storyline if x["wall_owner"] == user_id):
        g = own_map.setdefault(r["thread_id"], {"starter": None, "comments": []})
        if r["id"] == r["thread_id"]:
            g["starter"] = r
        else:
            g["comments"].append(r)
    for g in own_map.values():
        g["comments"].sort(key=lambda r: r["created_at"])
    own_groups = sorted(
        own_map.items(),
        key=lambda kv: (kv[1]["starter"]["created_at"] if kv[1]["starter"]
                        else max((c["created_at"] for c in kv[1]["comments"]), default="")),
        reverse=True,
    )
    community = _user_community(con, user_id, 0, _USER_COMM_PAGE)
    n_story = con.execute(
        "SELECT COUNT(*) FROM messages WHERE sender_id = ? AND group_id IS NULL", (user_id,)
    ).fetchone()[0]
    n_comm = con.execute(
        "SELECT COUNT(*) FROM messages WHERE sender_id = ? AND group_id IS NOT NULL", (user_id,)
    ).fetchone()[0]
    n_own = con.execute(
        "SELECT COUNT(*) FROM messages m WHERE m.sender_id = ? AND m.group_id IS NULL "
        "AND (SELECT s.sender_id FROM messages s WHERE s.id = m.thread_id) = ?",
        (user_id, user_id),
    ).fetchone()[0]
    con.close()
    if not info and not storyline and not community:
        raise HTTPException(404, "användaren finns inte i arkivet")
    return templates.TemplateResponse(request, "archive/user.html", {
        "info": info, "uid": user_id, "community": community,
        "own_groups": own_groups, "story_others": story_others,
        "n_comm": n_comm, "n_own": n_own, "n_others": n_story - n_own,
    })
