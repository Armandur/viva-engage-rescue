"""Bygger ett sökbart SQLite-arkiv från råd-dumpen i data/raw/.

Kör: uv run python -m scraper.build

Full ombyggnad varje gång - rådatan i data/raw/ är sanningskällan, så detta är
idempotent och kan köras om när nya sidor dumpats. Dedupar på message-id.
Skapar data/archive.db med FTS5-index för fritextsök.
"""

import json
import sqlite3
from pathlib import Path

from . import config

RAW = Path("data/raw")
ATT = Path("data/attachments")
RXN = Path("data/raw/reactions")
CINFO = Path("data/raw/community_info")
UPROF = Path("data/raw/users")
AVATARS = Path("data/avatars")
THUMBS = Path("data/thumbnails")
DB = Path("data/archive.db")

SCHEMA = """
CREATE TABLE communities (
    id INTEGER PRIMARY KEY, full_name TEXT, description TEXT,
    privacy TEXT, created_at TEXT, web_url TEXT,
    company_group INTEGER, moderated INTEGER, restricted_posting INTEGER,
    accessible INTEGER, message_count INTEGER DEFAULT 0,
    extended_description TEXT, member_count INTEGER
);
CREATE TABLE pinned (group_id INTEGER, title TEXT, url TEXT, type TEXT, description TEXT);
CREATE TABLE group_members (group_id INTEGER, user_id INTEGER, is_admin INTEGER);
CREATE TABLE users (
    id INTEGER PRIMARY KEY, full_name TEXT, name TEXT, email TEXT, job_title TEXT,
    web_url TEXT, mugshot_url TEXT, state TEXT, aad_guest INTEGER, activated_at TEXT,
    location TEXT, department TEXT, summary TEXT, expertise TEXT, interests TEXT,
    hire_date TEXT, birth_date TEXT, phone TEXT, timezone TEXT, network_name TEXT,
    avatar_local TEXT, raw_json TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, group_id INTEGER, thread_id INTEGER, replied_to_id INTEGER,
    sender_id INTEGER, created_at TEXT, body_plain TEXT, body_rich TEXT,
    body_parsed TEXT, body_urls TEXT,
    web_url TEXT, system_message INTEGER, like_count INTEGER DEFAULT 0,
    title TEXT, message_type TEXT, privacy TEXT, shared_message_id INTEGER
);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE polls (message_id INTEGER, option_index INTEGER, answer TEXT);
CREATE TABLE attachments (
    id INTEGER, message_id INTEGER, type TEXT, name TEXT,
    web_url TEXT, local_path TEXT, description TEXT, thumb_local TEXT
);
CREATE TABLE mentions (message_id INTEGER, user_id INTEGER);
CREATE TABLE likes (message_id INTEGER, user_id INTEGER);
CREATE TABLE thread_meta (thread_id INTEGER PRIMARY KEY, seen_by_count INTEGER,
    best_reply_id INTEGER, verified_reply_id INTEGER);
CREATE TABLE reactions (message_id INTEGER, type TEXT, count INTEGER);
CREATE TABLE reactors (message_id INTEGER, type TEXT, user_id INTEGER);
CREATE TABLE upvotes (message_id INTEGER, count INTEGER);
CREATE TABLE upvoters (message_id INTEGER, user_id INTEGER);
CREATE INDEX idx_messages_group ON messages(group_id);
CREATE INDEX idx_messages_thread ON messages(thread_id);
CREATE INDEX idx_attachments_msg ON attachments(message_id);
CREATE INDEX idx_mentions_msg ON mentions(message_id);
CREATE INDEX idx_likes_msg ON likes(message_id);
CREATE INDEX idx_reactions_msg ON reactions(message_id);
CREATE INDEX idx_reactors_msg ON reactors(message_id);
CREATE INDEX idx_upvotes_msg ON upvotes(message_id);
CREATE INDEX idx_pinned_group ON pinned(group_id);
CREATE INDEX idx_members_group ON group_members(group_id);
CREATE VIRTUAL TABLE messages_fts USING fts5(body_plain, content='messages', content_rowid='id');
"""


def _local_path(att_id: int) -> str | None:
    if not ATT.exists():
        return None
    hit = next(ATT.glob(f"{att_id}_*"), None)
    return str(hit.relative_to("data")) if hit else None


def _avatar_path(uid: int) -> str | None:
    if not AVATARS.exists():
        return None
    hit = next(AVATARS.glob(f"{uid}.*"), None)
    return str(hit.relative_to("data")) if hit else None


def _thumb_path(att_id) -> str | None:
    if att_id is None or not THUMBS.exists():
        return None
    hit = next(THUMBS.glob(f"{att_id}.*"), None)
    return str(hit.relative_to("data")) if hit else None


def _phone(contact: dict | None) -> str | None:
    """Plockar första telefonnumret ur users.json:s contact-block."""
    for p in ((contact or {}).get("phone_numbers") or []):
        if p.get("number"):
            return p["number"]
    return None


def _profile_row(u: dict) -> tuple:
    """Full profil + raw_json ur en users.json-post -> UPDATE-tupel (id sist)."""
    return (
        u.get("full_name") or u.get("name"), u.get("name"), u.get("email"),
        u.get("job_title"), u.get("web_url"), u.get("mugshot_url"), u.get("state"),
        1 if u.get("aad_guest") else 0, u.get("activated_at"),
        u.get("location"), u.get("department"), u.get("summary"),
        u.get("expertise"), u.get("interests"), u.get("hire_date"),
        u.get("birth_date"), _phone(u.get("contact")), u.get("timezone"),
        u.get("network_name"), _avatar_path(u["id"]),
        json.dumps(u, ensure_ascii=False), u["id"],
    )


def main() -> None:
    if not (RAW / "groups").exists():
        raise SystemExit("Ingen rådata i data/raw/ - kör dumpen först.")

    DB.unlink(missing_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    communities, users, messages, attachments = {}, {}, {}, {}
    mentions, likes, polls, tags = {}, {}, {}, {}

    # Exkluderade grupper (våra egna test-/arkiv-grupper i nätverket) ska aldrig in.
    exc = config.excluded_groups()
    if exc:
        print(f"  exkluderar {len(exc)} grupper ur bygget: {sorted(exc)}")

    for g in json.loads((RAW / "groups.json").read_text(encoding="utf-8")):
        if g["id"] in exc:
            continue
        # accessible=0 om dumpen fick åtkomst nekad (privat grupp utan medlemskap).
        accessible = 0 if (RAW / "groups" / str(g["id"]) / ".skipped").exists() else 1
        communities[g["id"]] = (
            g["id"], g.get("full_name") or g.get("name"), g.get("description"),
            g.get("privacy"), g.get("created_at"), g.get("web_url"),
            1 if g.get("company_group") else 0, 1 if g.get("moderated") else 0,
            1 if g.get("restricted_posting") else 0, accessible,
        )

    page_files = (sorted((RAW / "groups").glob("*/page_*.json"))
                  + sorted((RAW / "threads").glob("*/page_*.json")))
    for page in page_files:
        data = json.loads(page.read_text(encoding="utf-8"))
        for r in data.get("references", []):
            if r.get("type") == "user" and r["id"] not in users:
                users[r["id"]] = (
                    r["id"], r.get("full_name") or r.get("name"), r.get("name"),
                    r.get("email"), r.get("job_title"), r.get("web_url"),
                    r.get("mugshot_url"), r.get("state"),
                    1 if r.get("aad_guest") else 0, r.get("activated_at"),
                )
            elif r.get("type") == "tag" and r["id"] not in tags:
                tags[r["id"]] = (r["id"], r.get("name"))
        for m in data.get("messages", []):
            if m.get("group_id") in exc:
                continue
            body = m.get("body") or {}
            lb = m.get("liked_by") or {}
            urls = body.get("urls") or []
            messages[m["id"]] = (
                m["id"], m.get("group_id"), m.get("thread_id"), m.get("replied_to_id"),
                m.get("sender_id"), m.get("created_at"), body.get("plain"),
                body.get("rich"), body.get("parsed"),
                json.dumps(urls, ensure_ascii=False) if urls else None,
                m.get("web_url"), 1 if m.get("system_message") else 0,
                lb.get("count") or 0, m.get("title"), m.get("message_type"),
                m.get("privacy"), m.get("shared_message_id"),
            )
            for uid in (m.get("notified_user_ids") or []):
                mentions[(m["id"], uid)] = (m["id"], uid)
            for n in (lb.get("names") or []):
                if n.get("user_id"):
                    likes[(m["id"], n["user_id"])] = (m["id"], n["user_id"])
            for p in (m.get("poll_options") or []):
                polls[(m["id"], p.get("option"))] = (m["id"], p.get("option"), p.get("answer"))
            for a in m.get("attachments", []):
                attachments[(a.get("id"), m["id"])] = (
                    a.get("id"), m["id"], a.get("type"),
                    a.get("original_name") or a.get("name"),
                    a.get("web_url"), _local_path(a.get("id")) if a.get("type") != "ymodule" else None,
                    a.get("description"), _thumb_path(a.get("id")),
                )

    # Berikning (reaktioner + seen) från GraphQL-passet, om det körts.
    # Reaktor-användare som inte fanns i v1-datan läggs till minimalt.
    thread_meta, reactions, reactors, upvotes, upvoters = {}, [], [], [], []
    if RXN.exists():
        for f in RXN.glob("*.json"):
            d = json.loads(f.read_text(encoding="utf-8"))
            tid = d["thread_id"]
            if (d.get("seen_by_count") is not None or d.get("best_reply_id")
                    or d.get("verified_reply_id")):
                thread_meta[tid] = (tid, d.get("seen_by_count"),
                                    d.get("best_reply_id"), d.get("verified_reply_id"))
            for mid_s, rec in d.get("messages", {}).items():
                mid = int(mid_s)
                for typ, cnt in (rec.get("reactions") or {}).items():
                    reactions.append((mid, typ, cnt))
                for typ, uids in (rec.get("reactors") or {}).items():
                    for uid in uids:
                        reactors.append((mid, typ, int(uid)))
                uv = rec.get("upvotes") or {}
                if uv.get("count"):
                    upvotes.append((mid, uv["count"]))
                    for uid in (uv.get("upvoters") or []):
                        upvoters.append((mid, int(uid)))
            for uid_s, info in (d.get("users") or {}).items():
                uid = int(uid_s)
                if uid not in users:
                    users[uid] = (uid, info.get("name"), None, info.get("email"),
                                  info.get("job_title"), None, None, None, None, None)

    con.executemany(
        "INSERT OR REPLACE INTO communities "
        "(id, full_name, description, privacy, created_at, web_url, "
        "company_group, moderated, restricted_posting, accessible, message_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,0)", communities.values())
    con.executemany(
        "INSERT OR REPLACE INTO users "
        "(id, full_name, name, email, job_title, web_url, mugshot_url, state, "
        "aad_guest, activated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", users.values())
    con.executemany("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", messages.values())
    con.executemany("INSERT OR REPLACE INTO tags VALUES (?,?)", tags.values())
    con.executemany("INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?)", attachments.values())
    con.executemany("INSERT INTO mentions VALUES (?,?)", mentions.values())
    con.executemany("INSERT INTO likes VALUES (?,?)", likes.values())
    con.executemany("INSERT INTO polls VALUES (?,?,?)", polls.values())

    # Community-info (GraphQL-passet): info-text, medlemsantal, fästa länkar, ev.
    # featured-medlemmar. Uppdaterar communities + fyller pinned/group_members.
    cinfo_updates, pinned_rows, member_rows, member_users = [], [], [], []
    if CINFO.exists():
        for f in CINFO.glob("*.json"):
            d = json.loads(f.read_text(encoding="utf-8"))
            gid = d["group_id"]
            cinfo_updates.append((d.get("extended_description"), d.get("member_count"), gid))
            for p in d.get("pinned") or []:
                pinned_rows.append((gid, p.get("title"), p.get("url"), p.get("type"), p.get("description")))
            for mrow in d.get("members") or []:
                uid = mrow.get("user_id")
                if uid is None:
                    continue
                member_rows.append((gid, uid, 1 if mrow.get("is_admin") else 0))
                member_users.append((uid, mrow.get("name"), mrow.get("email"), mrow.get("job_title")))
    con.executemany(
        "UPDATE communities SET extended_description=?, member_count=? WHERE id=?",
        cinfo_updates)
    # Medlemmar som aldrig postat saknas i users - lägg till minimalt (befintliga behålls).
    con.executemany(
        "INSERT OR IGNORE INTO users (id, full_name, email, job_title) VALUES (?,?,?,?)",
        member_users)
    con.executemany("INSERT INTO pinned VALUES (?,?,?,?,?)", pinned_rows)
    con.executemany("INSERT INTO group_members VALUES (?,?,?)", member_rows)

    # Full användarprofil (users.json-passet): hela nätverkets profiler med ort,
    # avdelning, bio, expertis, telefon, avatar m.m. Säkerställ att raden finns
    # (medlemmar som aldrig postat) och fyll alla profilfält.
    nprof = 0
    if UPROF.exists():
        rows = []
        for f in UPROF.glob("*.json"):
            try:
                u = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if u.get("id") is None:
                continue
            con.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (u["id"],))
            rows.append(_profile_row(u))
        con.executemany(
            "UPDATE users SET full_name=?, name=?, email=?, job_title=?, web_url=?, "
            "mugshot_url=?, state=?, aad_guest=?, activated_at=?, location=?, "
            "department=?, summary=?, expertise=?, interests=?, hire_date=?, "
            "birth_date=?, phone=?, timezone=?, network_name=?, avatar_local=?, "
            "raw_json=? WHERE id=?", rows)
        nprof = len(rows)

    con.executemany("INSERT INTO thread_meta VALUES (?,?,?,?)", thread_meta.values())
    con.executemany("INSERT INTO reactions VALUES (?,?,?)", reactions)
    con.executemany("INSERT INTO reactors VALUES (?,?,?)", reactors)
    con.executemany("INSERT INTO upvotes VALUES (?,?)", upvotes)
    con.executemany("INSERT INTO upvoters VALUES (?,?)", upvoters)

    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.execute("UPDATE communities SET message_count = "
                "(SELECT COUNT(*) FROM messages WHERE messages.group_id = communities.id)")
    # Privat grupp utan något arkiverat innehåll = vi är inte medlem (in_group ger
    # 200+tomt, users/in_group ger 403). Markera som ej åtkomstbar.
    con.execute("UPDATE communities SET accessible = 0 "
                "WHERE privacy = 'private' AND message_count = 0")
    con.commit()

    c = con.cursor()
    nc = c.execute("SELECT COUNT(*) FROM communities WHERE message_count > 0").fetchone()[0]
    nusers = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"Arkiv byggt: {DB}")
    print(f"  communities med innehåll: {nc}")
    print(f"  meddelanden: {len(messages)}  |  användare: {nusers}  |  bilagor: {len(attachments)}")
    if reactions or thread_meta:
        print(f"  reaktioner: {len(reactions)} rader  |  trådar m. seen: {len(thread_meta)}")
    if nprof:
        navatar = c.execute("SELECT COUNT(*) FROM users WHERE avatar_local IS NOT NULL").fetchone()[0]
        print(f"  fulla profiler: {nprof}  |  avatarer: {navatar}")
    con.close()


if __name__ == "__main__":
    main()
