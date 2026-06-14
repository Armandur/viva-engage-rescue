"""Hämtar full användarprofil + avatarer.

Kör: uv run python -m scraper.users

Två faser:
  1. roster   - paginerar legacy `users.json` (50/sida). Den endpointen ger
                SAMMA fält som `/users/{id}.json` (ort, avdelning, bio, expertis,
                telefon, mugshot m.m.), så hela nätverkets fulla profiler fångas
                i ett billigt svep. Varje användare sparas rått till
                data/raw/users/{id}.json. Resume via users_roster.state.json.
  2. avatars  - laddar ner mugshot-bilden, men bara för användare som faktiskt
                syns som innehåll i arkivet (avsändare/nämnda/gillare/reagerande
                - INTE rena gruppmedlemmar), till data/avatars/{id}.<ext>.
                Idempotent: befintliga hoppas.

build.py konsumerar data/raw/users/*.json (full profil + raw_json) och kopplar
avatar_local från data/avatars/.
"""

import json
import sqlite3
from collections import deque
from pathlib import Path

from . import yammer
from .yammer import Forbidden, TokenExpired

RAW = Path("data/raw/users")
AVATARS = Path("data/avatars")
DB = Path("data/archive.db")
STATE = Path("data/raw/users_roster.state.json")
PROGRESS = Path("data/users_progress.json")

_CT_EXT = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
           "image/jpeg": ".jpg", "image/jpg": ".jpg"}


def _progress(phase: str, done: int, total: int, recent: list | None = None) -> None:
    PROGRESS.write_text(json.dumps({
        "phase": phase, "done": done, "total": total, "recent": recent or [],
    }), encoding="utf-8")


def _read_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"next_page": 1, "complete": False}


def _write_state(next_page: int, complete: bool) -> None:
    STATE.write_text(json.dumps({"next_page": next_page, "complete": complete}),
                     encoding="utf-8")


def _roster() -> None:
    """Paginerar users.json och dumpar varje användares fulla profil rått.
    Resume från senast klara sida via STATE."""
    RAW.mkdir(parents=True, exist_ok=True)
    st = _read_state()
    if st.get("complete"):
        print(f"Roster redan klar ({len(list(RAW.glob('*.json')))} profiler).")
        return
    page = st.get("next_page", 1)
    done = len(list(RAW.glob("*.json")))
    while True:
        batch = yammer.get("users.json", page=page)
        us = batch if isinstance(batch, list) else batch.get("users", [])
        if not us:
            break
        for u in us:
            (RAW / f"{u['id']}.json").write_text(
                json.dumps(u, ensure_ascii=False), encoding="utf-8")
            done += 1
        _write_state(page + 1, False)
        _progress("roster", done, 0)
        print(f"  roster sida {page}: {len(us)} användare (totalt {done})")
        if len(us) < 50:
            break
        page += 1
    _write_state(page, True)
    _progress("roster", done, 0)
    print(f"Roster klar: {done} profiler i data/raw/users/.")


def _participant_ids() -> list[int]:
    """Användar-id som faktiskt syns som INNEHÅLL i arkivet - mål för
    avatarnedladdning. Medvetet INTE group_members: de stora communityn
    (t.ex. "Anställd i Svenska kyrkan" ~11k) drar in nästan hela organisationen,
    och rena medlemmar visas inte med bild någonstans i UI:t. Bara de som
    postat/nämnts/gillat/reagerat (~5k)."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ids: set[int] = set()
    for sql in (
        "SELECT DISTINCT sender_id FROM messages WHERE sender_id IS NOT NULL",
        "SELECT DISTINCT user_id FROM mentions",
        "SELECT DISTINCT user_id FROM likes",
        "SELECT DISTINCT user_id FROM reactors",
    ):
        try:
            ids.update(r[0] for r in con.execute(sql) if r[0] is not None)
        except sqlite3.Error:
            pass
    con.close()
    return sorted(ids)


def _profile(uid: int) -> dict | None:
    f = RAW / f"{uid}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _mugshot_url(u: dict) -> str | None:
    """Ladda-URL för avatar. OBS: mugshot_url är en SIGNERAD URL som går ut
    (403 efter en stund), så den duger inte om nedladdningen sker långt efter
    att profilen hämtades. mugshot_redirect_url (www.yammer.com) är stabil och
    302:ar till en färskt signerad asset - ladda via den. no_photo-markören
    finns dock bara i mugshot_url, så vi avgör 'saknar foto' utifrån den."""
    plain = u.get("mugshot_url") or ""
    if not plain or "no_photo" in plain.lower():
        return None
    return u.get("mugshot_redirect_url") or plain


def _avatars() -> None:
    if not DB.exists():
        print("Inget arkiv byggt - hoppar avatarer (kör build först).")
        return
    AVATARS.mkdir(parents=True, exist_ok=True)
    ids = _participant_ids()
    total = len(ids)
    print(f"{total} arkivdeltagare att hämta avatar för.")
    done = got = skipped = nophoto = 0
    recent: deque = deque(maxlen=12)  # senast hämtade (id+namn) för panel-tickern
    _progress("avatars", 0, total)
    for uid in ids:
        done += 1
        prof = _profile(uid)
        name = (prof or {}).get("full_name") or (prof or {}).get("name") or str(uid)
        if next(AVATARS.glob(f"{uid}.*"), None):
            skipped += 1
            recent.append({"id": uid, "name": name})
            _progress("avatars", done, total, list(recent))
        else:
            url = _mugshot_url(prof) if prof else None
            if not url:
                nophoto += 1
            else:
                try:
                    tmp = AVATARS / str(uid)
                    ct = yammer.download(url, tmp)
                    ext = _CT_EXT.get(ct.split(";")[0].strip().lower(), ".jpg")
                    tmp.rename(tmp.with_suffix(ext))
                    got += 1
                    recent.append({"id": uid, "name": name})
                    _progress("avatars", done, total, list(recent))
                except Forbidden:
                    nophoto += 1
        if done % 25 == 0:
            _progress("avatars", done, total, list(recent))
            print(f"  avatarer {done}/{total} (nya {got}, fanns {skipped})")
    _progress("avatars", total, total, list(recent))
    print(f"Avatarer klart. Nya: {got}, fanns: {skipped}, ingen bild: {nophoto}.")


def main() -> None:
    try:
        _roster()
        _avatars()
    except TokenExpired:
        print("Token slut - avbryter (kör om för resume).")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
