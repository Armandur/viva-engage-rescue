"""Bygger ett sökbart SQLite-arkiv från råd-dumpen i data/raw/.

Kör: uv run python -m scraper.build

Full ombyggnad varje gång - rådatan i data/raw/ är sanningskällan, så detta är
idempotent och kan köras om när nya sidor dumpats. Dedupar på message-id.
Skapar data/archive.db med FTS5-index för fritextsök.
"""

import json
import sqlite3
from pathlib import Path

RAW = Path("data/raw")
ATT = Path("data/attachments")
DB = Path("data/archive.db")

SCHEMA = """
CREATE TABLE communities (
    id INTEGER PRIMARY KEY, full_name TEXT, description TEXT,
    privacy TEXT, created_at TEXT, web_url TEXT, message_count INTEGER DEFAULT 0
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY, full_name TEXT, email TEXT, job_title TEXT, web_url TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, group_id INTEGER, thread_id INTEGER, replied_to_id INTEGER,
    sender_id INTEGER, created_at TEXT, body_plain TEXT, body_rich TEXT,
    web_url TEXT, system_message INTEGER
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY, message_id INTEGER, type TEXT, name TEXT,
    web_url TEXT, local_path TEXT
);
CREATE INDEX idx_messages_group ON messages(group_id);
CREATE INDEX idx_messages_thread ON messages(thread_id);
CREATE INDEX idx_attachments_msg ON attachments(message_id);
CREATE VIRTUAL TABLE messages_fts USING fts5(body_plain, content='messages', content_rowid='id');
"""


def _local_path(att_id: int) -> str | None:
    if not ATT.exists():
        return None
    hit = next(ATT.glob(f"{att_id}_*"), None)
    return str(hit.relative_to("data")) if hit else None


def main() -> None:
    if not (RAW / "groups").exists():
        raise SystemExit("Ingen rådata i data/raw/ - kör dumpen först.")

    DB.unlink(missing_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    communities, users, messages, attachments = {}, {}, {}, []

    for g in json.loads((RAW / "groups.json").read_text(encoding="utf-8")):
        communities[g["id"]] = (
            g["id"], g.get("full_name") or g.get("name"), g.get("description"),
            g.get("privacy"), g.get("created_at"), g.get("web_url"),
        )

    page_files = (sorted((RAW / "groups").glob("*/page_*.json"))
                  + sorted((RAW / "threads").glob("*/page_*.json")))
    for page in page_files:
        data = json.loads(page.read_text(encoding="utf-8"))
        for r in data.get("references", []):
            if r.get("type") == "user" and r["id"] not in users:
                users[r["id"]] = (r["id"], r.get("full_name") or r.get("name"),
                                  r.get("email"), r.get("job_title"), r.get("web_url"))
        for m in data.get("messages", []):
            body = m.get("body") or {}
            messages[m["id"]] = (
                m["id"], m.get("group_id"), m.get("thread_id"), m.get("replied_to_id"),
                m.get("sender_id"), m.get("created_at"), body.get("plain"),
                body.get("rich"), m.get("web_url"), 1 if m.get("system_message") else 0,
            )
            for a in m.get("attachments", []):
                attachments.append((
                    a.get("id"), m["id"], a.get("type"),
                    a.get("original_name") or a.get("name"),
                    a.get("web_url"), _local_path(a.get("id")) if a.get("type") != "ymodule" else None,
                ))

    con.executemany("INSERT OR REPLACE INTO communities VALUES (?,?,?,?,?,?,0)", communities.values())
    con.executemany("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?)", users.values())
    con.executemany("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)", messages.values())
    con.executemany("INSERT INTO attachments VALUES (?,?,?,?,?,?)", attachments)

    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.execute("UPDATE communities SET message_count = "
                "(SELECT COUNT(*) FROM messages WHERE messages.group_id = communities.id)")
    con.commit()

    c = con.cursor()
    nc = c.execute("SELECT COUNT(*) FROM communities WHERE message_count > 0").fetchone()[0]
    print(f"Arkiv byggt: {DB}")
    print(f"  communities med innehåll: {nc}")
    print(f"  meddelanden: {len(messages)}  |  användare: {len(users)}  |  bilagor: {len(attachments)}")
    con.close()


if __name__ == "__main__":
    main()
